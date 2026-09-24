"""Portfolio operations shared by the Copilot tools and the HTTP API."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from ..connectors.base import ConnectorError
from ..connectors.registry import get_connectors
from ..tools.tax_params import load_tax_params
from .portfolio import Portfolio, PortfolioStore, Price, nisa_allowance

if TYPE_CHECKING:
    from ..context import AppContext

PRICED_BY_STOOQ = ("stock", "etf", "reit")


def portfolio_store(ctx: AppContext) -> PortfolioStore:
    store = ctx.extras.get("portfolio_store")
    if store is None:
        store = PortfolioStore(ctx.settings.knowledge_dir)
        ctx.extras["portfolio_store"] = store
    return store


async def stock_price(ctx: AppContext, code: str, *, today: date | None = None) -> dict:
    today = today or date.today()
    store = portfolio_store(ctx)
    cached = store.cached_price(code, today)
    if cached:
        return cached | {"cached": True}
    result = await get_connectors(ctx)["stooq"].previous_close(code, today=today)
    store.cache_price(code, today, result)
    return result | {"cached": False}


async def refresh_stock_prices(ctx: AppContext) -> dict:
    store = portfolio_store(ctx)
    codes = sorted({h.code for h in store.load().holdings if h.kind in PRICED_BY_STOOQ and h.code})
    prices: dict[str, dict] = {}
    errors = []
    # Fetch first, then apply under the file lock, so the lock is never held across network calls.
    for code in codes:
        try:
            prices[code] = await stock_price(ctx, code)
        except ConnectorError as e:
            errors.append({"code": code, "error": str(e)})
    updated = []
    with store.transaction() as portfolio:
        for holding in portfolio.holdings:
            result = prices.get(holding.code) if holding.kind in PRICED_BY_STOOQ else None
            if result and holding.apply_price(Price(value=result["close"], date=result["date"], source="stooq")):
                updated.append(
                    {"code": holding.code, "name": holding.name, "close": result["close"], "date": result["date"]}
                )
    return {
        "updated": updated,
        "errors": errors,
        "note": "投資信託は Stooq の対象外です。"
        "基準価額はチャットで運用会社のサイトから取得するか、手入力してください。",
    }


def nisa_status(ctx: AppContext, portfolio: Portfolio, year: int) -> dict:
    params = load_tax_params(ctx.settings.tax_params_dir, year)
    return nisa_allowance(portfolio, year, params["nisa"]) | {"warnings": params.warnings}
