from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from life_helper.config import Settings
from life_helper.context import build_context
from life_helper.main import create_app

ALLOWED_ID = 134019422


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>app</html>", encoding="utf-8")
    return Settings(
        environment="development",
        base_url="http://testserver",
        data_dir=tmp_path / "data",
        static_dir=static,
        allowed_github_user_id=ALLOWED_ID,
        github_oauth_client_id="client-id",
        github_oauth_client_secret="client-secret-value",
        session_secret="test-session-secret-0123456789",
        token_encryption_key=Fernet.generate_key().decode(),
        dev_github_token="gho_devtoken_example",
    )


@pytest.fixture
def ctx(settings: Settings):
    return build_context(settings)


@pytest.fixture
def app(settings: Settings, ctx):
    return create_app(settings, ctx)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def sign_in(client: TestClient, ctx, login: str = "usagiandkamex") -> str:
    """Signs in through the dev-login endpoint with a mocked GitHub user and returns the CSRF token."""
    import respx
    from httpx import Response

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/user").mock(
            return_value=Response(200, json={"id": ALLOWED_ID, "login": login})
        )
        resp = client.post("/auth/dev-login")
    assert resp.status_code == 200, resp.text
    return client.get("/api/me").json()["csrf_token"]


def yahoo_chart(
    bars: list[tuple[str, float | None]],
    *,
    currency: str = "JPY",
    zone: str = "Asia/Tokyo",
    instrument: str = "EQUITY",
    hour: int | time = 9,
    period: tuple[datetime, datetime] | None = None,
) -> dict:
    """A Yahoo Finance chart answer with one daily bar per (YYYY-MM-DD, close), stamped at ``hour`` local time.

    Yahoo stamps a daily bar with the start of its session (09:00 in Tokyo, 09:30 in New York). ``period`` is the
    current trading period; by default one that settled long ago, so every bar counts as a close.
    """
    tz = ZoneInfo(zone)
    at = hour if isinstance(hour, time) else time(hour)
    stamps = [int(datetime.combine(date.fromisoformat(d), at, tz).timestamp()) for d, _ in bars]
    start, end = period or (datetime(2000, 1, 4, 9, tzinfo=tz), datetime(2000, 1, 4, 15, tzinfo=tz))
    meta: dict = {
        "currency": currency,
        "exchangeTimezoneName": zone,
        "instrumentType": instrument,
        "currentTradingPeriod": {"regular": {"start": int(start.timestamp()), "end": int(end.timestamp())}},
    }
    result = {"meta": meta, "timestamp": stamps, "indicators": {"quote": [{"close": [c for _, c in bars]}]}}
    return {"chart": {"result": [result], "error": None}}


YAHOO_NOT_FOUND = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found"}}}


def yahoo_market(symbol: str) -> dict:
    """The currency, exchange timezone and instrument type Yahoo reports for a symbol, by its shape."""
    if symbol == "JPY=X":
        return {"currency": "JPY", "zone": "Europe/London", "instrument": "CURRENCY"}
    if symbol.endswith(".T"):
        return {"currency": "JPY", "zone": "Asia/Tokyo"}
    return {"currency": "USD", "zone": "America/New_York"}


def mock_yahoo(closes: dict[str, float], day: str = "2026-09-24"):
    """Mocks the Yahoo Finance chart API per symbol. Unknown symbols answer 404 "Not Found", like Yahoo does."""
    import httpx
    import respx

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.path.rsplit("/", 1)[-1]
        if symbol not in closes:
            return httpx.Response(404, json=YAHOO_NOT_FOUND)
        return httpx.Response(200, json=yahoo_chart([(day, closes[symbol])], **yahoo_market(symbol)))

    return respx.get(url__startswith="https://query1.finance.yahoo.com/v8/finance/chart/").mock(side_effect=handler)


def yahoo_symbols(route) -> list[str]:
    return [call.request.url.path.rsplit("/", 1)[-1] for call in route.calls]


ALL_COUNTRY = {
    "fund_cd": "253425",
    "isin_cd": "JP90C000H1T1",
    "association_fund_cd": "0331418A",
    "fund_name": "ｅＭＡＸＩＳ Ｓｌｉｍ 全世界株式（オール・カントリー）",
}
SP500 = {
    "fund_cd": "253266",
    "isin_cd": "JP90C000FYT1",
    "association_fund_cd": "0331C180",
    "fund_name": "ｅＭＡＸＩＳ Ｓｌｉｍ 米国株式（Ｓ＆Ｐ５００）",
}


def mufg_payload(*datasets: dict) -> dict:
    """The envelope of the 三菱UFJアセットマネジメント fund API."""
    return {
        "result": {"status": 200, "retcount": len(datasets), "errcd": None, "errmsg": None},
        "errors": {"count": 0, "error_list": None},
        "datasets": list(datasets),
    }


def mock_mufg(navs: dict[str, tuple[float, str]], *, code_list: list[dict] | None = None, funds=(ALL_COUNTRY, SP500)):
    """Mocks the fund API: ``navs`` maps any code of a fund to (基準価額, 基準日 YYYYMMDD)."""
    import httpx
    import respx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/code_list":
            return httpx.Response(200, json=mufg_payload(*(code_list if code_list is not None else funds)))
        code = request.url.path.rsplit("/", 1)[-1]
        for fund in funds:
            if code in (fund["fund_cd"], fund["isin_cd"], fund["association_fund_cd"]) and code in navs:
                nav, base_date = navs[code]
                return httpx.Response(200, json=mufg_payload(fund | {"nav": nav, "base_date": base_date}))
        return httpx.Response(200, json=mufg_payload())

    return respx.get(url__startswith="https://developer.am.mufg.jp").mock(side_effect=handler)


FANG_PLUS = "ｉＦｒｅｅＮＥＸＴ ＦＡＮＧ＋インデックス"
DAIWA_HEADER = "基準日,基準価額（円）,前日比,純資産総額,直近決算日,直近分配金,分配金再投資基準価額"
RAKUTEN_HEADER = "基準日,基準価額 (円),分配金再投資基準価額 (円),純資産総額 (億円),分配金 (円)"


def daiwa_csv(nav: float, day: str = "20260924") -> str:
    """大和アセットマネジメント の基準価額 CSV: 基準日 is YYYYMMDD and the history runs oldest first."""
    return f"{DAIWA_HEADER}\n20180131,10000,0,500000000,0,0,10000\n{day},{nav},-18,4126016286,20260110,0,{nav}\n"


def rakuten_csv(nav: float, day: str = "2026/09/24") -> str:
    """楽天投信投資顧問 の基準価額 CSV: 基準日 is YYYY/MM/DD and 純資産総額 is in 億円."""
    return f"{RAKUTEN_HEADER}\n2018/01/31,10000,10000,500.00,\n{day},{nav},{nav},412.14,\n"


def mock_daiwa(navs: dict[str, tuple[float, str]], *, name: str = FANG_PLUS):
    """Mocks csv_out.php. The CSV is Shift_JIS and the fund name only appears in the download name."""
    import httpx
    import respx

    def handler(request: httpx.Request) -> httpx.Response:
        code = request.url.params.get("code", "")
        if code not in navs:
            # An unknown code answers with the fund search page instead of a CSV.
            return httpx.Response(200, text="<html><body>ファンドが見つかりません</body></html>")
        nav, day = navs[code]
        return httpx.Response(
            200,
            content=daiwa_csv(nav, day).encode("cp932"),
            headers={b"content-disposition": f'attachment; filename="{name}.csv"'.encode("cp932")},
        )

    return respx.get(url__startswith="https://www.daiwa-am.co.jp").mock(side_effect=handler)


def mock_rakuten(navs: dict[str, tuple[float, str]]):
    """Mocks the chart CSV of 楽天投信投資顧問, which is filed under a 6-digit chart id."""
    import httpx
    import respx

    def handler(request: httpx.Request) -> httpx.Response:
        code = request.url.path.removeprefix("/assets/csv/chart_").removesuffix(".csv")
        if code not in navs:
            return httpx.Response(404, text="Not Found")
        nav, day = navs[code]
        return httpx.Response(200, content=rakuten_csv(nav, day).encode("cp932"))

    return respx.get(url__startswith="https://www.rakuten-toushin.co.jp").mock(side_effect=handler)
