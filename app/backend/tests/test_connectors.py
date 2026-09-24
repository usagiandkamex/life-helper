from __future__ import annotations

import logging
from datetime import date

import httpx
import pytest
import respx

from life_helper.connectors.base import ConnectorError
from life_helper.connectors.fund_nav import (
    MIN_SCORE,
    DaiwaFundCsvConnector,
    MufgFundApiConnector,
    RakutenFundCsvConnector,
    match_score,
)
from life_helper.connectors.rakuten_travel import RakutenTravelConnector, parse_vacancies
from life_helper.connectors.stooq import StooqConnector, symbol_candidates, to_stooq_symbol
from life_helper.security import SecretMasker

from .conftest import (
    ALL_COUNTRY,
    DAIWA_HEADER,
    FANG_PLUS,
    RAKUTEN_HEADER,
    SP500,
    mock_daiwa,
    mock_mufg,
    mock_rakuten,
    mock_stooq,
    mufg_payload,
    sign_in,
    stooq_csv,
)

STOOQ_KEY = "stooqkey-ABCDEF123456"
APP_ID = "e5e2671a-b454-4e6f-aaaa-bbbbccccdddd"
ACCESS_KEY = "rakuten-access-key-987654"


@pytest.fixture
def masker():
    return SecretMasker([])


@pytest.fixture
def stooq(tmp_path, masker):
    return StooqConnector({"stooq_api_key": STOOQ_KEY}, masker, tmp_path / "state.json")


@pytest.fixture
def mufg(tmp_path, masker):
    return MufgFundApiConnector({}, masker, tmp_path / "state.json")


@pytest.fixture
def daiwa(tmp_path, masker):
    return DaiwaFundCsvConnector({}, masker, tmp_path / "state.json")


@pytest.fixture
def rakuten_fund(tmp_path, masker):
    return RakutenFundCsvConnector({}, masker, tmp_path / "state.json")


@pytest.fixture
def rakuten(tmp_path, masker):
    return RakutenTravelConnector(
        {"rakuten_application_id": APP_ID, "rakuten_access_key": ACCESS_KEY},
        masker,
        tmp_path / "state.json",
        referer="https://app.example",
    )


def test_symbol_conversion():
    assert to_stooq_symbol("7203") == "7203.jp"
    assert to_stooq_symbol("130a") == "130a.jp"
    assert to_stooq_symbol("7203.T") == "7203.jp"
    assert to_stooq_symbol("7203.JP") == "7203.jp"
    assert to_stooq_symbol("MSFT") == "msft.us"
    assert to_stooq_symbol("aapl") == "aapl.us"
    assert to_stooq_symbol("MSFT.US") == "msft.us"
    # The code shape only picks the market to try first; the other one stays available as a fallback.
    assert symbol_candidates("7203") == [("jp", "7203.jp"), ("us", "7203.us")]
    assert symbol_candidates("MSFT") == [("us", "msft.us"), ("jp", "msft.jp")]
    assert symbol_candidates("MSFT.JP") == [("jp", "msft.jp"), ("us", "msft.us")]
    for bad in ("../x", "", "1", "TOOLONG", "７２０３"):
        with pytest.raises(ConnectorError):
            to_stooq_symbol(bad)


@respx.mock
async def test_stooq_previous_close(stooq, masker):
    csv_body = (
        "Date,Open,High,Low,Close,Volume\n2026-09-22,3000,3050,2990,3020,100\n2026-09-24,3020,3100,3010,3080,120\n"
    )
    route = respx.get("https://stooq.com/q/d/l/").mock(return_value=httpx.Response(200, text=csv_body))
    result = await stooq.previous_close("7203", today=date(2026, 9, 25))
    assert result == {
        "code": "7203",
        "symbol": "7203.jp",
        "market": "jp",
        "currency": "JPY",
        "date": "2026-09-24",
        "close": 3080.0,
        "source": "stooq",
    }
    sent = route.calls.last.request.url
    assert sent.params["apikey"] == STOOQ_KEY and sent.params["s"] == "7203.jp"
    assert STOOQ_KEY not in str(result)
    # The key is registered with the masker as soon as the connector is created.
    assert masker.mask_text(f"x?apikey={STOOQ_KEY}") == "x?apikey=***"
    assert stooq.last_used() is not None


@respx.mock
async def test_stooq_previous_close_us_ticker(stooq):
    route = mock_stooq({"msft.us": 517.93})
    result = await stooq.previous_close("MSFT", today=date(2026, 9, 25))
    assert (result["market"], result["symbol"], result["currency"], result["close"]) == ("us", "msft.us", "USD", 517.93)
    assert [c.request.url.params["s"] for c in route.calls] == ["msft.us"]


@respx.mock
async def test_stooq_falls_back_to_the_other_market(stooq):
    stooq.min_interval_seconds = 0
    route = mock_stooq({"7203.us": 12.5})
    result = await stooq.previous_close("7203", today=date(2026, 9, 25))
    assert (result["market"], result["symbol"], result["currency"]) == ("us", "7203.us", "USD")
    assert [c.request.url.params["s"] for c in route.calls] == ["7203.jp", "7203.us"]


@respx.mock
async def test_stooq_reports_the_symbols_it_tried(stooq):
    stooq.min_interval_seconds = 0
    mock_stooq({})
    with pytest.raises(ConnectorError) as e:
        await stooq.previous_close("MSFT", today=date(2026, 9, 25))
    assert "msft.us と msft.jp を照会しました" in str(e.value)


@respx.mock
async def test_stooq_usd_jpy(stooq):
    mock_stooq({"usdjpy": 150.25})
    assert await stooq.usd_jpy(today=date(2026, 9, 25)) == {
        "pair": "USDJPY",
        "symbol": "usdjpy",
        "date": "2026-09-24",
        "rate": 150.25,
        "source": "stooq",
    }


@respx.mock
async def test_stooq_failures_are_distinguished(stooq):
    stooq.min_interval_seconds = 0
    route = respx.get("https://stooq.com/q/d/l/")
    for body, message in (
        ("Exceeded the daily hits limit", "利用上限"),
        ("<html>get your apikey</html>", "API キーが無効"),
        ("<html>maintenance</html>", "想定外の応答"),
        ("", "想定外の応答"),
        ("Date,Open,High,Low\n2026-09-24,1,1,1\n", "想定外の応答"),
        (stooq_csv(0), "株価が不正"),
        ("Date,Open,High,Low,Close,Volume\n2026-99-99,1,1,1,10,1\n", "日付が不正"),
        (stooq_csv(10, "2026-10-05"), "日付が不正"),
    ):
        route.mock(return_value=httpx.Response(200, text=body))
        with pytest.raises(ConnectorError, match=message):
            await stooq.previous_close("7203", today=date(2026, 9, 25))
    route.mock(return_value=httpx.Response(503, text=""))
    with pytest.raises(ConnectorError, match="HTTP 503"):
        await stooq.previous_close("7203", today=date(2026, 9, 25))
    # A bad key or a broken answer must not be retried on the other market.
    assert all(call.request.url.params["s"] == "7203.jp" for call in route.calls)


@respx.mock
async def test_stooq_html_response_is_reported_without_key(stooq):
    respx.get("https://stooq.com/q/d/l/").mock(return_value=httpx.Response(200, text="<html>get your apikey</html>"))
    with pytest.raises(ConnectorError) as e:
        await stooq.previous_close("7203")
    assert STOOQ_KEY not in str(e.value)


@respx.mock
async def test_network_error_does_not_leak_url(stooq):
    respx.get("https://stooq.com/q/d/l/").mock(
        side_effect=httpx.ConnectError(f"failed https://stooq.com/?apikey={STOOQ_KEY}")
    )
    with pytest.raises(ConnectorError) as e:
        await stooq.previous_close("7203")
    assert STOOQ_KEY not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


async def test_missing_key_message(tmp_path, masker):
    empty = StooqConnector({"stooq_api_key": ""}, masker, tmp_path / "s.json")
    assert not empty.configured
    with pytest.raises(ConnectorError, match="API キーが登録されていません"):
        await empty.previous_close("7203")


async def test_connector_refuses_other_hosts(stooq):
    with pytest.raises(ConnectorError):
        await stooq.get("https://evil.example/", params={})


def test_httpx_logger_is_silenced():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


RAKUTEN_V2 = {
    "hotels": [
        [
            {
                "hotelBasicInfo": {
                    "hotelNo": 123,
                    "hotelName": "テストホテル",
                    "planListUrl": "https://travel.rakuten.co.jp/p",
                }
            },
            {
                "roomInfo": [
                    {
                        "roomBasicInfo": {
                            "planName": "素泊まり",
                            "roomName": "ツイン",
                            "reserveUrl": "https://travel.rakuten.co.jp/r1",
                        }
                    },
                    {"dailyCharge": {"total": 24000}},
                    {
                        "roomBasicInfo": {
                            "planName": "朝食付き",
                            "roomName": "ダブル",
                            "reserveUrl": "https://travel.rakuten.co.jp/r2",
                        }
                    },
                    {"dailyCharge": {"total": 30000}},
                ]
            },
        ]
    ]
}


def test_parse_vacancies_v2():
    plans = parse_vacancies(RAKUTEN_V2)
    assert [p["plan_name"] for p in plans] == ["素泊まり", "朝食付き"]
    assert plans[0]["total_charge"] == 24000 and plans[0]["hotel_name"] == "テストホテル"


@respx.mock
async def test_rakuten_vacancy_found(rakuten):
    route = respx.get(url__startswith="https://openapi.rakuten.co.jp/").mock(
        return_value=httpx.Response(200, json=RAKUTEN_V2)
    )
    result = await rakuten.search_vacancy(hotel_no=123, checkin=date(2026, 12, 30), checkout=date(2026, 12, 31))
    assert result["vacant"] is True and result["signal"] == {"vacancy_count": 2}
    req = route.calls.last.request
    assert req.url.params["applicationId"] == APP_ID and req.url.params["accessKey"] == ACCESS_KEY
    assert req.headers["referer"] == "https://app.example"
    assert APP_ID not in str(result) and ACCESS_KEY not in str(result)


@respx.mock
async def test_rakuten_no_vacancy(rakuten):
    respx.get(url__startswith="https://openapi.rakuten.co.jp/").mock(
        return_value=httpx.Response(404, json={"error": "not_found", "error_description": "not found"})
    )
    result = await rakuten.search_vacancy(hotel_no=123, checkin=date(2026, 12, 30), checkout=date(2026, 12, 31))
    assert result["vacant"] is False and result["vacancy_count"] == 0


@respx.mock
async def test_rakuten_error_and_validation(rakuten):
    respx.get(url__startswith="https://openapi.rakuten.co.jp/").mock(
        return_value=httpx.Response(400, json={"error": "wrong_parameter"})
    )
    with pytest.raises(ConnectorError, match="wrong_parameter"):
        await rakuten.search_vacancy(hotel_no=1, checkin=date(2026, 1, 2), checkout=date(2026, 1, 3))
    with pytest.raises(ConnectorError):
        await rakuten.search_vacancy(hotel_no=1, checkin=date(2026, 1, 3), checkout=date(2026, 1, 3))


def test_rakuten_rejects_foreign_endpoint(tmp_path, masker):
    with pytest.raises(ValueError):
        RakutenTravelConnector({}, masker, tmp_path / "s.json", endpoint="https://evil.example/api")


def test_fund_code_decides_the_api_path():
    assert MufgFundApiConnector.code_path("0331418A") == "association_fund_cd/0331418A"
    assert MufgFundApiConnector.code_path("jp90c000h1t1") == "isin_cd/JP90C000H1T1"
    assert MufgFundApiConnector.code_path("253425") == "fund_cd/253425"
    # A code that could escape the URL path, or that fits no known code shape, never reaches the API.
    for bad in ("", "../../etc", "0331418A/../x", "03314", "25342A", "0331418A0331418A"):
        with pytest.raises(ConnectorError, match="ファンドコード"):
            MufgFundApiConnector.code_path(bad)


def test_fund_name_matching_only_scores_candidates():
    # Managers write fund names in full-width characters, brokers in half-width.
    assert (
        match_score(
            "eMAXIS Slim 全世界株式（オール・カントリー）", "ｅＭＡＸＩＳ Ｓｌｉｍ 全世界株式（オール・カントリー）"
        )
        == 1.0
    )
    assert match_score("eMAXIS Slim 全世界株式", "ｅＭＡＸＩＳ Ｓｌｉｍ 全世界株式（オール・カントリー）") == 0.9
    assert match_score("ひふみプラス", "ｅＭＡＸＩＳ Ｓｌｉｍ 米国株式（Ｓ＆Ｐ５００）") < MIN_SCORE


@respx.mock
async def test_mufg_fund_nav(mufg):
    route = mock_mufg({"0331418A": (25_341, "20260924")})
    result = await mufg.fund_nav("0331418A", today=date(2026, 9, 25))
    assert result == {
        "fund_code": "0331418A",
        "name": "ｅＭＡＸＩＳ Ｓｌｉｍ 全世界株式（オール・カントリー）",
        "nav": 25_341.0,
        "price_unit": 10_000,
        "date": "2026-09-24",
        "source": "mufg_api",
        "source_url": "https://developer.am.mufg.jp/fund_information_latest/association_fund_cd/0331418A",
        "manager": "三菱UFJアセットマネジメント",
        "isin": "JP90C000H1T1",
        "association_code": "0331418A",
    }
    assert route.calls.last.request.url.path == "/fund_information_latest/association_fund_cd/0331418A"
    assert mufg.configured and mufg.last_used() is not None


@respx.mock
async def test_mufg_search_offers_candidates(mufg):
    mock_mufg({})
    candidates = await mufg.search_funds("eMAXIS Slim 全世界株式（オール・カントリー）")
    assert [(c["fund_code"], c["score"]) for c in candidates] == [("0331418A", 1.0)]
    assert candidates[0]["provider"] == "mufg_api" and candidates[0]["price_unit"] == 10_000
    # A name that matches nothing offers nothing, instead of returning the closest fund.
    assert await mufg.search_funds("ひふみプラス") == []


@respx.mock
async def test_mufg_never_values_a_holding_with_another_fund(mufg):
    mufg.min_interval_seconds = 0
    route = respx.get(url__startswith="https://developer.am.mufg.jp")
    route.mock(return_value=httpx.Response(200, json=mufg_payload(SP500 | {"nav": 30_000, "base_date": "20260924"})))
    with pytest.raises(ConnectorError, match="別のファンド"):
        await mufg.fund_nav("0331418A", today=date(2026, 9, 25))
    # The code has to come back in the field it was asked for: another fund's ISIN is not an association code.
    mixed = ALL_COUNTRY | {"isin_cd": "JP90C000FYT1", "association_fund_cd": "JP90C000H1T1", "nav": 1}
    route.mock(return_value=httpx.Response(200, json=mufg_payload(mixed | {"base_date": "20260924"})))
    with pytest.raises(ConnectorError, match="別のファンド"):
        await mufg.fund_nav("JP90C000H1T1", today=date(2026, 9, 25))


@respx.mock
async def test_mufg_failures_are_distinguished(mufg):
    mufg.min_interval_seconds = 0
    route = respx.get(url__startswith="https://developer.am.mufg.jp")
    for response, message in (
        (httpx.Response(403, text="ERROR: The request could not be satisfied"), "HTTP 403"),
        (httpx.Response(503, text=""), "HTTP 503"),
        (httpx.Response(200, text="<html>maintenance</html>"), "JSON ではありません"),
        (httpx.Response(200, json={"result": {"status": 400}, "errors": {"count": 1}}), "エラーを返しました"),
        # An error next to a dataset is still an error: the dataset is not used.
        (
            httpx.Response(
                200,
                json=mufg_payload(ALL_COUNTRY | {"nav": 1, "base_date": "20260924"}) | {"errors": {"count": 1}},
            ),
            "エラーを返しました",
        ),
        (httpx.Response(200, json={"result": {"status": 200}}), "datasets がありません"),
        (httpx.Response(200, json=mufg_payload()), "見つかりませんでした"),
        (httpx.Response(200, json=mufg_payload(ALL_COUNTRY | {"nav": 0, "base_date": "20260924"})), "基準価額が不正"),
        (httpx.Response(200, json=mufg_payload(ALL_COUNTRY | {"base_date": "20260924"})), "基準価額が不正"),
        # A number too large to be a NAV is refused here, so it never reaches the portfolio file.
        (
            httpx.Response(200, json=mufg_payload(ALL_COUNTRY | {"nav": 1e100, "base_date": "20260924"})),
            "基準価額が不正",
        ),
        (httpx.Response(200, json=mufg_payload(ALL_COUNTRY | {"nav": 1, "base_date": "2026-99-99"})), "基準日が不正"),
        (httpx.Response(200, json=mufg_payload(ALL_COUNTRY | {"nav": 1, "base_date": "20261005"})), "未来の日付"),
    ):
        route.mock(return_value=response)
        with pytest.raises(ConnectorError, match=message):
            await mufg.fund_nav("0331418A", today=date(2026, 9, 25))


@respx.mock
async def test_mufg_network_error_is_reported_as_connector_error(mufg):
    respx.get(url__startswith="https://developer.am.mufg.jp").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ConnectorError, match="接続できませんでした"):
        await mufg.fund_nav("0331418A")


async def test_fund_connector_refuses_other_hosts(mufg):
    with pytest.raises(ConnectorError):
        await mufg.get("https://evil.example/fund_information_latest/fund_cd/253425", params={})


@respx.mock
async def test_daiwa_fund_nav(daiwa):
    route = mock_daiwa({"3346": (28_251, "20260924")})
    result = await daiwa.fund_nav("3346", today=date(2026, 9, 25))
    assert result == {
        "fund_code": "3346",
        # 大和 names the download after the fund, which is the only official name the CSV carries.
        "name": FANG_PLUS,
        "nav": 28_251.0,
        "price_unit": 10_000,
        "date": "2026-09-24",
        "source": "daiwa_csv",
        "source_url": "https://www.daiwa-am.co.jp/funds/detail/3346/detail_top.html",
        "manager": "大和アセットマネジメント",
        "isin": None,
        "association_code": None,
    }
    request = route.calls.last.request
    assert request.url.path == "/funds/detail/csv_out.php"
    assert dict(request.url.params) == {"code": "3346", "type": "1"}


@respx.mock
async def test_rakuten_fund_nav(rakuten_fund):
    route = mock_rakuten({"100124": (13_999, "2026/09/24")})
    result = await rakuten_fund.fund_nav("100124", today=date(2026, 9, 25))
    assert result == {
        "fund_code": "100124",
        # The CSV carries no fund name, so nothing is claimed as the official name.
        "name": "",
        "nav": 13_999.0,
        "price_unit": 10_000,
        "date": "2026-09-24",
        "source": "rakuten_csv",
        "source_url": "https://www.rakuten-toushin.co.jp/assets/csv/chart_100124.csv",
        "manager": "楽天投信投資顧問",
        "isin": None,
        "association_code": None,
    }
    assert route.calls.last.request.url.path == "/assets/csv/chart_100124.csv"


def test_fund_csv_code_shapes_keep_stray_codes_out_of_the_url(daiwa, rakuten_fund):
    assert daiwa.fund_code("３３４６") == "3346"
    assert rakuten_fund.fund_code(" 100124 ") == "100124"
    for connector, bad in (
        (daiwa, "../../etc/passwd"),
        (daiwa, "3346&type=2"),
        (daiwa, "33460"),
        (daiwa, "334a"),
        (daiwa, ""),
        (rakuten_fund, "100124.csv"),
        (rakuten_fund, "3346"),
        (rakuten_fund, "chart_100124"),
    ):
        with pytest.raises(ConnectorError, match="ファンドコード"):
            connector.fund_code(bad)


@respx.mock
async def test_fund_csv_uses_the_newest_row_whatever_the_order(rakuten_fund):
    rows = [RAKUTEN_HEADER, "2026/09/24,13999,13999,412.14,", "2026/09/18,13000,13000,410.00,"]
    respx.get(url__startswith="https://www.rakuten-toushin.co.jp").mock(
        return_value=httpx.Response(200, content="\n".join(rows).encode("cp932"))
    )
    result = await rakuten_fund.fund_nav("100124", today=date(2026, 9, 25))
    assert (result["date"], result["nav"]) == ("2026-09-24", 13_999.0)


@respx.mock
async def test_fund_csv_reads_columns_by_name(daiwa):
    # A manager adding or reordering columns must not shift which value is read as the NAV.
    body = "ダウンロード日 2026/09/25\n\n前日比,基準価額,基準日,分配金再投資基準価額\n-18,28251,2026年9月24日,99999\n"
    respx.get(url__startswith="https://www.daiwa-am.co.jp").mock(
        return_value=httpx.Response(200, content=body.encode("utf-8-sig"))
    )
    result = await daiwa.fund_nav("3346", today=date(2026, 9, 25))
    assert (result["date"], result["nav"]) == ("2026-09-24", 28_251.0)


@respx.mock
async def test_fund_csv_failures_are_distinguished(daiwa):
    daiwa.min_interval_seconds = 0
    route = respx.get(url__startswith="https://www.daiwa-am.co.jp")
    for response, message in (
        (httpx.Response(404, text="Not Found"), "公式 CSV がありません"),
        (httpx.Response(503, text=""), "HTTP 503"),
        (httpx.Response(200, text="<!DOCTYPE html><html>maintenance</html>"), "CSV ではありません"),
        (httpx.Response(200, content=b"\x89\xba\x97\x8e"), "列が見つかりません"),
        (httpx.Response(200, text=DAIWA_HEADER), "基準価額の行がありません"),
        (httpx.Response(200, text=f"{DAIWA_HEADER}\n備考,合計,,,,,\n"), "基準価額の行がありません"),
        (httpx.Response(200, text=f"{DAIWA_HEADER}\n20260924,0,0,1,0,0,0\n"), "基準価額が不正"),
        (httpx.Response(200, text=f"{DAIWA_HEADER}\n20260924,-1,0,1,0,0,0\n"), "基準価額が不正"),
        (httpx.Response(200, text=f"{DAIWA_HEADER}\n20260924,1e100,0,1,0,0,0\n"), "基準価額が不正"),
        # A date past today is a corrupt file, not tomorrow's NAV published early.
        (httpx.Response(200, text=f"{DAIWA_HEADER}\n20261005,28251,0,1,0,0,0\n"), "未来の日付"),
    ):
        route.mock(return_value=response)
        with pytest.raises(ConnectorError, match=message):
            await daiwa.fund_nav("3346", today=date(2026, 9, 25))


@respx.mock
async def test_fund_csv_network_error_is_reported_as_connector_error(rakuten_fund):
    respx.get(url__startswith="https://www.rakuten-toushin.co.jp").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ConnectorError, match="接続できませんでした"):
        await rakuten_fund.fund_nav("100124")


async def test_csv_providers_never_guess_a_fund_from_its_name(daiwa, rakuten_fund):
    # Neither manager publishes a fund list, so a name resolves to nothing instead of to a similar fund.
    assert await daiwa.search_funds("iFreeNEXT FANG+インデックス") == []
    assert await rakuten_fund.search_funds("楽天・全米株式インデックス・ファンド") == []


async def test_fund_csv_connectors_refuse_other_hosts(daiwa, rakuten_fund):
    for connector in (daiwa, rakuten_fund):
        with pytest.raises(ConnectorError):
            await connector.get("https://evil.example/assets/csv/chart_100124.csv", params={})


def test_tool_registered_only_when_configured(ctx, settings):
    from pydantic import SecretStr

    from life_helper.connectors.registry import build_tools

    assert build_tools(ctx) == []
    settings.rakuten_application_id = SecretStr(APP_ID)
    settings.rakuten_access_key = SecretStr(ACCESS_KEY)
    ctx.extras.pop("connectors", None)
    assert [s.tool.name for s in build_tools(ctx)] == ["search_rakuten_vacancy"]


def test_connectors_api_never_returns_secret_values(client, ctx, settings):
    from pydantic import SecretStr

    settings.stooq_api_key = SecretStr(STOOQ_KEY)
    ctx.extras.pop("connectors", None)
    sign_in(client, ctx)
    body = client.get("/api/connectors").json()
    stooq = next(c for c in body if c["name"] == "stooq")
    assert stooq["configured"] is True
    assert STOOQ_KEY not in str(body)
