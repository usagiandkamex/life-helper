"""Stooq connector: previous-day closing prices for Japanese stocks, ETFs and REITs (free; API key required)."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from .base import Connector, ConnectorError, ConnectorInfo

STOOQ_URL = "https://stooq.com/q/d/l/"


def to_stooq_symbol(code: str) -> str:
    """Converts a Tokyo Stock Exchange code (e.g. ``7203`` or ``130A``) to Stooq's symbol (``7203.jp``)."""
    cleaned = code.strip().upper().removesuffix(".T").removesuffix(".JP")
    if not (4 <= len(cleaned) <= 5) or not cleaned.isalnum():
        raise ConnectorError(f"証券コードの形式が正しくありません: {code}")
    return f"{cleaned.lower()}.jp"


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
        symbol = to_stooq_symbol(code)
        today = today or date.today()
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
            # Stooq returns an HTML page instead of CSV when the key is missing/invalid or the symbol is unknown.
            raise ConnectorError("Stooq から CSV を取得できませんでした（API キーまたは証券コードを確認してください）")
        rows = [r for r in csv.DictReader(io.StringIO(body)) if r.get("Close") not in (None, "", "N/D")]
        if not rows:
            raise ConnectorError(f"{code} の株価データが見つかりませんでした")
        last = rows[-1]
        return {"code": code, "symbol": symbol, "date": last["Date"], "close": float(last["Close"]), "source": "stooq"}
