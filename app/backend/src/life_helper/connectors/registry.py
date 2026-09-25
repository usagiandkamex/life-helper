"""Connector instances, the connector tools exposed to Copilot, and the connector status API."""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import date
from typing import cast

from copilot import define_tool
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..auth import CurrentUser, require_user
from ..context import AppContext, get_ctx
from ..tools.registry import ToolSpec
from .base import Connector, ConnectorError
from .browser import BrowserConnector
from .fund_nav import DaiwaFundCsvConnector, RakutenFundCsvConnector, ToushinLibConnector
from .rakuten import BookSort, GolfCourseSort, GolfPlanSort, ItemSort, KoboSort, RakutenConnector
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
        "rakuten": RakutenConnector(
            {k: secrets[k] for k in ("rakuten_application_id", "rakuten_access_key")},
            ctx.masker,
            state,
            transport=transport,
            endpoints=s.rakuten_endpoint_overrides,
            referer=s.base_url,
        ),
        "browser": BrowserConnector(s.browser_enabled, ctx.masker, state),
    }
    ctx.extras["connectors"] = connectors
    return connectors


KEY_NOTE = "API キーはツール側で扱うので、URL やキーを指定しないこと。"


def _page():
    return Field(default=1, ge=1, le=100, description="取得するページ（1 ページ 10 件）")


class RakutenItemSearchParams(BaseModel):
    keyword: str = Field(min_length=1, max_length=128, description="検索キーワード（スペース区切りで AND 検索）")
    min_price: int | None = Field(default=None, ge=0, description="下限価格（円、任意）")
    max_price: int | None = Field(default=None, ge=0, description="上限価格（円、任意）")
    sort: ItemSort = Field(
        default="standard",
        description="並び順: standard=標準、price_asc=安い順、price_desc=高い順、review_count=レビュー件数順、"
        "review_average=レビュー評価順、newest=新着順",
    )
    in_stock_only: bool = Field(default=True, description="在庫のある商品だけにする")
    postage_included_only: bool = Field(default=False, description="送料込みの商品だけにする")
    exclude_keyword: str | None = Field(default=None, max_length=128, description="除外するキーワード（任意）")
    page: int = _page()


class RakutenVacancyParams(BaseModel):
    hotel_no: int = Field(gt=0, description="楽天トラベルの施設番号（施設ページの URL に含まれる数字）")
    checkin: date = Field(description="チェックイン日（YYYY-MM-DD）")
    checkout: date = Field(description="チェックアウト日（YYYY-MM-DD）")
    adults: int = Field(default=2, ge=1, le=10, description="大人の人数")
    rooms: int = Field(default=1, ge=1, le=10, description="部屋数")
    max_charge: int | None = Field(default=None, ge=0, description="1 部屋あたりの上限料金（円、任意）")


class RakutenHotelSearchParams(BaseModel):
    keyword: str = Field(
        min_length=2, max_length=128, description="施設名・地名などのキーワード（スペース区切りで AND）"
    )
    page: int = _page()


class RakutenBookSearchParams(BaseModel):
    keyword: str | None = Field(default=None, max_length=128, description="書名・著者名などのキーワード")
    isbn_jan: str | None = Field(default=None, max_length=20, description="ISBN または JAN コード（13 桁、任意）")
    sort: BookSort = Field(
        default="standard",
        description="並び順: standard=標準、sales=売れている順、newest=発売日が新しい順、oldest=発売日が古い順、"
        "price_asc=安い順、price_desc=高い順、review_count=レビュー件数順、review_average=レビュー評価順",
    )
    page: int = _page()


class RakutenKoboSearchParams(BaseModel):
    keyword: str | None = Field(default=None, max_length=128, description="キーワード")
    title: str | None = Field(default=None, max_length=128, description="タイトル")
    author: str | None = Field(default=None, max_length=128, description="著者名")
    sort: KoboSort = Field(
        default="standard",
        description="並び順: standard=標準、newest=発売日が新しい順、oldest=発売日が古い順、price_asc=安い順、"
        "price_desc=高い順、review_count=レビュー件数順、review_average=レビュー評価順",
    )
    page: int = _page()


class RakutenGolfCourseSearchParams(BaseModel):
    keyword: str | None = Field(default=None, max_length=128, description="ゴルフ場名・地名などのキーワード")
    area_code: int | None = Field(
        default=None, ge=1, le=47, description="都道府県コード（1=北海道 … 13=東京都 … 47=沖縄県）"
    )
    sort: GolfCourseSort = Field(
        default="rating",
        description="並び順: rating=クチコミの多い順、reservation=予約件数順、evaluation=総合評価順、"
        "costperformance=コストパフォーマンス順、course=コース・戦略性の評価順、facility=設備の評価順、"
        "meal=食事の評価順、staff=スタッフ接客の評価順、beginner=初心者向け、normal=中級者向け、senior=上級者向け、"
        "woman=女性向け",
    )
    page: int = _page()


class RakutenGolfPlanSearchParams(BaseModel):
    play_date: date = Field(description="プレー日（YYYY-MM-DD、今日以降）")
    golf_course_id: int | None = Field(
        default=None, gt=0, description="ゴルフ場 ID（search_rakuten_golf_courses で調べる）"
    )
    golf_course_name: str | None = Field(default=None, max_length=128, description="ゴルフ場名")
    area_code: int | None = Field(default=None, ge=1, le=47, description="都道府県コード（1=北海道 … 47=沖縄県）")
    min_price: int | None = Field(default=None, ge=0, description="下限料金（円、任意）")
    max_price: int | None = Field(default=None, ge=0, description="上限料金（円、任意）")
    lunch_included: bool = Field(default=False, description="昼食付きのプランだけにする")
    sort: GolfPlanSort = Field(
        default="reservation",
        description="並び順: reservation=予約件数順、price=料金順、evaluation=総合評価順、"
        "costperformance=コストパフォーマンス順",
    )
    page: int = _page()


class RakutenRecipeRankingParams(BaseModel):
    category: str | None = Field(
        default=None,
        max_length=50,
        description="レシピのカテゴリ名（例: 鶏肉、カレー。部分一致）か、カテゴリ ID（例: 10-276）。"
        "省略すると総合ランキング",
    )


async def _run(call: Awaitable[dict]) -> dict:
    try:
        return await call
    except ConnectorError as e:
        return {"error": str(e)}


def build_tools(ctx: AppContext) -> list[ToolSpec]:
    connectors = get_connectors(ctx)
    rakuten = cast(RakutenConnector, connectors["rakuten"])
    if not rakuten.configured:
        return []

    @define_tool(
        name="search_rakuten_items",
        description=(
            "楽天市場で商品を検索する（1 ページ 10 件）。結果の signal.item_count が該当件数、"
            "signal.min_price がこのページ内の最安値（sort=price_asc なら条件内の最安値）。" + KEY_NOTE
        ),
    )
    async def search_rakuten_items(params: RakutenItemSearchParams) -> dict:
        return await _run(rakuten.search_items(**params.model_dump()))

    @define_tool(
        name="search_rakuten_vacancy",
        description=(
            "楽天トラベルで指定した施設・日程・人数の空室を検索する。結果の vacancy_count が空室のあるプラン数。"
            "施設番号がわからないときは search_rakuten_hotels で調べる。" + KEY_NOTE
        ),
    )
    async def search_rakuten_vacancy(params: RakutenVacancyParams) -> dict:
        return await _run(rakuten.search_vacancy(**params.model_dump()))

    @define_tool(
        name="search_rakuten_hotels",
        description="楽天トラベルの施設をキーワードで検索し、施設番号（hotel_no）・住所・最安料金・評価を調べる。"
        + KEY_NOTE,
    )
    async def search_rakuten_hotels(params: RakutenHotelSearchParams) -> dict:
        return await _run(rakuten.search_hotels(**params.model_dump()))

    @define_tool(
        name="search_rakuten_books",
        description=(
            "楽天ブックスで本・CD・DVD などを検索する（キーワードか ISBN・JAN コード）。signal.book_count が該当件数、"
            "signal.book_min_price がこのページ内の最安値。" + KEY_NOTE
        ),
    )
    async def search_rakuten_books(params: RakutenBookSearchParams) -> dict:
        return await _run(rakuten.search_books(**params.model_dump()))

    @define_tool(
        name="search_rakuten_kobo",
        description=(
            "楽天Kobo の電子書籍を検索する（キーワード・タイトル・著者名）。signal.ebook_count が該当件数、"
            "signal.ebook_min_price がこのページ内の最安値。" + KEY_NOTE
        ),
    )
    async def search_rakuten_kobo(params: RakutenKoboSearchParams) -> dict:
        return await _run(rakuten.search_kobo(**params.model_dump()))

    @define_tool(
        name="search_rakuten_golf_courses",
        description="楽天GORA でゴルフ場をキーワードか都道府県で検索し、ゴルフ場 ID・所在地・評価を調べる。" + KEY_NOTE,
    )
    async def search_rakuten_golf_courses(params: RakutenGolfCourseSearchParams) -> dict:
        return await _run(rakuten.search_golf_courses(**params.model_dump()))

    @define_tool(
        name="search_rakuten_golf_plans",
        description=(
            "楽天GORA で指定したプレー日のプランと空き状況を検索する（ゴルフ場 ID・ゴルフ場名・都道府県のどれか）。"
            "結果の plan_count が空き・在庫のあるプラン数。" + KEY_NOTE
        ),
    )
    async def search_rakuten_golf_plans(params: RakutenGolfPlanSearchParams) -> dict:
        return await _run(rakuten.search_golf_plans(**params.model_dump()))

    @define_tool(
        name="get_rakuten_recipe_ranking",
        description="楽天レシピのカテゴリ別ランキング（人気のレシピ）を取得する。"
        "カテゴリは名前（部分一致）か ID で指定する。" + KEY_NOTE,
    )
    async def get_rakuten_recipe_ranking(params: RakutenRecipeRankingParams) -> dict:
        return await _run(rakuten.recipe_ranking(**params.model_dump()))

    tools = (
        search_rakuten_items,
        search_rakuten_vacancy,
        search_rakuten_hotels,
        search_rakuten_books,
        search_rakuten_kobo,
        search_rakuten_golf_courses,
        search_rakuten_golf_plans,
        get_rakuten_recipe_ranking,
    )
    return [ToolSpec(tool, connector="rakuten") for tool in tools]


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
