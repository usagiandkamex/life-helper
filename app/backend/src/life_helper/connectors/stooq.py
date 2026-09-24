"""Stooq connector: previous-day closes for Japanese and US stocks, ETFs and REITs (free; API key required)."""

from __future__ import annotations

import csv
import io
import math
from datetime import date, timedelta

from ..market.portfolio import Market
from .base import Connector, ConnectorError, ConnectorInfo

STOOQ_URL = "https://stooq.com/q/d/l/"
USD_JPY_SYMBOL = "usdjpy"

MARKETS: tuple[Market, ...] = ("jp", "us")
# Suffixes brokers and data vendors use to name the market (7203.T, 7203.JP, MSFT.US).
MARKET_SUFFIXES: dict[str, Market] = {"T": "jp", "JP": "jp", "US": "us"}


class SymbolNotFoundError(ConnectorError):
    """Stooq answered, but has no price for that symbol, so the other market is worth trying."""


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
    """Stooq symbols to try for ``code``, likeliest market first (``MSFT`` -> ``msft.us``, then ``msft.jp``)."""
    cleaned, explicit = _split_market(code)
    if not cleaned.isascii() or not cleaned.isalnum() or not any(_fits(cleaned, m) for m in MARKETS):
        raise ConnectorError(f"証券コードの形式が正しくありません: {code}")
    # A digit means a Tokyo Stock Exchange code (7203, 130A); letters only mean a US ticker (MSFT).
    preferred = explicit or ("jp" if any(c.isdigit() for c in cleaned) else "us")
    markets = [preferred, *(m for m in MARKETS if m != preferred)]
    return [(m, f"{cleaned.lower()}.{m}") for m in markets]


def to_stooq_symbol(code: str) -> str:
    """Converts a security code (``7203``, ``130A``, ``7203.T``, ``MSFT``) to the Stooq symbol tried first."""
    return symbol_candidates(code)[0][1]


def _number(raw: str | None, label: str) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ConnectorError(f"Stooq から取得した{label}が不正です") from None
    if not math.isfinite(value) or value <= 0:
        raise ConnectorError(f"Stooq から取得した{label}が不正です")
    return value


def _day(raw: str | None, label: str) -> str:
    try:
        return date.fromisoformat((raw or "").strip()).isoformat()
    except ValueError:
        raise ConnectorError(f"Stooq から取得した{label}の日付が不正です") from None


def _body_error(symbol: str, body: str) -> ConnectorError:
    """Classifies a non-CSV answer: Stooq sends plain text for quota or unknown symbols, HTML for key problems."""
    text = body.lower()
    if "exceeded" in text or "hits limit" in text:
        return ConnectorError("Stooq の API キーの利用上限に達したため株価を取得できませんでした")
    if "apikey" in text or "api key" in text or "captcha" in text:
        return ConnectorError("Stooq の API キーが無効なため株価を取得できませんでした")
    if not text or "no data" in text:
        return SymbolNotFoundError(f"{symbol} の価格データが見つかりませんでした")
    return ConnectorError("Stooq から想定外の応答が返りました（API キーまたは証券コードを確認してください）")


class StooqConnector(Connector):
    info = ConnectorInfo(
        name="stooq",
        label="Stooq（株価）",
        hosts=("stooq.com",),
        secret_names=("stooq_api_key",),
        cost="無料（API キーはブラウザで CAPTCHA を解いて取得）",
    )
    min_interval_seconds = 1.0

    async def previous_close(self, code: str, *, today: date | None = None) -> dict:
        """Previous close of a Japanese or US stock. The market comes from the code, or both are tried in turn."""
        today = today or date.today()
        attempted: list[str] = []
        for market, symbol in symbol_candidates(code):
            attempted.append(symbol)
            try:
                row = await self._last_row(symbol, today)
            except SymbolNotFoundError:
                continue
            return {
                "code": code,
                "symbol": symbol,
                "market": market,
                "currency": "JPY" if market == "jp" else "USD",
                "date": _day(row.get("Date"), "株価"),
                "close": _number(row.get("Close"), "株価"),
                "source": "stooq",
            }
        raise SymbolNotFoundError(f"{' と '.join(attempted)} を照会しましたが、価格データが見つかりませんでした")

    async def usd_jpy(self, *, today: date | None = None) -> dict:
        """Previous close of USD/JPY, used to value US stocks in yen."""
        today = today or date.today()
        try:
            row = await self._last_row(USD_JPY_SYMBOL, today)
        except SymbolNotFoundError:
            raise ConnectorError("Stooq に USD/JPY の為替レートが見つかりませんでした") from None
        return {
            "pair": "USDJPY",
            "symbol": USD_JPY_SYMBOL,
            "date": _day(row.get("Date"), "為替レート"),
            "rate": _number(row.get("Close"), "為替レート"),
            "source": "stooq",
        }

    async def _last_row(self, symbol: str, today: date) -> dict[str, str]:
        params = {
            "s": symbol,
            "i": "d",
            "d1": (today - timedelta(days=14)).strftime("%Y%m%d"),
            "d2": today.strftime("%Y%m%d"),
            "apikey": self.secret("stooq_api_key"),
        }
        response = await self.get(STOOQ_URL, params=params)
        body = response.text.strip()
        if response.status_code != 200:
            raise ConnectorError(f"Stooq から株価を取得できませんでした（HTTP {response.status_code}）")
        if not body.lower().startswith("date,"):
            # Stooq returns an HTML page instead of CSV when the key is missing or invalid.
            raise _body_error(symbol, body)
        rows = [r for r in csv.DictReader(io.StringIO(body)) if r.get("Close") not in (None, "", "N/D")]
        if not rows:
            raise SymbolNotFoundError(f"{symbol} の価格データが見つかりませんでした")
        return rows[-1]
