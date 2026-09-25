"""HTTP API for the portfolio screen."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from ..auth import CurrentUser, require_user
from ..connectors.fund_nav import MAX_NAV
from ..context import AppContext, get_ctx
from ..tools.portfolio_tools import UpdateHoldingParams, apply_holding_update
from .broker_csv import BrokerCsvError, list_brokers, load_mapping, parse_broker_csv
from .funds import fund_providers, keep_fund_links, link_fund, refresh_fund_navs, suggest_funds
from .portfolio import DEFAULT_PRICE_UNIT, InvestmentSimParams, simulate_investment, summarize
from .service import portfolio_store, refresh_stock_prices

router = APIRouter(prefix="/api/portfolio")


class FundLinkParams(BaseModel):
    id: str = Field(description="保有銘柄の ID")
    provider: str = Field(description="データ提供元（manual は手入力のまま）")
    fund_code: str = Field(default="", max_length=32, description="提供元のファンドコード")
    price_unit: float = Field(
        default=DEFAULT_PRICE_UNIT, gt=0, le=1_000_000, description="手入力のときの価格単位（通常は 1 万口）"
    )
    nav: float | None = Field(default=None, gt=0, le=MAX_NAV, description="手入力する基準価額")
    price_date: date | None = Field(default=None, description="手入力する基準価額の基準日")


def _view(ctx: AppContext) -> dict:
    portfolio = portfolio_store(ctx).load()
    return summarize(portfolio) | {
        "brokers": list_brokers(ctx.settings.broker_csv_dir),
        "fund_providers": fund_providers(ctx),
        "updated_at": portfolio.updated_at,
    }


@router.get("")
def get_portfolio(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> dict:
    return _view(ctx)


@router.post("/holdings")
def change_holding(
    body: UpdateHoldingParams, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    result = apply_holding_update(ctx, body)
    if "error" in result:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result["error"])
    return _view(ctx)


@router.post("/import")
async def import_csv(
    broker: str = Form(...),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_user),
    ctx: AppContext = Depends(get_ctx),
) -> dict:
    data = await file.read(ctx.settings.upload_max_bytes + 1)
    if len(data) > ctx.settings.upload_max_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file is too large")
    try:
        holdings = parse_broker_csv(data, load_mapping(ctx.settings.broker_csv_dir, broker))
    except BrokerCsvError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    # The CSV is a full snapshot from the broker, so it replaces the holdings.
    with portfolio_store(ctx).transaction() as portfolio:
        keep_fund_links(portfolio.holdings, holdings)
        portfolio.holdings = holdings
    return _view(ctx) | {"imported": len(holdings)}


@router.post("/refresh-prices")
async def refresh_prices(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> dict:
    # Two independent sources: Yahoo Finance for what trades on an exchange, the fund library (投資信託協会) and the
    # managers' CSVs for 基準価額.
    result = await refresh_stock_prices(ctx)
    funds = await refresh_fund_navs(ctx)
    return _view(ctx) | {"refresh": result, "refresh_funds": funds}


@router.get("/fund-candidates")
async def fund_candidates(
    name: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    if not name.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "ファンド名を指定してください")
    return await suggest_funds(ctx, name[:200])


@router.post("/fund-link")
async def set_fund_link(
    body: FundLinkParams, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    result = await link_fund(ctx, body.id, body.provider, body.fund_code, body.price_unit, body.nav, body.price_date)
    if "error" in result:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result["error"])
    return _view(ctx) | {"link": result}


@router.post("/simulate")
def simulate(body: InvestmentSimParams, user: CurrentUser = Depends(require_user)) -> dict:
    return simulate_investment(body)
