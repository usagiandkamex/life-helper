"""HTTP API for the portfolio screen."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from ..auth import CurrentUser, require_user
from ..context import AppContext, get_ctx
from ..tools.portfolio_tools import UpdateHoldingParams, apply_holding_update
from .broker_csv import BrokerCsvError, list_brokers, load_mapping, parse_broker_csv
from .portfolio import InvestmentSimParams, NisaUsage, simulate_investment, summarize
from .service import nisa_status, portfolio_store, refresh_stock_prices

router = APIRouter(prefix="/api/portfolio")


class NisaUsageBody(BaseModel):
    year: int = Field(ge=2024, le=2100)
    tsumitate: float = Field(ge=0)
    growth: float = Field(ge=0)


def _view(ctx: AppContext) -> dict:
    portfolio = portfolio_store(ctx).load()
    year = date.today().year
    return summarize(portfolio) | {
        "nisa": nisa_status(ctx, portfolio, year),
        "brokers": list_brokers(ctx.settings.broker_csv_dir),
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
    # The CSV is a full snapshot from the broker, so it replaces the holdings (NISA usage input is kept).
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.holdings = holdings
    return _view(ctx) | {"imported": len(holdings)}


@router.post("/refresh-prices")
async def refresh_prices(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> dict:
    result = await refresh_stock_prices(ctx)
    return _view(ctx) | {"refresh": result}


@router.put("/nisa-usage")
def set_nisa_usage(
    body: NisaUsageBody, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> dict:
    with portfolio_store(ctx).transaction() as portfolio:
        portfolio.nisa_annual_used[str(body.year)] = NisaUsage(tsumitate=body.tsumitate, growth=body.growth)
    return _view(ctx)


@router.post("/simulate")
def simulate(body: InvestmentSimParams, user: CurrentUser = Depends(require_user)) -> dict:
    return simulate_investment(body)
