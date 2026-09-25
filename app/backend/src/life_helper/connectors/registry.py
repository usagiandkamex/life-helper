"""Connector instances, the connector tools exposed to Copilot, and the connector status API."""

from __future__ import annotations

from datetime import date

from copilot import define_tool
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import CurrentUser, require_user
from ..context import AppContext, get_ctx
from ..tools.registry import ToolSpec
from .base import Connector, ConnectorError
from .browser import BrowserConnector
from .fund_nav import DaiwaFundCsvConnector, RakutenFundCsvConnector, ToushinLibConnector
from .rakuten_travel import RakutenTravelConnector
from .yahoo_finance import YahooFinanceConnector


def get_connectors(ctx: AppContext) -> dict[str, Connector]:
    cached = ctx.extras.get("connectors")
    if cached is not None:
        return cached
    s = ctx.settings
    state = s.app_state_dir / "connectors.json"
    secrets = {
        "rakuten_application_id": s.rakuten_application_id.get_secret_value(),
        "rakuten_access_key": s.rakuten_access_key.get_secret_value(),
    }
    transport = ctx.extras.get("http_transport")
    connectors: dict[str, Connector] = {
        # No API key: the chart API behind finance.yahoo.com is open (unofficial, for personal use).
        "yahoo_finance": YahooFinanceConnector({}, ctx.masker, state, transport=transport),
        # No API key: the fund library and the managers' 基準価額 CSVs are published openly, under their terms of use.
        "toushin_lib": ToushinLibConnector({}, ctx.masker, state, transport=transport),
        "rakuten_csv": RakutenFundCsvConnector({}, ctx.masker, state, transport=transport),
        "daiwa_csv": DaiwaFundCsvConnector({}, ctx.masker, state, transport=transport),
        "rakuten_travel": RakutenTravelConnector(
            {k: secrets[k] for k in ("rakuten_application_id", "rakuten_access_key")},
            ctx.masker,
            state,
            transport=transport,
            endpoint=s.rakuten_vacant_endpoint,
            referer=s.base_url,
        ),
        "browser": BrowserConnector(s.browser_enabled, ctx.masker, state),
    }
    ctx.extras["connectors"] = connectors
    return connectors


class RakutenVacancyParams(BaseModel):
    hotel_no: int = Field(gt=0, description="楽天トラベルの施設番号（施設ページの URL に含まれる数字）")
    checkin: date = Field(description="チェックイン日（YYYY-MM-DD）")
    checkout: date = Field(description="チェックアウト日（YYYY-MM-DD）")
    adults: int = Field(default=2, ge=1, le=10, description="大人の人数")
    rooms: int = Field(default=1, ge=1, le=10, description="部屋数")
    max_charge: int | None = Field(default=None, ge=0, description="1 部屋あたりの上限料金（円、任意）")


def build_tools(ctx: AppContext) -> list[ToolSpec]:
    connectors = get_connectors(ctx)
    specs: list[ToolSpec] = []
    rakuten = connectors["rakuten_travel"]
    if rakuten.configured:

        @define_tool(
            name="search_rakuten_vacancy",
            description=(
                "楽天トラベルで指定した施設・日程・人数の空室を検索する。結果の vacancy_count が空室のあるプラン数。"
                "API キーはツール側で扱うので、URL やキーを指定しないこと。"
            ),
        )
        async def search_rakuten_vacancy(params: RakutenVacancyParams) -> dict:
            try:
                return await rakuten.search_vacancy(
                    hotel_no=params.hotel_no,
                    checkin=params.checkin,
                    checkout=params.checkout,
                    adults=params.adults,
                    rooms=params.rooms,
                    max_charge=params.max_charge,
                )
            except ConnectorError as e:
                return {"error": str(e)}

        specs.append(ToolSpec(search_rakuten_vacancy, connector="rakuten_travel"))
    return specs


router = APIRouter(prefix="/api")


@router.get("/connectors")
def list_connectors(user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)) -> list[dict]:
    return [
        {
            "name": c.info.name,
            "label": c.info.label,
            "configured": c.configured,
            "last_used": c.last_used(),
            "cost": c.info.cost,
        }
        for c in get_connectors(ctx).values()
    ]
