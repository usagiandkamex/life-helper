"""Copilot tools for the portfolio, funds and stocks."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from copilot import define_tool
from pydantic import BaseModel, Field

from ..connectors.base import ConnectorError
from ..market.clock import market_today
from ..market.funds import fund_connectors, refresh_fund_navs
from ..market.portfolio import (
    MAX_PRICE,
    MAX_QUANTITY,
    MAX_YEN,
    Account,
    CapitalGainsParams,
    Holding,
    InvestmentSimParams,
    Kind,
    Portfolio,
    Price,
    estimate_capital_gains_tax,
    simulate_investment,
    summarize,
)
from ..market.service import portfolio_store, refresh_stock_prices, stock_price
from .registry import ToolSpec

if TYPE_CHECKING:
    from ..context import AppContext


class EmptyParams(BaseModel):
    pass


class StockPriceParams(BaseModel):
    code: str = Field(description="証券コードまたはティッカー（例: 7203、1306、MSFT）")


class FundNavParams(BaseModel):
    provider: str = Field(
        default="toushin_lib", description="データ提供元（toushin_lib＝投資信託協会、rakuten_csv、daiwa_csv）"
    )
    fund_code: str = Field(
        max_length=32,
        description="toushin_lib は ISIN コード（例: JP90C000GKC6）、rakuten_csv・daiwa_csv は各社のファンドコード",
    )


CONFLICT_MESSAGE = "ほかの操作で数量または取得額が変わっています。最新の値を確認して、入力し直してください。"


class UpdateHoldingParams(BaseModel):
    action: Literal["add", "update", "delete"]
    id: str | None = Field(default=None, description="update / delete の対象 ID（get_portfolio の結果にある id）")
    account: Account | None = None
    kind: Kind | None = None
    code: str | None = None
    name: str | None = None
    quantity: float | None = Field(
        default=None,
        ge=0,
        le=MAX_QUANTITY,
        allow_inf_nan=False,
        description="株数、または投資信託の口数の合計。update で買い増しを反映するときは、買い増し分を足した後の合計",
    )
    cost_total: float | None = Field(
        default=None,
        ge=0,
        le=MAX_YEN,
        allow_inf_nan=False,
        description="取得金額の合計（円）。update で買い増しを反映するときは、購入金額を足した後の合計",
    )
    # The expected values are only compared with what is stored, never saved, so the storage limits do not apply
    # to them: a holding saved before those limits existed has to stay editable to a value within them.
    expected_quantity: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        description="update の前提にした現在の数量（省略可）。ほかの更新で変わっていたら更新しない",
    )
    expected_cost_total: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        description="update の前提にした現在の取得金額の合計（省略可）。ほかの更新で変わっていたら更新しない",
    )
    price: float | None = Field(
        default=None,
        gt=0,
        le=MAX_PRICE,
        allow_inf_nan=False,
        description="株価、または投資信託の 1 万口あたり基準価額",
    )
    price_date: date | None = None
    price_source: Literal["nav_site", "manual"] = "manual"


def _conflict(target: Holding, p: UpdateHoldingParams) -> dict | None:
    """Rejects totals computed from a quantity or cost that another update changed in the meantime.

    A value that already equals the requested one is not a conflict, so sending the same update twice is harmless.
    """
    for field in ("quantity", "cost_total"):
        expected = getattr(p, f"expected_{field}")
        current = getattr(target, field)
        if expected is not None and current != expected and current != getattr(p, field):
            return {"error": CONFLICT_MESSAGE, "conflict": True}
    return None


def _rescaled_valuation(target: Holding, quantity: float) -> tuple[bool, float | None]:
    """The broker CSV valuation for a new quantity, as ``(ok, valuation)``.

    The CSV valuation is in yen even when the CSV price is not (US stocks are quoted in USD there, and a fund may
    use another price unit), so it is scaled by the quantity rather than recomputed from that price.
    """
    valuation = target.valuation_yen
    if valuation is None or target.quantity <= 0:
        return True, None
    scaled = Decimal(str(valuation)) * Decimal(str(quantity)) / Decimal(str(target.quantity))
    if not scaled.is_finite() or scaled > MAX_YEN:
        return False, None
    return True, float(scaled)


class _Rejected(Exception):
    """Leaves the transaction without saving, so a refused update does not even touch ``updated_at``."""

    def __init__(self, result: dict) -> None:
        super().__init__(result.get("error"))
        self.result = result


def apply_holding_update(ctx: AppContext, p: UpdateHoldingParams) -> dict:
    try:
        with portfolio_store(ctx).transaction() as portfolio:
            result = _apply_holding_update(portfolio, p)
            if "error" in result:
                raise _Rejected(result)
    except _Rejected as rejected:
        return rejected.result
    return result


def _apply_holding_update(portfolio: Portfolio, p: UpdateHoldingParams) -> dict:
    """Changes ``portfolio`` in place. Every error is returned before anything is changed."""
    price_date = (p.price_date or market_today()).isoformat()
    if p.action == "add":
        if not (p.account and p.kind and p.name and p.quantity is not None and p.cost_total is not None):
            return {"error": "add には account・kind・name・quantity・cost_total が必要です"}
        holding = Holding(
            account=p.account,
            kind=p.kind,
            code=p.code or "",
            name=p.name,
            quantity=p.quantity,
            cost_total=p.cost_total,
        )
        if p.price:
            holding.apply_price(Price(value=p.price, date=price_date, source=p.price_source))
        portfolio.holdings.append(holding)
        return {"ok": True, "id": holding.id}
    target = next((h for h in portfolio.holdings if h.id == p.id), None)
    if target is None:
        return {"error": f"id {p.id} の銘柄が見つかりません"}
    if p.action == "delete":
        portfolio.holdings.remove(target)
        return {"ok": True, "id": target.id}
    conflict = _conflict(target, p)
    if conflict:
        return conflict
    renamed = p.name is not None and p.name != target.name
    retyped = p.kind is not None and p.kind != target.kind
    recoded = p.code is not None and p.code != target.code
    requantified = p.quantity is not None and p.quantity != target.quantity
    # Another instrument drops the old valuation below, so only a valuation that stays is scaled.
    rescale = requantified and target.valuation_yen is not None and not (renamed or retyped or recoded)
    valuation = target.valuation_yen
    if rescale:
        ok, valuation = _rescaled_valuation(target, p.quantity)
        if not ok:
            return {"error": "評価額が大きくなりすぎるため、この数量には更新できません"}
    for field in ("account", "kind", "code", "name", "quantity", "cost_total"):
        value = getattr(p, field)
        if value is not None:
            setattr(target, field, value)
    # A renamed or re-coded holding may be another fund, so its confirmed NAV source has to be picked again.
    if target.kind != "fund" or renamed or recoded:
        target.fund = None
    # The old price (and the valuation computed from it) belongs to the previous instrument, so it must
    # not value the new one. A price given in the same update replaces it below.
    if renamed or retyped or recoded:
        target.price, target.valuation_yen = None, None
    elif rescale:
        target.valuation_yen = valuation
        # Without a CSV valuation to scale, the CSV price (possibly USD or per another unit) cannot value it.
        if valuation is None and target.price is not None and target.price.source == "broker_csv":
            target.price = None
    if p.price:
        target.apply_price(Price(value=p.price, date=price_date, source=p.price_source))
    return {"ok": True, "id": target.id}


def build_tools(ctx: AppContext) -> list[ToolSpec]:
    fund_provider_names = tuple(fund_connectors(ctx))

    @define_tool(
        name="get_portfolio",
        description="保有銘柄、口座区分ごとの評価額・含み損益・資産配分、価格の日付と出どころを返す。",
        skip_permission=True,
    )
    def get_portfolio(params: EmptyParams) -> dict:
        return summarize(portfolio_store(ctx).load())

    @define_tool(
        name="get_stock_price",
        description="日本株・米国株・ETF・REIT の前日終値を Yahoo Finance から取得する"
        "（米国株は USD/JPY で円換算。当日取得済みならキャッシュを使う）。",
    )
    async def get_stock_price(params: StockPriceParams) -> dict:
        try:
            return await stock_price(ctx, params.code)
        except ConnectorError as e:
            return {"error": str(e)}

    @define_tool(
        name="refresh_stock_prices",
        description="保有している日本株・米国株・ETF・REIT の価格を Yahoo Finance の前日終値で更新する"
        "（証券会社 CSV より新しい場合のみ。米国株は USD/JPY で円換算。投資信託は対象外）。",
    )
    async def refresh_prices(params: EmptyParams) -> dict:
        return await refresh_stock_prices(ctx)

    @define_tool(
        name="get_fund_nav",
        description="投資信託の基準価額を投資信託協会の投信総合検索ライブラリー（ISIN で指定）、"
        "または運用会社の公式 CSV から取得する（株価の Yahoo Finance とは別）。",
    )
    async def get_fund_nav(params: FundNavParams) -> dict:
        connector = fund_connectors(ctx).get(params.provider)
        if connector is None:
            return {"error": f"対応していないデータ提供元です: {params.provider}"}
        try:
            return await connector.fund_nav(params.fund_code, today=market_today())
        except ConnectorError as e:
            return {"error": str(e)}

    @define_tool(
        name="refresh_fund_navs",
        description="保有している投資信託の基準価額を更新して評価額を計算し直す。取得元が未設定のファンドは、"
        "名前が一致する公式ファンドが 1 つだけなら自動で紐付ける（紐付けられないファンドは手入力のまま）。",
    )
    async def refresh_navs(params: EmptyParams) -> dict:
        return await refresh_fund_navs(ctx)

    @define_tool(
        name="simulate_investment",
        description="積立投資の将来シミュレーション（期待値とモンテカルロ法の幅、NISA と課税口座の税引後比較）。",
        skip_permission=True,
    )
    def simulate_investment_tool(params: InvestmentSimParams) -> dict:
        return simulate_investment(params)

    @define_tool(
        name="estimate_capital_gains_tax",
        description="課税口座の売却益・配当の税額の目安を計算する（損益通算を含む）。",
        skip_permission=True,
    )
    def estimate_tax(params: CapitalGainsParams) -> dict:
        return estimate_capital_gains_tax(params)

    @define_tool(
        name="update_holding",
        description="保有銘柄を追加・更新・削除する。利用者が明確に頼んだときだけ使う。"
        "積立・買い増しを反映するときは、quantity・cost_total に購入分を足した後の合計を渡す。",
    )
    def update_holding(params: UpdateHoldingParams) -> dict:
        return apply_holding_update(ctx, params)

    return [
        ToolSpec(get_portfolio),
        ToolSpec(get_stock_price, connector="yahoo_finance"),
        ToolSpec(refresh_prices, writes=True, connector="yahoo_finance"),
        ToolSpec(get_fund_nav, connector=fund_provider_names),
        ToolSpec(refresh_navs, writes=True, connector=fund_provider_names),
        ToolSpec(simulate_investment_tool),
        ToolSpec(estimate_tax),
        ToolSpec(update_holding, writes=True),
    ]
