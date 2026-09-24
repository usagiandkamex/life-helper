from __future__ import annotations

import logging
from datetime import date

import httpx
import pytest
import respx

from life_helper.connectors.base import ConnectorError
from life_helper.connectors.rakuten_travel import RakutenTravelConnector, parse_vacancies
from life_helper.connectors.stooq import StooqConnector, to_stooq_symbol
from life_helper.security import SecretMasker

from .conftest import sign_in

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
    with pytest.raises(ConnectorError):
        to_stooq_symbol("../x")


@respx.mock
async def test_stooq_previous_close(stooq, masker):
    csv_body = (
        "Date,Open,High,Low,Close,Volume\n2026-09-22,3000,3050,2990,3020,100\n2026-09-24,3020,3100,3010,3080,120\n"
    )
    route = respx.get("https://stooq.com/q/d/l/").mock(return_value=httpx.Response(200, text=csv_body))
    result = await stooq.previous_close("7203", today=date(2026, 9, 25))
    assert result == {"code": "7203", "symbol": "7203.jp", "date": "2026-09-24", "close": 3080.0, "source": "stooq"}
    sent = route.calls.last.request.url
    assert sent.params["apikey"] == STOOQ_KEY and sent.params["s"] == "7203.jp"
    assert STOOQ_KEY not in str(result)
    # The key is registered with the masker as soon as the connector is created.
    assert masker.mask_text(f"x?apikey={STOOQ_KEY}") == "x?apikey=***"
    assert stooq.last_used() is not None


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
