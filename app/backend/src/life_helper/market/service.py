"""Portfolio operations shared by the Copilot tools and the HTTP API."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from ..connectors.base import ConnectorError
from ..connectors.registry import get_connectors
from .clock import market_today
from .portfolio import PortfolioStore, Price

if TYPE_CHECKING:
    from ..context import AppContext

logger = logging.getLogger(__name__)

PRICED_BY_STOOQ = ("stock", "etf", "reit")
# Six characters, so it can never clash with a security code in the per-day price cache.
FX_CACHE_CODE = "USDJPY"
QUOTE_KEYS = frozenset(
    {"code", "symbol", "market", "currency", "close", "close_jpy", "date", "source", "fx_rate", "fx_date", "fx_source"}
)
FX_KEYS = frozenset({"pair", "symbol", "date", "rate", "source"})


def portfolio_store(ctx: AppContext) -> PortfolioStore:
    store = ctx.extras.get("portfolio_store")
    if store is None:
        store = PortfolioStore(ctx.settings.knowledge_dir)
        ctx.extras["portfolio_store"] = store
    return store


def _cached(store: PortfolioStore, code: str, on: date, keys: frozenset[str]) -> dict | None:
    """Same-day cache entry, ignored when it was written before the current fields existed."""
    cached = store.cached_price(code, on)
    return cached if isinstance(cached, dict) and keys <= cached.keys() else None


async def usd_jpy_rate(ctx: AppContext, *, today: date | None = None, memo: dict | None = None) -> dict:
    """USD/JPY previous close. Cached for the day; ``memo`` also avoids repeating a failure within one refresh."""
    today = today or market_today()
    if memo is not None and "error" in memo:
        raise memo["error"]
    store = portfolio_store(ctx)
    cached = _cached(store, FX_CACHE_CODE, today, FX_KEYS)
    if cached:
        return cached
    try:
        rate = await get_connectors(ctx)["stooq"].usd_jpy(today=today)
    except ConnectorError as e:
        if memo is not None:
            memo["error"] = e
        raise
    store.cache_price(FX_CACHE_CODE, today, rate)
    return rate


async def _in_yen(ctx: AppContext, quote: dict, *, today: date, fx_memo: dict | None) -> dict:
    """Adds the yen value. US closes are converted with USD/JPY; without a rate the quote is not usable."""
    if quote["market"] != "us":
        return quote | {"close_jpy": quote["close"], "fx_rate": None, "fx_date": None, "fx_source": None}
    try:
        fx = await usd_jpy_rate(ctx, today=today, memo=fx_memo)
    except ConnectorError as e:
        raise ConnectorError(
            f"米国株の価格（{quote['close']} {quote['currency']}）は取得できましたが、"
            f"USD/JPY を取得できなかったため円換算できませんでした（{e}）"
        ) from None
    return quote | {
        "close_jpy": quote["close"] * fx["rate"],
        "fx_rate": fx["rate"],
        "fx_date": fx["date"],
        "fx_source": fx["source"],
    }


async def stock_price(ctx: AppContext, code: str, *, today: date | None = None, fx_memo: dict | None = None) -> dict:
    """Previous close of a Japanese or US stock, in its own currency and in yen (cached per day)."""
    today = today or market_today()
    key = code.strip().upper()
    store = portfolio_store(ctx)
    cached = _cached(store, key, today, QUOTE_KEYS)
    if cached:
        return cached | {"cached": True}
    quote = await get_connectors(ctx)["stooq"].previous_close(code, today=today)
    quote = await _in_yen(ctx, quote, today=today, fx_memo=fx_memo)
    store.cache_price(key, today, quote)
    return quote | {"cached": False}


def _price(quote: dict) -> Price:
    return Price(
        value=quote["close_jpy"],
        date=quote["date"],
        source="stooq",
        market=quote["market"],
        symbol=quote["symbol"],
        local_currency=quote["currency"],
        local_value=quote["close"],
        fx_rate=quote["fx_rate"],
        fx_date=quote["fx_date"],
        fx_source=quote["fx_source"],
    )


async def refresh_stock_prices(ctx: AppContext) -> dict:
    store = portfolio_store(ctx)
    today = market_today()
    codes = sorted({h.code for h in store.load().holdings if h.kind in PRICED_BY_STOOQ and h.code})
    prices: dict[str, Price] = {}
    errors = []
    fx_memo: dict = {}
    # Fetch first, then apply under the file lock, so the lock is never held across network calls.
    # A failure for one code only removes that code from the update.
    for code in codes:
        try:
            prices[code] = _price(await stock_price(ctx, code, today=today, fx_memo=fx_memo))
        except ConnectorError as e:
            errors.append({"code": code, "error": str(e)})
        except (ValueError, KeyError, TypeError):
            logger.warning("could not build a price for %s", code, exc_info=True)
            errors.append({"code": code, "error": "取得した株価を取り込めませんでした（データの形式が不正です）"})
    updated = []
    with store.transaction() as portfolio:
        for holding in portfolio.holdings:
            price = prices.get(holding.code) if holding.kind in PRICED_BY_STOOQ else None
            if price and holding.apply_price(price.model_copy()):
                updated.append(
                    {
                        "code": holding.code,
                        "name": holding.name,
                        "market": price.market,
                        "symbol": price.symbol,
                        "currency": price.local_currency,
                        "close": price.local_value,
                        "close_jpy": price.value,
                        "date": price.date,
                        "fx_rate": price.fx_rate,
                        "fx_date": price.fx_date,
                    }
                )
    return {
        "updated": updated,
        "errors": errors,
        "note": "株価は Stooq の前日終値（日本株・米国株）です。米国株は USD/JPY で円換算しています。"
        "投資信託は Stooq の対象外のため、紐付け済みの基準価額は運用会社の公式 API・公式 CSV で別途更新します。"
        "未紐付け・未対応のファンドは取得元を設定するか、公式サイトの基準価額を手入力してください。",
    }
