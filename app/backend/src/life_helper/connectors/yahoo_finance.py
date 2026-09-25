"""Yahoo Finance connector: previous-day closes for Japanese and US stocks, ETFs and REITs, and USD/JPY.

Uses the chart API behind finance.yahoo.com (the one yfinance reads). It needs no API key but is not an official,
documented API, so it is meant for personal use and may change or throttle without notice.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from ..market.clock import market_today
from ..market.portfolio import Market, price_within_range
from .base import Connector, ConnectorError, ConnectorInfo

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
# Without a User-Agent the API answers 429 to every request.
USER_AGENT = "life-helper/1.0 (personal portfolio)"
USD_JPY_SYMBOL = "JPY=X"
# A month of daily bars is a few KB; anything far larger is not a chart answer.
MAX_CHART_BYTES = 500_000
# The daily bar of a session is only used once the session has ended and Yahoo had time to settle the close.
SETTLE_DELAY = timedelta(minutes=30)
PRICE_DECIMALS = 4

MARKETS: tuple[Market, ...] = ("jp", "us")
# Suffixes brokers and data vendors use to name the market (7203.T, 7203.JP, MSFT.US).
MARKET_SUFFIXES: dict[str, Market] = {"T": "jp", "JP": "jp", "US": "us"}
MARKET_CURRENCY: dict[Market, str] = {"jp": "JPY", "us": "USD"}
MARKET_TIMEZONE: dict[Market, str] = {"jp": "Asia/Tokyo", "us": "America/New_York"}
# Stocks and REITs are EQUITY, ETFs are ETF. Funds (MUTUALFUND) are priced from the fund library instead.
LISTED_INSTRUMENTS = ("EQUITY", "ETF")
RATE_LIMIT_MESSAGE = "Yahoo Finance の利用制限に達したため株価を取得できませんでした（時間をおいて再度お試しください）"


class SymbolNotFoundError(ConnectorError):
    """Yahoo Finance answered, but has no price for that symbol in that market, so the other one is worth trying."""


class RateLimitedError(ConnectorError):
    """Yahoo Finance is throttling requests; further calls in the same refresh would be refused as well."""


def _split_market(code: str) -> tuple[str, Market | None]:
    cleaned = code.strip().upper()
    head, _, suffix = cleaned.rpartition(".")
    if head and suffix in MARKET_SUFFIXES:
        return head, MARKET_SUFFIXES[suffix]
    return cleaned, None


def _fits(code: str, market: Market) -> bool:
    if market == "jp":
        # Tokyo Stock Exchange codes: 4 digits (7203) or alphanumeric (130A); 5 characters for some indices.
        return 4 <= len(code) <= 5 and any(c.isdigit() for c in code)
    return 1 <= len(code) <= 5 and code.isalpha()


def symbol_candidates(code: str) -> list[tuple[Market, str]]:
    """Yahoo symbols to try for ``code``, likeliest market first (``MSFT`` -> ``MSFT``, then ``MSFT.T``)."""
    cleaned, explicit = _split_market(code)
    if not cleaned.isascii() or not cleaned.isalnum() or not any(_fits(cleaned, m) for m in MARKETS):
        raise ConnectorError(f"証券コードの形式が正しくありません: {code}")
    # A digit means a Tokyo Stock Exchange code (7203, 130A); letters only mean a US ticker (MSFT).
    preferred = explicit or ("jp" if any(c.isdigit() for c in cleaned) else "us")
    markets = [preferred, *(m for m in MARKETS if m != preferred)]
    return [(m, f"{cleaned}.T" if m == "jp" else cleaned) for m in markets]


def to_yahoo_symbol(code: str) -> str:
    """Converts a security code (``7203``, ``130A``, ``7203.T``, ``MSFT``) to the Yahoo symbol tried first."""
    return symbol_candidates(code)[0][1]


def _number(raw: Any, label: str) -> float:
    # The same bound as a stored price: a quote that could not be rounded to yen would break the whole
    # portfolio screen, so it is refused here, before it is cached.
    if not price_within_range(raw):
        raise ConnectorError(f"Yahoo Finance から取得した{label}が不正です")
    # Yahoo sends single-precision artifacts (424.79998779296875 for 424.8); no quote has more than 4 decimals.
    return round(float(raw), PRICE_DECIMALS)


def _epoch(raw: Any) -> datetime | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        return None
    try:
        return datetime.fromtimestamp(raw, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _zone(meta: dict) -> ZoneInfo:
    name = meta.get("exchangeTimezoneName")
    if not isinstance(name, str) or not name:
        _bad_answer("取引所のタイムゾーン")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        _bad_answer("取引所のタイムゾーン")


def _bad_answer(detail: str = "") -> NoReturn:
    raise ConnectorError("Yahoo Finance から想定外の応答が返りました" + (f"（{detail}）" if detail else ""))


def _chart_result(symbol: str, response: httpx.Response) -> dict:
    """The ``chart.result[0]`` object, or the error that explains why there is none."""
    if response.status_code == 429:
        raise RateLimitedError(RATE_LIMIT_MESSAGE)
    try:
        data = response.json()
    except ValueError:
        data = None
    chart = data.get("chart") if isinstance(data, dict) else None
    error = chart.get("error") if isinstance(chart, dict) else None
    if response.status_code == 404 or (isinstance(error, dict) and error.get("code") == "Not Found"):
        raise SymbolNotFoundError(f"{symbol} の価格データが見つかりませんでした")
    if response.status_code != 200:
        raise ConnectorError(f"Yahoo Finance から株価を取得できませんでした（HTTP {response.status_code}）")
    results = chart.get("result") if isinstance(chart, dict) else None
    if error or not isinstance(results, list) or not results or not isinstance(results[0], dict):
        _bad_answer()
    return results[0]


def _settled_close(symbol: str, result: dict, *, today: date, now: datetime) -> dict:
    """The last settled daily close on or before ``today``, and until when that answer stays current.

    A session that is still running (or has just ended) has a bar whose close is only the latest trade, so bars
    from the current trading period are skipped until the period ended plus ``SETTLE_DELAY``. Outside trading
    hours Yahoo reports the next period, which no existing bar belongs to, so the last bar is then the answer.
    """
    meta = result.get("meta")
    if not isinstance(meta, dict):
        _bad_answer("meta")
    zone = _zone(meta)
    currency, instrument = meta.get("currency"), meta.get("instrumentType")
    if not isinstance(currency, str) or not isinstance(instrument, str):
        _bad_answer("通貨・銘柄の種類")
    # Without the trading period there is no telling a running session from a settled one; guessing would let an
    # intraday price pass as the close.
    regular = meta.get("currentTradingPeriod")
    regular = regular.get("regular") if isinstance(regular, dict) else None
    start = _epoch(regular.get("start")) if isinstance(regular, dict) else None
    end = _epoch(regular.get("end")) if isinstance(regular, dict) else None
    if start is None or end is None or start >= end:
        _bad_answer("取引時間")
    settles_at = end + SETTLE_DELAY
    running = now < settles_at
    try:
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        # Yahoo leaves the bars out entirely for a symbol that exists but has not traded in the range.
        raise SymbolNotFoundError(f"{symbol} の価格データが見つかりませんでした") from None
    if not isinstance(timestamps, list) or not isinstance(closes, list) or len(timestamps) != len(closes):
        _bad_answer("価格の系列")
    bars = []
    for raw_ts, close in zip(timestamps, closes, strict=True):
        at = _epoch(raw_ts)
        if at is None:
            _bad_answer("価格の日時")
        if close is None or (running and at >= start):
            continue
        day = at.astimezone(zone).date()
        if day <= today:
            bars.append((day, close))
    if not bars:
        raise SymbolNotFoundError(f"{symbol} の価格データが見つかりませんでした")
    day, close = bars[-1]
    return {
        "date": day.isoformat(),
        "close": close,
        "currency": currency,
        "timezone": meta.get("exchangeTimezoneName"),
        "instrument": instrument,
        # While a period is running, the answer changes once its close settles; the cache must not outlive that.
        "valid_until": settles_at.isoformat() if running else None,
    }


class YahooFinanceConnector(Connector):
    info = ConnectorInfo(
        name="yahoo_finance",
        label="Yahoo Finance（株価）",
        hosts=("query1.finance.yahoo.com",),
        secret_names=(),
        cost="無料（API キー不要。公式に公開された API ではないため個人利用の範囲で使う）",
    )
    min_interval_seconds = 1.0

    async def previous_close(self, code: str, *, today: date | None = None, now: datetime | None = None) -> dict:
        """Previous close of a Japanese or US stock. The market comes from the code, or both are tried in turn."""
        today = today or market_today()
        attempted: list[str] = []
        for market, symbol in symbol_candidates(code):
            attempted.append(symbol)
            try:
                bar = await self._settled_close(symbol, today=today, now=now)
                if (
                    bar["currency"] != MARKET_CURRENCY[market]
                    or bar["timezone"] != MARKET_TIMEZONE[market]
                    or bar["instrument"] not in LISTED_INSTRUMENTS
                ):
                    # Some other instrument answered under this symbol (a fund, an index, another exchange).
                    raise SymbolNotFoundError(f"{symbol} の価格データが見つかりませんでした")
            except SymbolNotFoundError:
                continue
            return {
                "code": code,
                "symbol": symbol,
                "market": market,
                "currency": MARKET_CURRENCY[market],
                "date": bar["date"],
                "close": _number(bar["close"], "株価"),
                "source": "yahoo_finance",
                "valid_until": bar["valid_until"],
            }
        raise SymbolNotFoundError(f"{' と '.join(attempted)} を照会しましたが、価格データが見つかりませんでした")

    async def usd_jpy(self, *, today: date | None = None, now: datetime | None = None) -> dict:
        """Previous close of USD/JPY, used to value US stocks in yen."""
        today = today or market_today()
        try:
            bar = await self._settled_close(USD_JPY_SYMBOL, today=today, now=now)
        except SymbolNotFoundError:
            raise ConnectorError("Yahoo Finance に USD/JPY の為替レートが見つかりませんでした") from None
        if bar["currency"] != "JPY" or bar["instrument"] != "CURRENCY":
            _bad_answer("USD/JPY の通貨")
        return {
            "pair": "USDJPY",
            "symbol": USD_JPY_SYMBOL,
            "date": bar["date"],
            "rate": _number(bar["close"], "為替レート"),
            "source": "yahoo_finance",
            "valid_until": bar["valid_until"],
        }

    async def _settled_close(self, symbol: str, *, today: date, now: datetime | None) -> dict:
        response = await self.get(
            CHART_URL + symbol,
            params={"range": "1mo", "interval": "1d"},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            max_bytes=MAX_CHART_BYTES,
        )
        return _settled_close(symbol, _chart_result(symbol, response), today=today, now=now or datetime.now(UTC))
