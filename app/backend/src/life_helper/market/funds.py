"""Fund NAV lookup: linking a holding to an official fund and refreshing 基準価額.

Kept apart from the Yahoo Finance stock refresh on purpose: funds are not traded on an exchange, so their prices
come from the fund library and the managers instead, and a failure on one side never blocks the other.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from ..connectors.base import ConnectorError
from ..connectors.fund_nav import (
    MAX_CANDIDATES,
    FundNavConnector,
    TooManyFundsError,
    nav_amount,
    nav_date,
    normalize_name,
)
from ..connectors.registry import get_connectors
from .clock import market_today
from .portfolio import DEFAULT_PRICE_UNIT, FundRef, Holding, Price
from .service import portfolio_store

if TYPE_CHECKING:
    from ..context import AppContext

logger = logging.getLogger(__name__)

MANUAL_PROVIDER = "manual"
NOTE = (
    "基準価額は投資信託協会の投信総合検索ライブラリー（予備として運用会社の公式 CSV）から取得しています"
    "（株価の Yahoo Finance 更新とは別処理です）。取得元が未設定の投資信託は、ファンド名が一致する公式ファンドが"
    " 1 つだけなら自動で紐付けます。紐付けられなかったファンドは「取得元を設定」から選ぶか、"
    "公式サイトで確認して手入力してください。"
)
# A fund linked by name is checked against the NAV the holding already has (from the broker CSV): a candidate
# quoting a very different NAV is another fund, or quotes another number of units, so it is left to the user.
AUTO_LINK_NAV_RATIO = (0.5, 2.0)


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


def _plausible(holding: Holding, nav: float | None) -> bool:
    """False when the holding's current NAV and the official one are too far apart to be the same fund."""
    if holding.price is None or holding.price.value <= 0 or not nav:
        return True
    low, high = AUTO_LINK_NAV_RATIO
    return low <= nav / holding.price.value <= high


async def _verified_quote(ctx: AppContext, candidate: dict) -> dict:
    """The NAV of ``candidate``, fetched through its fund page, and checked to be the fund the search named."""
    quote = await fund_connectors(ctx)[candidate["provider"]].fund_nav(candidate["fund_code"], today=market_today())
    listed = candidate.get("association_code")
    if quote["fund_code"] != candidate["fund_code"] or (listed and quote["association_code"] != listed):
        raise ConnectorError("検索結果とファンドページで協会コードが一致しないため、紐付けませんでした")
    return quote


async def auto_link_funds(ctx: AppContext, *, quotes: dict[tuple[str, str], dict] | None = None) -> dict:
    """Links every fund without a source to the one official fund with the same name, if there is exactly one.

    A name that matches no fund, or more than one, is left for the user to choose, and a failed search is
    reported as an error rather than as "no match". Funds the user set to manual entry are never touched.
    A fund is only linked once its NAV was fetched through the ISIN's own fund page; the NAVs fetched on the way
    are put in ``quotes`` (keyed like the links, by provider and fund code) so a refresh need not fetch them again.
    """
    store = portfolio_store(ctx)
    report: dict[str, list[dict]] = {"linked": [], "ambiguous": [], "unmatched": [], "errors": []}
    pending = [h for h in store.load().holdings if h.kind == "fund" and h.fund is None and h.name.strip()]
    by_name: dict[str, list[Holding]] = {}
    for h in pending:
        by_name.setdefault(normalize_name(h.name), []).append(h)
    chosen: dict[str, tuple[dict, dict]] = {}
    # Search and fetch outside the file lock, then apply under it: the lock is never held across network calls.
    for group in by_name.values():
        rows = [{"id": h.id, "name": h.name} for h in group]
        try:
            candidates = [
                c
                for connector in fund_connectors(ctx).values()
                for c in await connector.search_funds(group[0].name, exhaustive=True)
            ]
            exact = {(c["provider"], c["fund_code"]): c for c in candidates if c["exact"]}
            if len(exact) != 1:
                if exact:
                    reason = f"同じ名前の公式ファンドが {len(exact)} 件あります"
                    report["ambiguous"] += [r | {"reason": reason} for r in rows]
                else:
                    report["unmatched"] += rows
                continue
            (candidate,) = exact.values()
            quote = await _verified_quote(ctx, candidate)
        except TooManyFundsError as e:
            report["ambiguous"] += [r | {"reason": str(e)} for r in rows]
            continue
        except ConnectorError as e:
            report["errors"] += [r | {"error": str(e)} for r in rows]
            continue
        if quotes is not None:
            quotes[(candidate["provider"], quote["fund_code"])] = quote
        for h, row in zip(group, rows, strict=True):
            if _plausible(h, quote["nav"]):
                chosen[h.id] = (candidate, quote)
            else:
                report["ambiguous"] += [row | {"reason": "保有中の基準価額と公式の基準価額が大きく違います"}]
    before = {h.id: (h.kind, h.name, h.code) for h in pending}
    with store.transaction() as portfolio:
        for target in portfolio.holdings:
            if target.id not in chosen:
                continue
            # The holding may have been edited, relinked or set to manual entry while the search was running.
            if target.fund is not None or (target.kind, target.name, target.code) != before[target.id]:
                continue
            candidate, quote = chosen[target.id]
            target.fund = FundRef(
                provider=candidate["provider"],
                fund_code=quote["fund_code"],
                manager=candidate["manager"],
                isin=quote["isin"],
                association_code=quote["association_code"],
                price_unit=quote["price_unit"],
                source_url=quote["source_url"],
            )
            report["linked"].append(
                {
                    "id": target.id,
                    "name": target.name,
                    "official_name": candidate["name"],
                    "code": quote["fund_code"],
                }
            )
    return report


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
    """Links a holding to a user-selected fund and optionally saves a manual NAV with its basis date.

    A response received after its holding was renamed or re-linked is discarded so it cannot change a new selection.
    """
    holding = _holding(ctx, holding_id)
    if holding is None:
        return {"error": f"id {holding_id} の銘柄が見つかりません"}
    if holding.kind != "fund":
        return {"error": "基準価額の取得元は投資信託にだけ設定できます"}
    quote = None
    if provider == MANUAL_PROVIDER:
        if (manual_nav is None) != (price_date is None):
            return {"error": "手入力する基準価額と基準日を両方指定してください"}
        if manual_nav is not None:
            try:
                manual_nav = nav_amount(manual_nav)
                nav_date(price_date, latest=market_today())
            except ConnectorError as e:
                return {"error": str(e)}
        fund = FundRef(provider=MANUAL_PROVIDER, price_unit=price_unit)
    else:
        if manual_nav is not None or price_date is not None:
            return {"error": "基準価額と基準日は手入力のときだけ指定できます"}
        connector = fund_connectors(ctx).get(provider)
        if connector is None:
            return {"error": f"対応していないデータ提供元です: {provider}"}
        try:
            quote = await connector.fund_nav(fund_code, today=market_today())
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
            # Replacing the source also replaces its price, even when the hand-entered basis date is older.
            target.price = None
            target.apply_price(Price(value=manual_nav, date=price_date.isoformat(), source=MANUAL_PROVIDER))
        elif unit_changed:
            target.price, target.valuation_yen = None, None
    official = quote["name"] if quote else None
    return {"ok": True, "id": holding_id, "fund": fund.model_dump(mode="json"), "official_name": official}


async def refresh_fund_navs(ctx: AppContext) -> dict:
    """Links unlinked funds by name, then updates the NAV of every linked fund.

    One fund failing leaves the others, and its own NAV, untouched.
    """
    fetched: dict[tuple[str, str], dict] = {}
    links = await auto_link_funds(ctx, quotes=fetched)
    store = portfolio_store(ctx)
    today = market_today()
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
            # A fund linked a moment ago already had its NAV fetched to confirm the link.
            quote = fetched.get((provider, code)) or await connector.fund_nav(code, today=today)
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
            if not found:
                continue
            quote = found[0]
            # The codes the source confirmed replace whatever the link was saved with (a migrated link, say).
            holding.fund.isin = quote["isin"] or holding.fund.isin
            holding.fund.association_code = quote["association_code"] or holding.fund.association_code
            # apply_price keeps the newer NAV, so a provider replaying an old date never overwrites a newer one.
            if not holding.apply_price(found[1]):
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
        "auto_link": links,
        "manual": [{"id": h.id, "name": h.name} for h in holdings if not (h.fund and h.fund.automatic)],
        "note": NOTE,
    }
