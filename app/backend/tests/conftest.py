from __future__ import annotations

from pathlib import Path

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


def stooq_csv(close: float, day: str = "2026-09-24") -> str:
    return f"Date,Open,High,Low,Close,Volume\n{day},1,1,1,{close},1\n"


def mock_stooq(bodies: dict[str, float | str]):
    """Mocks the Stooq CSV endpoint per symbol. Unknown symbols answer "No data", like Stooq does."""
    import httpx
    import respx

    def handler(request: httpx.Request) -> httpx.Response:
        body = bodies.get(request.url.params["s"])
        if body is None:
            return httpx.Response(200, text="No data")
        return httpx.Response(200, text=body if isinstance(body, str) else stooq_csv(body))

    return respx.get("https://stooq.com/q/d/l/").mock(side_effect=handler)


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
