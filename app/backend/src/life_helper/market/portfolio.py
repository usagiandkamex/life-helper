"""Portfolio model and storage (money/portfolio.yaml), valuation, simulations and tax estimates."""

from __future__ import annotations

import json
import math
import random
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from ..automation.locks import FileLock
from ..knowledge.store import atomic_write
from .clock import market_today

Account = Literal["nisa_tsumitate", "nisa_growth", "tokutei", "ippan", "ideco"]
Kind = Literal["stock", "etf", "reit", "fund"]
# Fund NAV providers double as price sources, so the screen can show where a NAV came from.
# mufg_api is retired (its API refuses every request); it stays only so that saved portfolios still load.
FundProvider = Literal["toushin_lib", "rakuten_csv", "daiwa_csv", "manual", "mufg_api"]
# "stooq" and "mufg_api" are no longer fetched but stay valid, so prices saved before the switch still load.
PriceSource = Literal[
    "broker_csv",
    "yahoo_finance",
    "stooq",
    "nav_site",
    "manual",
    "toushin_lib",
    "rakuten_csv",
    "daiwa_csv",
    "mufg_api",
]
OFFICIAL_NAV_SOURCES = frozenset({"toushin_lib", "rakuten_csv", "daiwa_csv", "mufg_api"})
# A broker CSV is dated with the day it was imported, not with the 基準日 of the NAV inside it, which is up to a
# week older around Japanese holidays. An official NAV that much older than the import is still the newer one.
BROKER_NAV_GRACE_DAYS = 10
ISIN_SHAPE = re.compile(r"JP[0-9A-Z]{9}[0-9]")
Market = Literal["jp", "us"]
Currency = Literal["JPY", "USD"]
# Japanese funds quote the NAV per 10,000 units, but the unit is kept per fund because it can differ.
DEFAULT_PRICE_UNIT = 10_000

ACCOUNT_LABELS = {
    "nisa_tsumitate": "NISA つみたて投資枠",
    "nisa_growth": "NISA 成長投資枠",
    "tokutei": "特定口座",
    "ippan": "一般口座",
    "ideco": "iDeCo",
}
NISA_ACCOUNTS = ("nisa_tsumitate", "nisa_growth")
TAXABLE_ACCOUNTS = ("tokutei", "ippan")

# Far above any real holding, but small enough that quantity × price still rounds to yen with Decimal's precision.
# The paths that write a holding (手入力ツールと証券会社 CSV の取り込み) check these, so a value they store cannot
# make ``summarize()`` fail when it rounds to yen.
MAX_QUANTITY = 1_000_000_000_000
MAX_YEN = 1_000_000_000_000_000
MAX_PRICE = 10_000_000_000


def price_within_range(value: object) -> bool:
    """True when ``value`` can be stored as a price in yen (``Price.value``).

    Every path that writes a newly fetched or hand-entered price checks this, because ``summarize()`` rounds
    quantity × price to yen and a value outside this range makes that fail for the whole portfolio. A price that
    is already saved is not checked again, so a portfolio written before this check still loads and can be fixed.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and 0 < value <= MAX_PRICE


class Price(BaseModel):
    """A price in yen. US stocks also keep the local (USD) price and the rate used to convert it."""

    value: float = Field(description="円換算後の価格。投資信託は価格単位（通常 1 万口）あたりの基準価額")
    date: str = Field(description="価格の日付（YYYY-MM-DD）")
    source: PriceSource
    # Optional, so that portfolios saved before US stocks were supported still load.
    market: Market | None = Field(default=None, description="日本株（jp）か米国株（us）か")
    symbol: str | None = Field(default=None, description="取得に使ったシンボル（例: 7203.T、MSFT）")
    local_currency: Currency = Field(default="JPY", description="現地通貨（米国株は USD）")
    local_value: float | None = Field(default=None, description="現地通貨建ての価格（米国株は USD）")
    fx_rate: float | None = Field(default=None, description="円換算に使った USD/JPY")
    fx_date: str | None = Field(default=None, description="為替レートの日付（YYYY-MM-DD）")
    fx_source: PriceSource | None = Field(default=None, description="為替レートの取得元")
    source_url: str | None = Field(default=None, description="公式の出典 URL（投資信託の基準価額）")
    fetched_at: str | None = Field(default=None, description="価格を取得した日時（ISO 8601）")


class FundRef(BaseModel):
    """Links a holding to an official fund: chosen by the user, or set when exactly one fund has the same name."""

    provider: FundProvider = Field(description="基準価額のデータ提供元")
    fund_code: str = Field(default="", description="提供元のファンドコード")
    manager: str = Field(default="", description="運用会社")
    isin: str | None = Field(default=None, description="ISIN コード")
    association_code: str | None = Field(default=None, description="投資信託協会コード")
    price_unit: float = Field(default=DEFAULT_PRICE_UNIT, gt=0, description="基準価額の口数単位（通常は 1 万口）")
    source_url: str | None = Field(default=None, description="公式の出典 URL")

    @property
    def automatic(self) -> bool:
        """True when a connector can fetch the NAV; ``manual`` funds stay on the hand-entered price."""
        return self.provider != "manual" and bool(self.fund_code)


class Holding(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    account: Account
    kind: Kind
    code: str = Field(default="", description="証券コード（株・ETF・REIT）またはファンドコード")
    name: str
    quantity: float = Field(ge=0, description="株数、または投資信託の口数")
    cost_total: float = Field(ge=0, description="取得金額の合計（円、簿価）")
    price: Price | None = None
    valuation_yen: float | None = Field(default=None, description="評価額（円）。証券会社 CSV の値など")
    fund: FundRef | None = Field(default=None, description="投資信託の公式データとの紐付け")

    @model_validator(mode="after")
    def _retire_mufg_link(self) -> Holding:
        """Moves a link to the retired MUFG API over to the fund library, which knows the fund by its ISIN.

        A link without a valid ISIN is dropped, so the fund is matched again by name instead of failing forever.
        The price it already has is kept.
        """
        if self.fund is None or self.fund.provider != "mufg_api":
            return self
        codes = (str(c or "").strip().upper() for c in (self.fund.isin, self.fund.fund_code))
        isin = next((c for c in codes if ISIN_SHAPE.fullmatch(c)), None)
        self.fund = (
            FundRef(
                provider="toushin_lib",
                fund_code=isin,
                manager=self.fund.manager,
                isin=isin,
                association_code=self.fund.association_code,
                price_unit=self.fund.price_unit,
            )
            if isin
            else None
        )
        return self

    @property
    def price_unit(self) -> Decimal:
        """Units the NAV is quoted for. Only funds use it; stocks are always priced per share."""
        if self.kind != "fund":
            return Decimal(1)
        return Decimal(str(self.fund.price_unit)) if self.fund else Decimal(DEFAULT_PRICE_UNIT)

    def market_value(self) -> Decimal | None:
        """保有口数 ÷ 価格単位 × 基準価額（株は 株数 × 株価）。Decimal のまま返し、丸めは呼び出し側で行う。"""
        if self.valuation_yen is not None:
            return Decimal(str(self.valuation_yen))
        if self.price is None:
            return None
        # Multiply before dividing so the unit division never loses digits of the NAV.
        return Decimal(str(self.quantity)) * Decimal(str(self.price.value)) / self.price_unit

    def apply_price(self, price: Price) -> bool:
        """Uses ``price`` if it is at least as new as the current one. Returns True when applied."""
        if self.price is not None and self.price.date > price.date and not self._replaces_broker_nav(price):
            return False
        self.price = price
        self.valuation_yen = None
        return True

    def _replaces_broker_nav(self, price: Price) -> bool:
        """An official NAV replaces a broker CSV one dated up to ``BROKER_NAV_GRACE_DAYS`` later (the import date)."""
        current = self.price
        if self.kind != "fund" or current is None or current.source != "broker_csv":
            return False
        if price.source not in OFFICIAL_NAV_SOURCES:
            return False
        try:
            oldest = date.fromisoformat(current.date) - timedelta(days=BROKER_NAV_GRACE_DAYS)
            return date.fromisoformat(price.date) >= oldest
        except ValueError:
            return False


class Portfolio(BaseModel):
    holdings: list[Holding] = Field(default_factory=list)
    updated_at: str | None = None


class PortfolioStore:
    def __init__(self, knowledge_root: Path) -> None:
        self.path = knowledge_root / "money" / "portfolio.yaml"
        self.prices_dir = knowledge_root / "money" / "prices"
        self._lock = threading.Lock()

    def load(self) -> Portfolio:
        with self._lock:
            if not self.path.exists():
                return Portfolio()
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            return Portfolio.model_validate(raw)

    def save(self, portfolio: Portfolio) -> Portfolio:
        with self._lock:
            portfolio.updated_at = datetime.now(UTC).isoformat()
            header = "# 保有銘柄。ポートフォリオ画面（手入力・証券会社 CSV の取り込み）から更新します。\n"
            body = yaml.safe_dump(portfolio.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
            atomic_write(self.path, header + body)
            return portfolio

    @contextmanager
    def transaction(self) -> Iterator[Portfolio]:
        """Load-modify-save under a file lock shared with the scheduled job (same Azure Files volume)."""
        lock = FileLock(self.path.with_name(".portfolio.lock"), ttl_seconds=60)
        for _ in range(100):
            if lock.try_acquire():
                break
            time.sleep(0.05)
        else:
            raise TimeoutError("portfolio is being updated; try again")
        try:
            portfolio = self.load()
            yield portfolio
            self.save(portfolio)
        finally:
            lock.release()

    # -- price cache -------------------------------------------------------------------------------------

    def cached_price(self, code: str, on: date) -> dict | None:
        path = self.prices_dir / f"{on.isoformat()}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")).get(code)
        except (OSError, ValueError):
            return None

    def cache_price(self, code: str, on: date, data: dict) -> None:
        path = self.prices_dir / f"{on.isoformat()}.json"
        try:
            cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            cache = {}
        cache[code] = data
        atomic_write(path, json.dumps(cache, ensure_ascii=False))


def yen(value: Decimal) -> int:
    """Rounds a money amount to whole yen explicitly, instead of relying on float rounding."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def previous_business_day(today: date) -> date:
    """Previous weekday. Japanese holidays are not known here, so those days count as business days."""
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def is_stale(price: Price | None, *, today: date | None = None) -> bool:
    """A fund publishes the NAV of a day in the evening, so the previous business day is still current."""
    if price is None:
        return False
    return price.date < previous_business_day(today or market_today()).isoformat()


def summarize(portfolio: Portfolio, *, today: date | None = None) -> dict:
    today = today or market_today()
    by_account: dict[str, dict] = {}
    by_kind: dict[str, int] = {}
    items = []
    total_value = total_cost = 0
    for h in portfolio.holdings:
        exact = h.market_value()
        # Each holding is rounded to yen once and the totals add those, so the rows and the total always agree.
        value = yen(exact) if exact is not None else None
        cost = yen(Decimal(str(h.cost_total)))
        items.append(
            {
                "id": h.id,
                "account": h.account,
                "account_label": ACCOUNT_LABELS[h.account],
                "kind": h.kind,
                "code": h.code,
                "name": h.name,
                "quantity": h.quantity,
                "cost_total": cost,
                # Unrounded, so that an edit based on it keeps the fraction of yen a CSV import may have left.
                "cost_total_exact": h.cost_total,
                "value": value,
                "gain": (value - cost) if value is not None else None,
                "price": h.price.model_dump() if h.price else None,
                "fund": h.fund.model_dump() if h.fund else None,
                "price_unit": float(h.price_unit),
                "auto_nav": h.kind == "fund" and h.fund is not None and h.fund.automatic,
                "stale": is_stale(h.price, today=today),
            }
        )
        acc = by_account.setdefault(h.account, {"label": ACCOUNT_LABELS[h.account], "value": 0, "cost": 0})
        acc["cost"] += cost
        if value is not None:
            acc["value"] += value
            total_value += value
            by_kind[h.kind] = by_kind.get(h.kind, 0) + value
        total_cost += cost
    missing = [i["name"] for i in items if i["value"] is None]
    dates = sorted({i["price"]["date"] for i in items if i["price"]})
    return {
        "holdings": items,
        "accounts": by_account,
        "allocation": ({k: round(v / total_value, 4) for k, v in by_kind.items()} if total_value else {}),
        "total_value": total_value,
        "total_cost": total_cost,
        "total_gain": total_value - total_cost,
        "oldest_price_date": dates[0] if dates else None,
        "missing_prices": missing,
        "stale_prices": [i["name"] for i in items if i["stale"]],
        "manual_funds": [
            {"id": i["id"], "name": i["name"]} for i in items if i["kind"] == "fund" and not i["auto_nav"]
        ],
        "note": "評価額は価格の日付時点の目安です。正確な評価額は証券会社の画面で確認してください。",
    }


class InvestmentSimParams(BaseModel):
    initial: float = Field(default=0, ge=0, description="現在の元本（円）")
    monthly_contribution: float = Field(default=0, ge=0, description="毎月の積立額（円）")
    years: int = Field(ge=1, le=60, description="運用年数")
    expected_return: float = Field(default=0.04, ge=-0.2, le=0.3, description="想定利回り（年率、信託報酬控除前）")
    volatility: float = Field(default=0.15, ge=0, le=0.6, description="リスク（年率の標準偏差）")
    expense_ratio: float = Field(default=0.001, ge=0, le=0.03, description="信託報酬（年率）")
    tax_rate: float = Field(default=0.20315, ge=0, le=0.6, description="課税口座の税率")
    simulations: int = Field(default=1000, ge=100, le=5000)
    seed: int | None = Field(default=42, description="乱数の種（同じ条件なら同じ結果になる）")


def simulate_investment(p: InvestmentSimParams) -> dict:
    months = p.years * 12
    net_annual = p.expected_return - p.expense_ratio
    monthly_rate = (1 + net_annual) ** (1 / 12) - 1
    principal = p.initial + p.monthly_contribution * months

    deterministic, yearly = p.initial, []
    for m in range(1, months + 1):
        deterministic = deterministic * (1 + monthly_rate) + p.monthly_contribution
        if m % 12 == 0:
            yearly.append(
                {
                    "year": m // 12,
                    "principal": round(p.initial + p.monthly_contribution * m),
                    "value": round(deterministic),
                }
            )

    rng = random.Random(p.seed)  # noqa: S311 - simulation, not cryptography
    mu = math.log(1 + net_annual) / 12 - (p.volatility**2) / 24
    sigma = p.volatility / math.sqrt(12)
    finals = []
    for _ in range(p.simulations):
        value = p.initial
        for _m in range(months):
            value = value * math.exp(rng.gauss(mu, sigma)) + p.monthly_contribution
        finals.append(value)
    finals.sort()

    def pct(q: float) -> int:
        return round(finals[min(len(finals) - 1, int(q * len(finals)))])

    gain = max(0.0, deterministic - principal)
    return {
        "principal": round(principal),
        "expected_value": round(deterministic),
        "percentiles": {"p10": pct(0.10), "p50": pct(0.50), "p90": pct(0.90)},
        "yearly": yearly,
        "after_tax": {
            "nisa": round(deterministic),
            "taxable": round(deterministic - gain * p.tax_rate),
            "tax_saved_by_nisa": round(gain * p.tax_rate),
        },
        "assumptions": [
            f"想定利回り {p.expected_return:.1%}、信託報酬 {p.expense_ratio:.2%}、リスク {p.volatility:.0%}（年率）",
            f"モンテカルロ法 {p.simulations} 回の 10% / 50% / 90% 点を示しています。",
            "課税口座は運用終了時に一括で売却した場合の税引後額です。",
        ],
        "chart": {"type": "investment", "x": "year", "series": ["principal", "value"]},
        "disclaimer": "結果は目安であり、将来の運用成果を保証するものではありません。",
    }


class GainItem(BaseModel):
    label: str = Field(default="", description="銘柄名など")
    kind: Literal["sale", "dividend"] = "sale"
    amount: float = Field(description="売却益（損失はマイナス）または配当金額（税引前、円）")


class CapitalGainsParams(BaseModel):
    items: list[GainItem]
    tax_rate: float = Field(default=0.20315, ge=0, le=0.6)


def estimate_capital_gains_tax(p: CapitalGainsParams) -> dict:
    sales = sum(i.amount for i in p.items if i.kind == "sale")
    dividends = sum(i.amount for i in p.items if i.kind == "dividend")
    # Losses on sales offset dividends received in the same taxable account (損益通算).
    net = sales + dividends
    taxable = max(0.0, net)
    return {
        "sales_net": round(sales),
        "dividends": round(dividends),
        "taxable_amount": round(taxable),
        "estimated_tax": math.floor(taxable * p.tax_rate),
        "carryforward_loss": round(-net) if net < 0 else 0,
        "notes": [
            "特定口座（源泉徴収あり）の場合、税金は証券会社が計算して納付します。",
            "損失は確定申告をすると翌年以降 3 年間繰り越せます。NISA 口座の損益は通算できません。",
        ],
        "disclaimer": "結果は目安です。正確な税額は証券会社の年間取引報告書や税理士に確認してください。",
    }
