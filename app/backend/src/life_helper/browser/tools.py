"""The browser_* tools exposed to Copilot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from copilot import define_tool
from pydantic import BaseModel, Field

from ..tools.registry import ToolSpec
from .guard import MAX_FILL_CHARS
from .service import BrowserSession, get_browser_service, screenshot_dir

if TYPE_CHECKING:
    from ..context import AppContext

BROWSER_CONNECTOR = "browser"


class OpenParams(BaseModel):
    url: str = Field(description="開く URL（http / https の外部サイト）")
    wait_for_selector: str | None = Field(default=None, description="表示されるまで待つ要素の CSS セレクタ（任意）")


class ReadParams(BaseModel):
    selector: str | None = Field(default=None, description="読む範囲の CSS セレクタ。省略するとページ全体")


class ClickParams(BaseModel):
    selector: str | None = Field(default=None, description="クリックする要素の CSS セレクタ")
    text: str | None = Field(default=None, description="クリックする要素に表示されている文字（selector の代わり）")


class FillParams(BaseModel):
    selector: str = Field(description="入力欄の CSS セレクタ（browser_open の inputs に出る selector）")
    value: str = Field(max_length=MAX_FILL_CHARS, description="入力する検索語や条件")
    submit: bool = Field(default=False, description="入力後に Enter を押して送信するか")


class ScrollParams(BaseModel):
    times: int = Field(default=1, ge=1, le=10, description="1 画面分ずつスクロールする回数")
    wait_for_selector: str | None = Field(default=None, description="表示されるまで待つ要素の CSS セレクタ（任意）")


class ScreenshotParams(BaseModel):
    full_page: bool = Field(default=False, description="ページ全体を撮るか（縦 4000px まで）。既定は見えている範囲")


def build_tools(ctx: AppContext) -> list[ToolSpec]:
    if not ctx.settings.browser_enabled:
        return []
    from ..connectors.registry import get_connectors

    connector = get_connectors(ctx).get(BROWSER_CONNECTOR)
    session = BrowserSession(
        get_browser_service(ctx),
        ctx.masker,
        screenshot_dir(ctx.settings),
        on_use=getattr(connector, "mark_used", None),
    )

    @define_tool(
        name="browser_open",
        description=(
            "ヘッドレスブラウザで Web ページを開き、JavaScript で表示された後の本文・リンク・表・入力欄・"
            "画像の説明（image_labels）を返す。web_fetch で本文が取れないページに使う。"
            "開けるのは外部の http / https サイトだけ。"
        ),
    )
    async def browser_open(params: OpenParams) -> str:
        return await session.run_json(lambda: session.open(params.url, params.wait_for_selector))

    @define_tool(
        name="browser_read",
        description=(
            "browser_open で開いているページの本文・リンク・表・入力欄・画像の説明（image_labels）を読み直す。"
            "selector で範囲を絞れる。"
        ),
    )
    async def browser_read(params: ReadParams) -> str:
        return await session.run_json(lambda: session.read(params.selector))

    @define_tool(
        name="browser_click",
        description=(
            "開いているページの要素（「もっと見る」、タブ、ページ送り、リンクなど）をクリックし、クリック後の内容を返す。"
            "selector か text のどちらかを指定する。"
        ),
    )
    async def browser_click(params: ClickParams) -> str:
        return await session.run_json(lambda: session.click(params.selector, params.text))

    @define_tool(
        name="browser_fill",
        description=(
            "開いているページの入力欄に検索語や条件を入力する（submit=true で Enter を押して送信）。"
            "パスワード・カード情報の欄、ログインや会員登録のフォーム、個人情報や機微情報の入力はできない。"
        ),
    )
    async def browser_fill(params: FillParams) -> str:
        return await session.run_json(lambda: session.fill(params.selector, params.value, params.submit))

    @define_tool(
        name="browser_scroll",
        description="開いているページを下へスクロールして続きを読み込み、内容を返す。",
    )
    async def browser_scroll(params: ScrollParams) -> str:
        return await session.run_json(lambda: session.scroll(params.times, params.wait_for_selector))

    @define_tool(
        name="browser_screenshot",
        description="開いているページのスクリーンショットを撮り、利用者のチャット画面に表示する（画像はあなたには渡らない）。",
    )
    async def browser_screenshot(params: ScreenshotParams) -> str:
        return await session.run_json(lambda: session.screenshot(params.full_page))

    tools = (browser_open, browser_read, browser_click, browser_fill, browser_scroll, browser_screenshot)
    return [ToolSpec(tool, connector=BROWSER_CONNECTOR, release=session.close) for tool in tools]
