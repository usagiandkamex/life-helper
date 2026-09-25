"""Copilot tools for the portfolio, funds and stocks."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Literal

from copilot import define_tool
from pydantic import BaseModel, Field

from ..connectors.base import ConnectorError
from ..market.clock import market_today
from ..market.funds import fund_connectors, refresh_fund_navs
from ..market.portfolio import (
    Account,
    CapitalGainsParams,
    Holding,
    InvestmentSimParams,
    Kind,
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
    provider: str = Field(description="データ提供元（例: mufg_api、rakuten_csv、daiwa_csv）")
    fund_code: str = Field(
        max_length=32, description="運用会社のファンドコード・投資信託協会コード・ISIN（例: 0331418A）"
    )


class UpdateHoldingParams(BaseModel):
    action: Literal["add", "update", "delete"]
    id: str | None = Field(default=None, description="update / delete の対象 ID（get_portfolio の結果にある id）")
    account: Account | None = None
    kind: Kind | None = None
    code: str | None = None
    name: str | None = None
    quantity: float | None = Field(default=None, ge=0)
    cost_total: float | None = Field(default=None, ge=0, description="取得金額の合計（円）")
    price: float | None = Field(default=None, gt=0, description="株価、または投資信託の 1 万口あたり基準価額")
    price_date: date | None = None
    price_source: Literal["nav_site", "manual"] = "manual"


def apply_holding_update(ctx: AppContext, p: UpdateHoldingParams) -> dict:
    price_date = (p.price_date or market_today()).isoformat()
    with portfolio_store(ctx).transaction() as portfolio:
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
        else:
            name, kind, code = target.name, target.kind, target.code
            for field in ("account", "kind", "code", "name", "quantity", "cost_total"):
                value = getattr(p, field)
                if value is not None:
                    setattr(target, field, value)
            renamed = p.name is not None and p.name != name
            retyped = p.kind is not None and p.kind != kind
            recoded = p.code is not None and p.code != code
            # A renamed or re-coded holding may be another fund, so its confirmed NAV source has to be picked again.
            if target.kind != "fund" or renamed or recoded:
                target.fund = None
            # The old price (and the valuation computed from it) belongs to the previous instrument, so it must
            # not value the new one. A price given in the same update replaces it below.
            if renamed or retyped or recoded:
                target.price, target.valuation_yen = None, None
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
        description="日本株・米国株・ETF・REIT の前日終値を Stooq から取得する"
        "（米国株は USD/JPY で円換算。当日取得済みならキャッシュを使う）。",
    )
    async def get_stock_price(params: StockPriceParams) -> dict:
        try:
            return await stock_price(ctx, params.code)
        except ConnectorError as e:
            return {"error": str(e)}

    @define_tool(
        name="refresh_stock_prices",
        description="保有している日本株・米国株・ETF・REIT の価格を Stooq の前日終値で更新する"
        "（証券会社 CSV より新しい場合のみ。米国株は USD/JPY で円換算。投資信託は対象外）。",
    )
    async def refresh_prices(params: EmptyParams) -> dict:
        return await refresh_stock_prices(ctx)

    @define_tool(
        name="get_fund_nav",
        description="投資信託の基準価額を運用会社の公式 API・公式 CSV から取得する"
        "（データ提供元とファンドコードを指定。株価の Stooq とは別）。",
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
        description="保有している投資信託の基準価額を、紐付けた運用会社の公式 API・公式 CSV から更新して"
        "評価額を計算し直す（紐付けていないファンドは手入力のまま）。",
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
        description="保有銘柄を追加・更新・削除する。利用者が明確に頼んだときだけ使う。",
    )
    def update_holding(params: UpdateHoldingParams) -> dict:
        return apply_holding_update(ctx, params)

    return [
        ToolSpec(get_portfolio),
        ToolSpec(get_stock_price, connector="stooq"),
        ToolSpec(refresh_prices, writes=True, connector="stooq"),
        ToolSpec(get_fund_nav, connector=fund_provider_names),
        ToolSpec(refresh_navs, writes=True, connector=fund_provider_names),
        ToolSpec(simulate_investment_tool),
        ToolSpec(estimate_tax),
        ToolSpec(update_holding, writes=True),
    ]
