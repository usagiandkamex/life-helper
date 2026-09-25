"""Fund NAV lookup: linking a holding to an official fund and refreshing 基準価額.

Kept apart from the Stooq stock refresh on purpose: funds are not traded on an exchange, so their prices come
from the fund manager instead, and a failure on one side never blocks the other.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from ..connectors.base import ConnectorError
from ..connectors.fund_nav import MAX_CANDIDATES, FundNavConnector
from ..connectors.registry import get_connectors
from .portfolio import DEFAULT_PRICE_UNIT, FundRef, Holding, Price
from .service import portfolio_store

if TYPE_CHECKING:
    from ..context import AppContext

logger = logging.getLogger(__name__)

MANUAL_PROVIDER = "manual"
NOTE = (
    "基準価額は運用会社の公式 API・公式 CSV から取得しています（株価の Stooq 更新とは別処理です）。"
    "自動取得に対応していないファンドは、公式サイトで確認して手入力してください。"
)


def fund_connectors(ctx: AppContext) -> dict[str, FundNavConnector]:
    return {name: c for name, c in get_connectors(ctx).items() if isinstance(c, FundNavConnector)}


def fund_providers(ctx: AppContext) -> list[dict]:
    """Data sources the screen can offer, plus the manual fallback for funds nothing supports yet."""
    providers = [
        {"provider": c.provider, "label": c.info.label, "manager": c.manager, "price_unit": c.price_unit}
        for c in fund_connectors(ctx).values()
    ]
    return providers + [
        {"provider": MANUAL_PROVIDER, "label": "手入力", "manager": "", "price_unit": DEFAULT_PRICE_UNIT}
    ]


def nav_price(quote: dict) -> Price:
    return Price(
        value=quote["nav"],
        date=quote["date"],
        source=quote["source"],
        source_url=quote["source_url"],
        fetched_at=datetime.now(UTC).isoformat(),
    )


def _holding(ctx: AppContext, holding_id: str) -> Holding | None:
    return next((h for h in portfolio_store(ctx).load().holdings if h.id == holding_id), None)


async def suggest_funds(ctx: AppContext, name: str) -> dict:
    """Official funds whose name is close to ``name``. Candidates only: the user confirms before linking."""
    candidates: list[dict] = []
    errors = []
    for provider, connector in fund_connectors(ctx).items():
        try:
            candidates += await connector.search_funds(name)
        except ConnectorError as e:
            errors.append({"code": provider, "error": str(e)})
    candidates.sort(key=lambda c: (-c["score"], c["name"]))
    return {
        "name": name,
        "candidates": candidates[:MAX_CANDIDATES],
        "errors": errors,
        "note": "名前が似ているだけの別ファンドがあります。公式名称を確認してから紐付けてください。",
    }


def keep_fund_links(previous: list[Holding], imported: list[Holding]) -> None:
    """Carries confirmed fund links over a CSV import, which replaces the holdings.

    Only an identical fund name is carried over. A name that merely looks similar is left for the user to
    confirm again, so a re-import can never move a link to another fund.
    """
    links: dict[str, FundRef] = {}
    for h in previous:
        if h.kind == "fund" and h.fund:
            links.setdefault(h.name, h.fund)
    for h in imported:
        if h.kind == "fund" and h.fund is None and h.name in links:
            h.fund = links[h.name].model_copy(deep=True)


async def link_fund(
    ctx: AppContext,
    holding_id: str,
    provider: str,
    fund_code: str = "",
    price_unit: float = DEFAULT_PRICE_UNIT,
    manual_nav: float | None = None,
    price_date: date | None = None,
) -> dict:
    """Ties a holding to an official fund after the user picked it. ``manual`` keeps the hand-entered NAV."""
    holding = _holding(ctx, holding_id)
    if holding is None:
        return {"error": f"id {holding_id} の銘柄が見つかりません"}
    if holding.kind != "fund":
        return {"error": "基準価額の取得元は投資信託にだけ設定できます"}
    quote = None
    if provider == MANUAL_PROVIDER:
        if manual_nav is not None and price_date is None:
            return {"error": "手入力した基準価額の基準日を指定してください"}
        fund = FundRef(provider=MANUAL_PROVIDER, price_unit=price_unit)
    else:
        connector = fund_connectors(ctx).get(provider)
        if connector is None:
            return {"error": f"対応していないデータ提供元です: {provider}"}
        try:
            quote = await connector.fund_nav(fund_code)
            price = nav_price(quote)
        except ConnectorError as e:
            return {"error": str(e)}
        fund = FundRef(
            provider=connector.provider,
            fund_code=quote["fund_code"],
            manager=quote["manager"],
            isin=quote["isin"],
            association_code=quote["association_code"],
            price_unit=quote["price_unit"],
            source_url=quote["source_url"],
        )
    with portfolio_store(ctx).transaction() as portfolio:
        target = next((h for h in portfolio.holdings if h.id == holding_id), None)
        if target is None or target.kind != "fund":
            return {"error": f"id {holding_id} の投資信託が見つかりません"}
        if target.name != holding.name or target.fund != holding.fund:
            return {"error": "保有銘柄が変更されました。内容を確認してもう一度紐付けてください"}
        # A price kept from another fund, or quoted for another number of units, would value this holding wrongly.
        relinked = target.fund is not None and (target.fund.provider, target.fund.fund_code) != (
            fund.provider,
            fund.fund_code,
        )
        unit_changed = float(target.price_unit) != fund.price_unit
        target.fund = fund
        if quote:
            if relinked or unit_changed:
                target.price, target.valuation_yen = price, None
            else:
                target.apply_price(price)
        elif manual_nav is not None:
            target.price = Price(value=manual_nav, date=price_date.isoformat(), source=MANUAL_PROVIDER)
            target.valuation_yen = None
        elif unit_changed:
            target.price, target.valuation_yen = None, None
    official = quote["name"] if quote else None
    return {"ok": True, "id": holding_id, "fund": fund.model_dump(mode="json"), "official_name": official}


async def refresh_fund_navs(ctx: AppContext) -> dict:
    """Updates the NAV of every linked fund. One fund failing leaves the others, and its own NAV, untouched."""
    store = portfolio_store(ctx)
    today = date.today()
    connectors = fund_connectors(ctx)
    holdings = [h for h in store.load().holdings if h.kind == "fund"]
    wanted = sorted({(h.fund.provider, h.fund.fund_code) for h in holdings if h.fund and h.fund.automatic})
    quotes: dict[tuple[str, str], tuple[dict, Price]] = {}
    errors = []
    # Fetch and validate first, then apply under the file lock: the lock is never held across network calls,
    # and a malformed answer is turned into an error for that fund instead of aborting every other update.
    for provider, code in wanted:
        connector = connectors.get(provider)
        if connector is None:
            errors.append({"code": code, "error": f"対応していないデータ提供元です（{provider}）"})
            continue
        try:
            quote = await connector.fund_nav(code, today=today)
            quotes[(provider, code)] = (quote, nav_price(quote))
        except ConnectorError as e:
            errors.append({"code": code, "error": str(e)})
        except (ValueError, KeyError, TypeError):
            logger.warning("could not build a NAV for %s", code, exc_info=True)
            errors.append({"code": code, "error": "取得した基準価額を取り込めませんでした（データの形式が不正です）"})
    updated = []
    with store.transaction() as portfolio:
        for holding in portfolio.holdings:
            if holding.kind != "fund" or not holding.fund or not holding.fund.automatic:
                continue
            found = quotes.get((holding.fund.provider, holding.fund.fund_code))
            # apply_price keeps the newer NAV, so a provider replaying an old date never overwrites a newer one.
            if not found or not holding.apply_price(found[1]):
                continue
            quote = found[0]
            holding.fund.price_unit = quote["price_unit"]
            holding.fund.source_url = quote["source_url"]
            updated.append(
                {
                    "code": holding.fund.fund_code,
                    "name": holding.name,
                    "official_name": quote["name"],
                    "nav": quote["nav"],
                    "price_unit": quote["price_unit"],
                    "date": quote["date"],
                    "source": quote["source"],
                    "source_url": quote["source_url"],
                }
            )
    return {
        "updated": updated,
        "errors": errors,
        "manual": [{"id": h.id, "name": h.name} for h in holdings if not (h.fund and h.fund.automatic)],
        "note": NOTE,
    }
