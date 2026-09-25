from __future__ import annotations

import gzip
import json
import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from life_helper.connectors.base import ConnectorError
from life_helper.connectors.fund_nav import (
    MAX_CSV_BYTES,
    MIN_SCORE,
    DaiwaFundCsvConnector,
    RakutenFundCsvConnector,
    TooManyFundsError,
    ToushinLibConnector,
    keyword_variants,
    match_score,
    same_fund_name,
)
from life_helper.connectors.rakuten_travel import RakutenTravelConnector, parse_vacancies
from life_helper.connectors.registry import get_connectors
from life_helper.connectors.yahoo_finance import (
    CHART_URL,
    MAX_CHART_BYTES,
    RateLimitedError,
    YahooFinanceConnector,
    symbol_candidates,
    to_yahoo_symbol,
)
from life_helper.security import SecretMasker

from .conftest import (
    ALL_COUNTRY,
    DAIWA_HEADER,
    FANG_PLUS,
    RAKUTEN_HEADER,
    SCHD,
    SCHD_GROWTH,
    SP500,
    TOUSHIN_HEADER,
    YAHOO_NOT_FOUND,
    daiwa_csv,
    mock_daiwa,
    mock_rakuten,
    mock_toushin,
    mock_yahoo,
    sign_in,
    toushin_calls,
    toushin_csv,
    toushin_page,
    yahoo_chart,
    yahoo_symbols,
)

APP_ID = "e5e2671a-b454-4e6f-aaaa-bbbbccccdddd"
ACCESS_KEY = "rakuten-access-key-987654"
JST = ZoneInfo("Asia/Tokyo")
NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def masker():
    return SecretMasker([])


@pytest.fixture
def yahoo(tmp_path, masker):
    connector = YahooFinanceConnector({}, masker, tmp_path / "state.json")
    connector.min_interval_seconds = 0
    return connector


@pytest.fixture
def toushin(tmp_path, masker):
    connector = ToushinLibConnector({}, masker, tmp_path / "state.json")
    connector.min_interval_seconds = 0
    return connector


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
    assert to_yahoo_symbol("7203") == "7203.T"
    assert to_yahoo_symbol("130a") == "130A.T"
    assert to_yahoo_symbol("7203.T") == "7203.T"
    assert to_yahoo_symbol("7203.JP") == "7203.T"
    assert to_yahoo_symbol("MSFT") == "MSFT"
    assert to_yahoo_symbol("aapl") == "AAPL"
    assert to_yahoo_symbol("MSFT.US") == "MSFT"
    # The code shape only picks the market to try first; the other one stays available as a fallback.
    assert symbol_candidates("7203") == [("jp", "7203.T"), ("us", "7203")]
    assert symbol_candidates("MSFT") == [("us", "MSFT"), ("jp", "MSFT.T")]
    assert symbol_candidates("MSFT.JP") == [("jp", "MSFT.T"), ("us", "MSFT")]
    for bad in ("../x", "", "1", "TOOLONG", "７２０３", "JPY=X"):
        with pytest.raises(ConnectorError):
            to_yahoo_symbol(bad)


def _settled(bars, **kwargs) -> httpx.Response:
    return httpx.Response(200, json=yahoo_chart(bars, **kwargs))


@respx.mock
async def test_yahoo_previous_close(yahoo):
    route = respx.get(url__startswith=CHART_URL).mock(
        return_value=_settled([("2026-09-22", 3020), ("2026-09-24", 3080)])
    )
    result = await yahoo.previous_close("7203", today=date(2026, 9, 25))
    assert result == {
        "code": "7203",
        "symbol": "7203.T",
        "market": "jp",
        "currency": "JPY",
        "date": "2026-09-24",
        "close": 3080.0,
        "source": "yahoo_finance",
        "valid_until": None,
    }
    sent = route.calls.last.request
    assert sent.url.path == "/v8/finance/chart/7203.T"
    assert (sent.url.params["range"], sent.url.params["interval"]) == ("1mo", "1d")
    # Yahoo answers 429 to the default httpx User-Agent, so the connector always names itself.
    assert sent.headers["user-agent"].startswith("life-helper/")
    assert yahoo.last_used() is not None


def test_yahoo_needs_no_key(yahoo, ctx):
    assert yahoo.configured
    assert get_connectors(ctx)["yahoo_finance"].configured
    assert "stooq" not in get_connectors(ctx)


@respx.mock
async def test_yahoo_previous_close_us_ticker(yahoo):
    route = mock_yahoo({"MSFT": 517.93})
    result = await yahoo.previous_close("MSFT", today=date(2026, 9, 25))
    assert (result["market"], result["symbol"], result["currency"], result["close"]) == ("us", "MSFT", "USD", 517.93)
    assert yahoo_symbols(route) == ["MSFT"]


@respx.mock
async def test_single_precision_noise_is_rounded(yahoo):
    mock_yahoo({"1306.T": 424.79998779296875, "JPY=X": 158.26499938964844})
    assert (await yahoo.previous_close("1306", today=date(2026, 9, 25)))["close"] == 424.8
    assert (await yahoo.usd_jpy(today=date(2026, 9, 25)))["rate"] == 158.265


@respx.mock
async def test_yahoo_falls_back_to_the_other_market(yahoo):
    route = mock_yahoo({"7203": 12.5})
    result = await yahoo.previous_close("7203", today=date(2026, 9, 25))
    assert (result["market"], result["symbol"], result["currency"]) == ("us", "7203", "USD")
    assert yahoo_symbols(route) == ["7203.T", "7203"]


@respx.mock
async def test_yahoo_reports_the_symbols_it_tried(yahoo):
    mock_yahoo({})
    with pytest.raises(ConnectorError) as e:
        await yahoo.previous_close("MSFT", today=date(2026, 9, 25))
    assert "MSFT と MSFT.T を照会しました" in str(e.value)


@respx.mock
async def test_yahoo_price_in_another_currency_is_not_used_for_that_market(yahoo):
    """A symbol that answers in an unexpected currency or exchange is some other instrument, not this market's."""
    route = respx.get(url__startswith=CHART_URL)
    route.mock(
        side_effect=[
            _settled([("2026-09-24", 25)], currency="USD", zone="Asia/Tokyo"),
            _settled([("2026-09-24", 12.5)], currency="USD", zone="Europe/London"),
        ]
    )
    with pytest.raises(ConnectorError, match="7203.T と 7203 を照会しましたが"):
        await yahoo.previous_close("7203", today=date(2026, 9, 25))


@respx.mock
async def test_funds_are_not_priced_as_stocks(yahoo):
    """US mutual funds (VFIAX) are not listed securities; funds are priced from the fund managers."""
    route = respx.get(url__startswith=CHART_URL)
    route.mock(
        side_effect=[
            _settled([("2026-09-24", 600)], currency="USD", zone="America/New_York", instrument="MUTUALFUND"),
            httpx.Response(404, json=YAHOO_NOT_FOUND),
        ]
    )
    with pytest.raises(ConnectorError, match="VFIAX と VFIAX.T を照会しましたが"):
        await yahoo.previous_close("VFIAX", today=date(2026, 9, 25))


@respx.mock
async def test_yahoo_usd_jpy(yahoo):
    route = mock_yahoo({"JPY=X": 150.25})
    assert await yahoo.usd_jpy(today=date(2026, 9, 25)) == {
        "pair": "USDJPY",
        "symbol": "JPY=X",
        "date": "2026-09-24",
        "rate": 150.25,
        "source": "yahoo_finance",
        "valid_until": None,
    }
    assert route.calls.last.request.url.path == "/v8/finance/chart/JPY=X"


@respx.mock
async def test_running_session_is_not_a_close(yahoo):
    """Today's bar only carries the latest trade until the session ends and the close settles."""
    session = (datetime(2026, 9, 25, 9, 0, tzinfo=JST), datetime(2026, 9, 25, 15, 30, tzinfo=JST))
    respx.get(url__startswith=CHART_URL).mock(
        return_value=_settled([("2026-09-24", 3000), ("2026-09-25", None), ("2026-09-25", 3100)], period=session)
    )
    for now in (datetime(2026, 9, 25, 11, 0, tzinfo=JST), datetime(2026, 9, 25, 15, 45, tzinfo=JST)):
        result = await yahoo.previous_close("7203", today=date(2026, 9, 25), now=now)
        assert (result["date"], result["close"]) == ("2026-09-24", 3000)
        # The answer is only good until the running session settles.
        assert datetime.fromisoformat(result["valid_until"]) == datetime(2026, 9, 25, 16, 0, tzinfo=JST)
    settled = await yahoo.previous_close("7203", today=date(2026, 9, 25), now=datetime(2026, 9, 25, 16, 5, tzinfo=JST))
    assert (settled["date"], settled["close"], settled["valid_until"]) == ("2026-09-25", 3100, None)


@respx.mock
async def test_closed_market_uses_the_last_session(yahoo):
    """On a weekend Yahoo reports the next session, which no bar belongs to yet."""
    monday = (datetime(2026, 9, 28, 9, 0, tzinfo=JST), datetime(2026, 9, 28, 15, 30, tzinfo=JST))
    respx.get(url__startswith=CHART_URL).mock(
        return_value=_settled([("2026-09-24", 3000), ("2026-09-25", 3100)], period=monday)
    )
    result = await yahoo.previous_close("7203", today=date(2026, 9, 26), now=datetime(2026, 9, 26, 10, tzinfo=JST))
    assert (result["date"], result["close"]) == ("2026-09-25", 3100)


@respx.mock
async def test_us_close_is_dated_in_new_york(yahoo):
    """A US session ends after midnight in Japan, but the close keeps the New York trading date."""
    session = (datetime(2026, 9, 24, 9, 30, tzinfo=NEW_YORK), datetime(2026, 9, 24, 16, 0, tzinfo=NEW_YORK))
    respx.get(url__startswith=CHART_URL).mock(
        return_value=_settled(
            [("2026-09-23", 337.02), ("2026-09-24", 335.92)],
            currency="USD",
            zone="America/New_York",
            hour=time(9, 30),
            period=session,
        )
    )
    early = await yahoo.previous_close("AAPL", today=date(2026, 9, 25), now=datetime(2026, 9, 25, 1, tzinfo=JST))
    assert (early["date"], early["close"]) == ("2026-09-23", 337.02)
    later = await yahoo.previous_close("AAPL", today=date(2026, 9, 25), now=datetime(2026, 9, 25, 7, tzinfo=JST))
    assert (later["date"], later["close"]) == ("2026-09-24", 335.92)


@respx.mock
async def test_fx_bar_is_dated_with_the_exchange_timezone_across_dst(yahoo):
    """Midnight bars in London fall on the previous UTC day during summer time; the offset of today must not be used."""
    summer_midnight = int(datetime(2026, 10, 23, 0, 0, tzinfo=ZoneInfo("Europe/London")).timestamp())
    chart = yahoo_chart([("2026-10-23", 157.5)], zone="Europe/London", instrument="CURRENCY")
    chart["chart"]["result"][0]["timestamp"] = [summer_midnight]
    chart["chart"]["result"][0]["meta"]["gmtoffset"] = 0  # winter time again when the chart is read
    respx.get(url__startswith=CHART_URL).mock(return_value=httpx.Response(200, json=chart))
    result = await yahoo.usd_jpy(today=date(2026, 10, 27), now=datetime(2026, 10, 27, 12, tzinfo=JST))
    assert (result["date"], result["rate"]) == ("2026-10-23", 157.5)


@respx.mock
async def test_bars_after_today_are_ignored(yahoo):
    respx.get(url__startswith=CHART_URL).mock(return_value=_settled([("2026-09-24", 3000), ("2026-09-25", 3100)]))
    result = await yahoo.previous_close("7203", today=date(2026, 9, 24))
    assert (result["date"], result["close"]) == ("2026-09-24", 3000)


@respx.mock
async def test_yahoo_failures_are_distinguished(yahoo):
    route = respx.get(url__startswith=CHART_URL)
    no_zone = yahoo_chart([("2026-09-24", 10)])
    del no_zone["chart"]["result"][0]["meta"]["exchangeTimezoneName"]
    no_period = yahoo_chart([("2026-09-25", 10)])
    del no_period["chart"]["result"][0]["meta"]["currentTradingPeriod"]
    half_period = yahoo_chart([("2026-09-25", 10)])
    del half_period["chart"]["result"][0]["meta"]["currentTradingPeriod"]["regular"]["end"]
    no_currency = yahoo_chart([("2026-09-24", 10)])
    del no_currency["chart"]["result"][0]["meta"]["currency"]
    uneven = yahoo_chart([("2026-09-24", 10)])
    uneven["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [10, 11]
    for response, message in (
        (httpx.Response(200, text="<html>maintenance</html>"), "想定外の応答"),
        (httpx.Response(200, json={"chart": {"result": None, "error": None}}), "想定外の応答"),
        (httpx.Response(200, json={"chart": {"result": None, "error": {"code": "Bad Request"}}}), "想定外の応答"),
        (httpx.Response(200, json=no_zone), "タイムゾーン"),
        # Without the trading period a running session cannot be told apart, so the answer is refused.
        (httpx.Response(200, json=no_period), "取引時間"),
        (httpx.Response(200, json=half_period), "取引時間"),
        (httpx.Response(200, json=no_currency), "通貨"),
        (httpx.Response(200, json=uneven), "価格の系列"),
        (_settled([("2026-09-24", 0)]), "株価が不正"),
        (_settled([("2026-09-24", "3000")]), "株価が不正"),
        (httpx.Response(503, text=""), "HTTP 503"),
    ):
        route.mock(return_value=response)
        with pytest.raises(ConnectorError, match=message):
            await yahoo.previous_close("7203", today=date(2026, 9, 25))
    route.mock(return_value=httpx.Response(429, text="Too Many Requests"))
    with pytest.raises(RateLimitedError, match="利用制限"):
        await yahoo.previous_close("7203", today=date(2026, 9, 25))
    # A broken answer or a rate limit must not be retried on the other market.
    assert set(yahoo_symbols(route)) == {"7203.T"}


@respx.mock
async def test_oversized_answer_is_refused(yahoo):
    respx.get(url__startswith=CHART_URL).mock(return_value=httpx.Response(200, content=b"x" * (MAX_CHART_BYTES + 1)))
    with pytest.raises(ConnectorError, match="想定より大きい"):
        await yahoo.previous_close("7203", today=date(2026, 9, 25))


@respx.mock
async def test_network_error_does_not_leak_url(yahoo):
    respx.get(url__startswith=CHART_URL).mock(side_effect=httpx.ConnectError(f"failed {CHART_URL}7203.T?x=1"))
    with pytest.raises(ConnectorError, match="接続できませんでした") as e:
        await yahoo.previous_close("7203")
    assert CHART_URL not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


async def test_connector_refuses_other_hosts(yahoo):
    with pytest.raises(ConnectorError):
        await yahoo.get("https://evil.example/", params={})
    with pytest.raises(ConnectorError):
        await yahoo.get("https://stooq.com/q/d/l/", params={})


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


def test_same_fund_name_needs_the_whole_official_name():
    assert same_fund_name("eMAXIS Slim 米国株式(S&P500)", SP500["fundNm"])
    # Brokers append the nickname to the official name; that is still the same fund.
    sbi = "楽天・シュワブ・高配当株式・米国ファンド(四半期決算型)(楽天・SCHD)"
    assert same_fund_name(sbi, SCHD["fundNm"], SCHD["fundNkNm"])
    assert not same_fund_name(sbi, SCHD_GROWTH["fundNm"], SCHD_GROWTH["fundNkNm"])
    # A part of the name, or the nickname alone, is only similar.
    assert not same_fund_name("eMAXIS Slim 米国株式", SP500["fundNm"])
    assert not same_fund_name("楽天・SCHD", SCHD["fundNm"], SCHD["fundNkNm"])
    assert not same_fund_name("", SP500["fundNm"])


def test_keyword_variants_drop_trailing_brackets_one_at_a_time():
    assert keyword_variants("楽天・シュワブ・高配当株式・米国ファンド(四半期決算型)(楽天・SCHD)") == [
        "楽天・シュワブ・高配当株式・米国ファンド(四半期決算型)(楽天・SCHD)",
        "楽天・シュワブ・高配当株式・米国ファンド(四半期決算型)",
        "楽天・シュワブ・高配当株式・米国ファンド",
    ]
    # Full-width brackets are the same brackets, and a name without any is searched once.
    assert keyword_variants("ｅＭＡＸＩＳ Ｓｌｉｍ米国株式（Ｓ＆Ｐ５００）") == [
        "eMAXIS Slim米国株式(S&P500)",
        "eMAXIS Slim米国株式",
    ]
    assert keyword_variants("ひふみプラス") == ["ひふみプラス"]
    assert keyword_variants("  ") == []


def test_toushin_isin_keeps_stray_codes_out_of_the_url(toushin):
    assert toushin.fund_code(" jp90c000gkc6 ") == "JP90C000GKC6"
    assert toushin.fund_code("ＪＰ９０Ｃ０００ＧＫＣ６") == "JP90C000GKC6"
    for bad in ("", "0331418A", "JP90C000GKC", "JP90C000GKC6X", "US0378331005", "JP90C000GKC6&associFundCd=1", "../x"):
        with pytest.raises(ConnectorError, match="ISIN"):
            toushin.fund_code(bad)


@respx.mock
async def test_toushin_fund_nav(toushin):
    route = mock_toushin({SP500["isinCd"]: (44_842, "2026年09月24日")})
    result = await toushin.fund_nav("JP90C000GKC6", today=date(2026, 9, 25))
    assert result == {
        "fund_code": "JP90C000GKC6",
        "name": "ｅＭＡＸＩＳ　Ｓｌｉｍ米国株式（Ｓ＆Ｐ５００）",
        "nav": 44_842.0,
        "price_unit": 10_000,
        "date": "2026-09-24",
        "source": "toushin_lib",
        "source_url": "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000?isinCd=JP90C000GKC6",
        "manager": "",
        "isin": "JP90C000GKC6",
        "association_code": "03311187",
    }
    (csv_call,) = toushin_calls(route, "/FdsWeb/FDST030000/csv-file-download")
    assert dict(csv_call.request.url.params) == {"isinCd": "JP90C000GKC6", "associFundCd": "03311187"}
    # The 協会コード confirmed on the fund page is remembered, so the next refresh only fetches the CSV.
    await toushin.fund_nav("JP90C000GKC6", today=date(2026, 9, 25))
    assert len(toushin_calls(route, "/FdsWeb/FDST030000")) == 1
    assert len(toushin_calls(route, "/FdsWeb/FDST030000/csv-file-download")) == 2
    assert toushin.configured and toushin.last_used() is not None


@respx.mock
async def test_toushin_only_trusts_the_association_code_of_the_isins_own_page(toushin):
    route = respx.route(url__startswith="https://toushin-lib.fwg.ne.jp")
    csv = httpx.Response(200, content=toushin_csv(25_341).encode("cp932"))
    for page, error in (
        # The page of another fund: its code would silently select that fund's CSV.
        (toushin_page(ALL_COUNTRY), "形式が想定と異なります"),
        # The requested ISIN appears on the page, but the download link is for another fund.
        (toushin_page(ALL_COUNTRY) + '<input type="hidden" value="JP90C000GKC6">', "形式が想定と異なります"),
        # Two different codes on the page: there is no telling which one is this fund's.
        (toushin_page(SP500) + 'associFundCd=0331418A"', "形式が想定と異なります"),
        (toushin_page(SP500).replace('id="associFundCd" value="03311187"', 'id="associFundCd" value="x"'), "形式"),
        # Two download links, even if one is for this ISIN.
        (
            toushin_page(SP500) + '<a href="csv-file-download?isinCd=JP90C000H1T1&amp;associFundCd=03311187">',
            "形式",
        ),
        (
            toushin_page(SP500).replace(
                "<title>ｅＭＡＸＩＳ　Ｓｌｉｍ米国株式（Ｓ＆Ｐ５００）</title>", "<title></title>"
            ),
            "形式",
        ),
        (toushin_page(None), "見つかりませんでした"),
    ):
        route.mock(side_effect=[httpx.Response(200, text=page), csv])
        with pytest.raises(ConnectorError, match=error):
            await toushin.fund_nav("JP90C000GKC6", today=date(2026, 9, 25))
        # The CSV was never asked for with a code the page did not confirm.
        assert all(c.request.url.path != "/FdsWeb/FDST030000/csv-file-download" for c in route.calls)
    assert toushin._identified == {}


@respx.mock
async def test_toushin_csv_failures_are_distinguished(toushin):
    toushin.min_interval_seconds = 0
    toushin._identified["JP90C000GKC6"] = ("03311187", SP500["fundNm"])
    route = respx.get(url__startswith="https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download")
    for response, message in (
        (httpx.Response(500, json={"statusCode": None}), "ライブラリーにありません"),
        (httpx.Response(200, json={"statusCode": None}), "CSV が返りませんでした"),
        (httpx.Response(503, text=""), "HTTP 503"),
        (httpx.Response(200, text="<!DOCTYPE html><html>maintenance</html>"), "CSV ではありません"),
        (httpx.Response(200, text=TOUSHIN_HEADER), "基準価額の行がありません"),
        (httpx.Response(200, content=f"{TOUSHIN_HEADER}\n2026年09月24日,0,1,,\n".encode("cp932")), "基準価額が不正"),
        (httpx.Response(200, content=f"{TOUSHIN_HEADER}\n2026年10月05日,44842,1,,\n".encode("cp932")), "未来の日付"),
    ):
        route.mock(return_value=response)
        with pytest.raises(ConnectorError, match=message):
            await toushin.fund_nav("JP90C000GKC6", today=date(2026, 9, 25))


@respx.mock
async def test_toushin_search_offers_candidates(toushin):
    toushin.min_interval_seconds = 0
    route = mock_toushin({})
    candidates = await toushin.search_funds("eMAXIS Slim 米国株式(S&P500)")
    assert [(c["fund_code"], c["exact"], c["score"]) for c in candidates] == [("JP90C000GKC6", True, 1.0)]
    assert candidates[0] | {"source_url": None} == {
        "provider": "toushin_lib",
        "provider_label": toushin.info.label,
        "manager": "三菱UFJアセットマネジメント",
        "fund_code": "JP90C000GKC6",
        "name": "ｅＭＡＸＩＳ　Ｓｌｉｍ米国株式（Ｓ＆Ｐ５００）",
        "nickname": "",
        "isin": "JP90C000GKC6",
        "association_code": "03311187",
        "price_unit": 10_000,
        "score": 1.0,
        "exact": True,
        "nav": 44_842.0,
        "date": "2026-09-24",
        "source_url": None,
    }
    body = json.loads(route.calls.last.request.content)
    # Without t_kensakuKbn the library ignores the keyword and answers with every fund.
    assert body == {"t_keyword": "eMAXIS Slim 米国株式(S&P500)", "t_kensakuKbn": "1", "startNo": 0, "draw": 1}

    # The broker name with the nickname appended finds nothing as is, so it is searched again without it.
    sbi = "楽天・シュワブ・高配当株式・米国ファンド(四半期決算型)(楽天・SCHD)"
    candidates = await toushin.search_funds(sbi)
    assert [(c["fund_code"], c["exact"]) for c in candidates] == [("JP90C000R6N1", True)]
    everything = await toushin.search_funds(sbi, exhaustive=True)
    # Every variant is searched when uniqueness matters, so the other share class is seen, but is not exact.
    assert [(c["fund_code"], c["exact"]) for c in everything] == [("JP90C000R6N1", True), ("JP90C000S073", False)]
    assert await toushin.search_funds("ひふみプラス") == []


@respx.mock
async def test_toushin_exhaustive_search_reads_every_page_or_refuses(toushin):
    toushin.min_interval_seconds = 0
    many = [
        SP500 | {"isinCd": f"JP90C{i:06d}0", "associFundCd": f"T{i:07d}", "fundNm": f"テストファンド{i}"}
        for i in range(45)
    ]
    route = mock_toushin({}, funds=many)
    assert len(await toushin.search_funds("テストファンド")) == 20
    assert len(route.calls) == 1
    assert len(await toushin.search_funds("テストファンド", exhaustive=True)) == 45
    assert [json.loads(c.request.content)["startNo"] for c in route.calls[1:]] == [0, 20, 40]
    too_many = many * 3
    toushin._searched.clear()
    mock_toushin({}, funds=[f | {"isinCd": f"JP91C{i:06d}0"} for i, f in enumerate(too_many)])
    with pytest.raises(TooManyFundsError, match="多すぎる"):
        await toushin.search_funds("テストファンド", exhaustive=True)


@respx.mock
async def test_toushin_search_failures_are_errors_not_empty_results(toushin):
    toushin.min_interval_seconds = 0
    route = respx.post("https://toushin-lib.fwg.ne.jp/FdsWeb/FDST999900/fundDataSearch")
    ok = {"statusCode": None, "searchResultInfo": {"recordsTotal": "1", "resultInfoMapList": [SP500]}}
    for response, message in (
        (httpx.Response(503, text=""), "HTTP 503"),
        (httpx.Response(200, text="<html>maintenance</html>"), "JSON ではありません"),
        (httpx.Response(200, json={"statusCode": None}), "検索結果の形式"),
        (
            httpx.Response(200, json={"searchResultInfo": {"recordsTotal": "x", "resultInfoMapList": []}}),
            "検索結果の形式",
        ),
    ):
        route.mock(return_value=response)
        with pytest.raises(ConnectorError, match=message):
            await toushin.search_funds("eMAXIS Slim 米国株式")
    # One ISIN answering with two 協会コード is a broken result, not two candidates.
    conflict = ok | {
        "searchResultInfo": {"recordsTotal": "2", "resultInfoMapList": [SP500, SP500 | {"associFundCd": "0331418A"}]}
    }
    route.mock(return_value=httpx.Response(200, json=conflict))
    with pytest.raises(ConnectorError, match="矛盾"):
        await toushin.search_funds("eMAXIS Slim 米国株式")
    # Records without a usable ISIN or name are skipped; a broken 協会コード or NAV is only left out, because
    # the NAV is fetched with the code the fund page confirms.
    broken = [
        SP500 | {"isinCd": "../x"},
        SP500 | {"fundNm": " "},
        SP500 | {"isinCd": "JP90C000H1T1", "associFundCd": "", "standardPrice": "-"},
    ]
    route.mock(
        return_value=httpx.Response(200, json={"searchResultInfo": {"recordsTotal": "3", "resultInfoMapList": broken}})
    )
    (only,) = await toushin.search_funds("eMAXIS Slim 米国株式")
    assert (only["fund_code"], only["association_code"], only["nav"], only["date"]) == (
        "JP90C000H1T1",
        None,
        None,
        None,
    )


@respx.mock
async def test_toushin_exhaustive_search_never_returns_a_partial_list(toushin):
    route = respx.post("https://toushin-lib.fwg.ne.jp/FdsWeb/FDST999900/fundDataSearch")
    others = [SP500 | {"isinCd": f"JP90C{i:06d}0", "fundNm": f"別のファンド{i}"} for i in range(25)]

    def page(items, total):
        return httpx.Response(200, json={"searchResultInfo": {"recordsTotal": str(total), "resultInfoMapList": items}})

    for pages in (
        # The total changes between pages.
        [page(others[:20], 25), page(others[20:], 26)],
        # A page stops short of the total.
        [page(others[:20], 25), page([], 25)],
        # A page repeats funds of the one before.
        [page(others[:20], 25), page(others[15:20], 25)],
        # More funds than the total.
        [page(others[:20], 21), page(others[20:], 21)],
        # A non-final page returns fewer rows than the fixed startNo offset expects, though the total stays put.
        [page(others[:15], 25)],
    ):
        route.mock(side_effect=pages)
        with pytest.raises(ConnectorError, match="途中で変わりました"):
            await toushin.search_funds("ひふみプラス", exhaustive=True)
    # A fund with the very name but no usable ISIN makes it unknowable whether the name is unique.
    route.mock(return_value=page([SP500, SP500 | {"isinCd": "", "associFundCd": "0331999Z"}], 2))
    with pytest.raises(ConnectorError, match="ISIN の不正"):
        await toushin.search_funds("eMAXIS Slim 米国株式(S&P500)", exhaustive=True)
    # With nothing wrong, the complete answer is kept for the day, so a name is not searched on every refresh.
    route.mock(return_value=page([SP500], 1))
    calls = len(route.calls)
    first = await toushin.search_funds("ひふみプラス", exhaustive=True)
    assert await toushin.search_funds("ひふみプラス", exhaustive=True) == first
    assert len(route.calls) == calls + 1


@respx.mock
async def test_toushin_network_error_is_reported_as_connector_error(toushin):
    respx.route(url__startswith="https://toushin-lib.fwg.ne.jp").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ConnectorError, match="接続できませんでした"):
        await toushin.fund_nav("JP90C000GKC6")
    with pytest.raises(ConnectorError, match="接続できませんでした"):
        await toushin.search_funds("eMAXIS Slim")


async def test_fund_connector_refuses_other_hosts(toushin):
    with pytest.raises(ConnectorError):
        await toushin.get("https://evil.example/FdsWeb/FDST030000", params={})
    with pytest.raises(ConnectorError):
        await toushin.post("https://evil.example/FdsWeb/FDST999900/fundDataSearch", body={})
    with pytest.raises(ConnectorError):
        await toushin.post("http://toushin-lib.fwg.ne.jp/FdsWeb/FDST999900/fundDataSearch", body={})


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
        # A file Python's csv module refuses is this fund's error, never an exception that aborts the refresh.
        (httpx.Response(200, text=f'{DAIWA_HEADER}\n20260924,"{"x" * 131_100}",0,1,0,0,0\n'), "読み取れませんでした"),
        (httpx.Response(200, content=b"x" * (MAX_CSV_BYTES + 1)), "想定より大きい"),
    ):
        route.mock(return_value=response)
        with pytest.raises(ConnectorError, match=message):
            await daiwa.fund_nav("3346", today=date(2026, 9, 25))


@respx.mock
async def test_fund_csv_reads_a_compressed_answer(daiwa):
    # The size cap reads the body itself, so a gzip answer must still arrive decompressed and keep its headers.
    body = gzip.compress(daiwa_csv(28_251).encode("cp932"))
    respx.get(url__startswith="https://www.daiwa-am.co.jp").mock(
        return_value=httpx.Response(
            200,
            content=body,
            headers={
                "content-encoding": "gzip",
                b"content-disposition": f'attachment; filename="{FANG_PLUS}.csv"'.encode("cp932"),
            },
        )
    )
    result = await daiwa.fund_nav("3346", today=date(2026, 9, 25))
    assert (result["nav"], result["name"]) == (28_251.0, FANG_PLUS)


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

    settings.rakuten_application_id = SecretStr(APP_ID)
    settings.rakuten_access_key = SecretStr(ACCESS_KEY)
    ctx.extras.pop("connectors", None)
    sign_in(client, ctx)
    body = client.get("/api/connectors").json()
    rakuten = next(c for c in body if c["name"] == "rakuten_travel")
    assert rakuten["configured"] is True
    assert APP_ID not in str(body) and ACCESS_KEY not in str(body)
    # Stock prices need no key, so the connector is always ready.
    yahoo = next(c for c in body if c["name"] == "yahoo_finance")
    assert yahoo["configured"] is True
    assert all(c["name"] != "stooq" for c in body)
