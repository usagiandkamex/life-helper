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


async def link_fund(ctx: AppContext, holding_id: str, provider: str, fund_code: str = "") -> dict:
    """Ties a holding to an official fund after the user picked it. ``manual`` keeps the hand-entered NAV."""
    holding = _holding(ctx, holding_id)
    if holding is None:
        return {"error": f"id {holding_id} の銘柄が見つかりません"}
    if holding.kind != "fund":
        return {"error": "基準価額の取得元は投資信託にだけ設定できます"}
    quote = None
    if provider == MANUAL_PROVIDER:
        fund = FundRef(provider=MANUAL_PROVIDER, price_unit=float(holding.price_unit))
    else:
        connector = fund_connectors(ctx).get(provider)
        if connector is None:
            return {"error": f"対応していないデータ提供元です: {provider}"}
        try:
            quote = await connector.fund_nav(fund_code)
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
        if target is None:
            return {"error": f"id {holding_id} の銘柄が見つかりません"}
        target.fund = fund
        if quote:
            target.apply_price(nav_price(quote))
    official = quote["name"] if quote else None
    return {"ok": True, "id": holding_id, "fund": fund.model_dump(mode="json"), "official_name": official}


async def refresh_fund_navs(ctx: AppContext) -> dict:
    """Updates the NAV of every linked fund. One fund failing leaves the others, and its own NAV, untouched."""
    store = portfolio_store(ctx)
    today = date.today()
    connectors = fund_connectors(ctx)
    holdings = [h for h in store.load().holdings if h.kind == "fund"]
    wanted = sorted({(h.fund.provider, h.fund.fund_code) for h in holdings if h.fund and h.fund.automatic})
    quotes: dict[tuple[str, str], dict] = {}
    errors = []
    # Fetch first, then apply under the file lock, so the lock is never held across network calls.
    for provider, code in wanted:
        connector = connectors.get(provider)
        if connector is None:
            errors.append({"code": code, "error": f"対応していないデータ提供元です（{provider}）"})
            continue
        try:
            quotes[(provider, code)] = await connector.fund_nav(code, today=today)
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
            quote = quotes.get((holding.fund.provider, holding.fund.fund_code))
            # apply_price keeps the newer NAV, so a provider replaying an old date never overwrites a newer one.
            if not quote or not holding.apply_price(nav_price(quote)):
                continue
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
