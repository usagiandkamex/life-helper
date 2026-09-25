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
PUBLIC_TEST_IP = "93.184.215.14"


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """No real DNS in tests: every name resolves to a public address unless a test maps it elsewhere."""
    from life_helper import netguard

    answers: dict[str, list[str]] = {}

    async def lookup(host: str) -> list[str]:
        if host in answers and not answers[host]:
            raise OSError("no such host")
        return answers.get(host, [PUBLIC_TEST_IP])

    netguard.clear_dns_cache()
    monkeypatch.setattr(netguard, "_lookup", lookup)
    yield answers
    netguard.clear_dns_cache()


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
    "isinCd": "JP90C000H1T1",
    "associFundCd": "0331418A",
    "fundNm": "ｅＭＡＸＩＳ　Ｓｌｉｍ全世界株式（オール・カントリー）",
    "fundNkNm": None,
    "entrustCmpNm": "三菱ＵＦＪアセットマネジメント",
    "standardPrice": "25341.0",
    "standardDate": "2026-09-24 00:00:00",
}
SP500 = {
    "isinCd": "JP90C000GKC6",
    "associFundCd": "03311187",
    "fundNm": "ｅＭＡＸＩＳ　Ｓｌｉｍ米国株式（Ｓ＆Ｐ５００）",
    "fundNkNm": None,
    "entrustCmpNm": "三菱ＵＦＪアセットマネジメント",
    "standardPrice": "44842.0",
    "standardDate": "2026-09-24 00:00:00",
}
SCHD = {
    "isinCd": "JP90C000R6N1",
    "associFundCd": "9I312249",
    "fundNm": "楽天・シュワブ・高配当株式・米国ファンド（四半期決算型）",
    "fundNkNm": "楽天・ＳＣＨＤ",
    "entrustCmpNm": "楽天投信投資顧問",
    "standardPrice": "13246.0",
    "standardDate": "2026-09-24 00:00:00",
}
SCHD_GROWTH = {
    "isinCd": "JP90C000S073",
    "associFundCd": "9I316257",
    "fundNm": "楽天・シュワブ・高配当株式・米国ファンド（資産成長型）",
    "fundNkNm": "楽天・ＳＣＨＤ（資産成長型）",
    "entrustCmpNm": "楽天投信投資顧問",
    "standardPrice": "13509.0",
    "standardDate": "2026-09-24 00:00:00",
}
LIBRARY = (ALL_COUNTRY, SP500, SCHD, SCHD_GROWTH)
TOUSHIN_HEADER = "年月日,基準価額(円),純資産総額（百万円）,分配金,決算期"


def toushin_csv(nav: float, day: str = "2026年09月24日") -> str:
    """The NAV history CSV of the fund library: 年月日 like 2026年09月24日, oldest first, no fund identifier."""
    return f"{TOUSHIN_HEADER}\n2018年07月03日,10038,1,,\n{day},{nav},12979588,,\n"


def toushin_page(fund: dict | None) -> str:
    """The fund page: the title is the official name, and the CSV download link names the ISIN and 協会コード."""
    if fund is None:
        return "<html><head><title></title></head><body>該当するファンドがありません</body></html>"
    isin, code = fund["isinCd"], fund["associFundCd"]
    return (
        f"<html><head><title>{fund['fundNm']}</title></head><body>"
        f'<a href="/FdsWeb/download?reportId=1&amp;updateFlag=1&amp;associFundCd={code}">目論見書</a>'
        f'<a href="/FdsWeb/FDST030000/csv-file-download?isinCd={isin}&amp;associFundCd={code}" id="download">CSV</a>'
        f'<input type="hidden" id="isinCd" value="{isin}"><input type="hidden" id="associFundCd" value="{code}" />'
        "</body></html>"
    )


def mock_toushin(navs: dict[str, tuple[float, str]], *, funds=LIBRARY):
    """Mocks the fund library. ``navs`` maps an ISIN to (基準価額, 年月日 like 2026年09月24日).

    Like the real site, the keyword search is not fuzzy enough to find a name with the nickname appended, and the
    CSV answers with the fund of the 協会コード even when the ISIN belongs to another fund.
    """
    import json

    import httpx
    import respx

    from life_helper.connectors.fund_nav import normalize_name

    def search(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        keyword = normalize_name(body["t_keyword"]) if body.get("t_kensakuKbn") == "1" else ""
        hits = [f for f in funds if keyword in normalize_name(f["fundNm"])]
        start = int(body.get("startNo", 0))
        page = hits[start : start + 20]
        info = {"recordsTotal": str(len(hits)), "pageSize": "20", "startNo": str(start), "resultInfoMapList": page}
        return httpx.Response(200, json={"statusCode": None, "searchResultInfo": info})

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.method == "POST" and request.url.path == "/FdsWeb/FDST999900/fundDataSearch":
            return search(request)
        if request.url.path == "/FdsWeb/FDST030000":
            return httpx.Response(
                200, text=toushin_page(next((f for f in funds if f["isinCd"] == params.get("isinCd")), None))
            )
        if request.url.path == "/FdsWeb/FDST030000/csv-file-download":
            if not params.get("isinCd") or not params.get("associFundCd"):
                return httpx.Response(200, json={"statusCode": None})
            fund = next((f for f in funds if f["associFundCd"] == params["associFundCd"]), None)
            if fund is None or fund["isinCd"] not in navs:
                return httpx.Response(500, json={"statusCode": None})
            nav, day = navs[fund["isinCd"]]
            return httpx.Response(200, content=toushin_csv(nav, day).encode("cp932"))
        return httpx.Response(404, text="Not Found")

    return respx.route(url__startswith="https://toushin-lib.fwg.ne.jp").mock(side_effect=handler)


def toushin_calls(route, path: str) -> list:
    return [c for c in route.calls if c.request.url.path == path]


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
