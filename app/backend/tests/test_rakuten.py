"""Rakuten Web Service connector: every API of the app registration (Ichiba, Travel, Books, Kobo, GORA, Recipe)."""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import respx
from copilot import ToolInvocation
from pydantic import SecretStr, ValidationError

from life_helper.automation.locks import FileLock
from life_helper.config import Settings
from life_helper.connectors import base
from life_helper.connectors import rakuten as rakuten_module
from life_helper.connectors.base import ConnectorError
from life_helper.connectors.rakuten import DEFAULT_ENDPOINTS, RakutenConnector, parse_golf_plans
from life_helper.connectors.registry import build_tools, get_connectors
from life_helper.security import SecretMasker

APP_ID = "e5e2671a-b454-4e6f-aaaa-bbbbccccdddd"
ACCESS_KEY = "rakuten-access-key-987654"
HOST = "https://openapi.rakuten.co.jp"
ICHIBA = DEFAULT_ENDPOINTS["ichiba_item_search"]
BOOKS = DEFAULT_ENDPOINTS["books_total_search"]
KOBO = DEFAULT_ENDPOINTS["kobo_ebook_search"]
HOTELS = DEFAULT_ENDPOINTS["travel_keyword_hotel_search"]
GOLF_COURSES = DEFAULT_ENDPOINTS["gora_golf_course_search"]
GOLF_PLANS = DEFAULT_ENDPOINTS["gora_plan_search"]
RECIPE_LIST = DEFAULT_ENDPOINTS["recipe_category_list"]
RECIPE_RANKING = DEFAULT_ENDPOINTS["recipe_category_ranking"]


@pytest.fixture
def masker():
    return SecretMasker([])


@pytest.fixture
def rakuten(tmp_path, masker):
    connector = RakutenConnector(
        {"rakuten_application_id": APP_ID, "rakuten_access_key": ACCESS_KEY},
        masker,
        tmp_path / "state.json",
        referer="https://app.example",
    )
    connector.min_interval_seconds = 0
    return connector


def item(name: str, price: int, **extra) -> dict:
    return {
        "itemName": name,
        "itemPrice": price,
        "shopName": "テスト商店",
        "itemUrl": f"https://item.rakuten.co.jp/shop/{price}/",
        "reviewAverage": 4.5,
        "reviewCount": 12,
        "postageFlag": 0,
        "availability": 1,
        "pointRate": 1,
        "itemCaption": "説明" * 500,
        **extra,
    }


# -- 共通: キー、ヘッダー、エラー、エンドポイント ---------------------------------------------------------------


@respx.mock
async def test_items_sends_keys_headers_and_mapped_params(rakuten):
    route = respx.get(ICHIBA).mock(
        return_value=httpx.Response(200, json={"count": 123, "Items": [item("A", 2500), item("B", 1980)]})
    )
    result = await rakuten.search_items(
        keyword="ノートPC", min_price=1000, max_price=5000, sort="price_asc", exclude_keyword="中古"
    )
    params = route.calls.last.request.url.params
    assert (params["applicationId"], params["accessKey"]) == (APP_ID, ACCESS_KEY)
    assert (params["format"], params["formatVersion"]) == ("json", "2")
    assert (params["keyword"], params["sort"], params["minPrice"], params["maxPrice"]) == (
        "ノートPC",
        "+itemPrice",
        "1000",
        "5000",
    )
    assert (params["availability"], params["hits"], params["NGKeyword"]) == ("1", "10", "中古")
    assert "postageFlag" not in params  # only sent when the model asks for postage-included items
    headers = route.calls.last.request.headers
    assert (headers["referer"], headers["origin"]) == ("https://app.example", "https://app.example")
    assert result["signal"] == {"item_count": 123, "min_price": 1980}
    assert [i["name"] for i in result["items"]] == ["A", "B"]
    first = result["items"][0]
    assert first["postage_included"] is True and first["in_stock"] is True and first["url"].startswith("https://")
    assert "itemCaption" not in json.dumps(result) and APP_ID not in str(result) and ACCESS_KEY not in str(result)


@respx.mock
async def test_items_reads_format_version_1(rakuten):
    respx.get(ICHIBA).mock(
        return_value=httpx.Response(200, json={"count": 1, "Items": [{"Item": item("旧形式", 800, postageFlag=1)}]})
    )
    result = await rakuten.search_items(keyword="x", postage_included_only=True)
    assert result["items"][0]["name"] == "旧形式" and result["items"][0]["postage_included"] is False
    assert result["signal"] == {"item_count": 1, "min_price": 800}


@respx.mock
async def test_items_caps_results_and_long_names(rakuten):
    items = [item("商品" * 200, 100 + i) for i in range(30)]
    respx.get(ICHIBA).mock(return_value=httpx.Response(200, json={"count": 30, "Items": items}))
    result = await rakuten.search_items(keyword="x")
    assert len(result["items"]) == 10 and len(result["items"][0]["name"]) == 200


@respx.mock
async def test_not_found_means_no_results(rakuten):
    respx.get(ICHIBA).mock(return_value=httpx.Response(404, json={"error": "not_found", "error_description": "x"}))
    result = await rakuten.search_items(keyword="存在しない商品")
    assert result["items"] == [] and result["total_count"] == 0 and result["signal"] == {"item_count": 0}


NOT_FOUND_CASES = [
    (
        DEFAULT_ENDPOINTS["travel_vacant_hotel_search"],
        "search_vacancy",
        {"hotel_no": 1, "checkin": date(2026, 12, 30), "checkout": date(2026, 12, 31)},
        "plans",
    ),
    (HOTELS, "search_hotels", {"keyword": "品川"}, "hotels"),
    (BOOKS, "search_books", {"keyword": "x"}, "items"),
    (KOBO, "search_kobo", {"keyword": "x"}, "items"),
    (GOLF_COURSES, "search_golf_courses", {"keyword": "x"}, "courses"),
    (GOLF_PLANS, "search_golf_plans", {"play_date": date(2099, 1, 1), "area_code": 12}, "plans"),
    (RECIPE_RANKING, "recipe_ranking", {"category": "10"}, "recipes"),
]


@pytest.mark.parametrize(("endpoint", "method", "kwargs", "key"), NOT_FOUND_CASES)
async def test_every_api_treats_not_found_as_no_results(rakuten, endpoint, method, kwargs, key):
    with respx.mock:
        route = respx.get(endpoint).mock(return_value=httpx.Response(404, json={"error": "not_found"}))
        result = await getattr(rakuten, method)(**kwargs)
    assert route.called and result[key] == []
    assert all(value == 0 for value in result.get("signal", {}).values())


@respx.mock
async def test_documented_error_shape(rakuten):
    respx.get(ICHIBA).mock(
        return_value=httpx.Response(400, json={"error": "wrong_parameter", "error_description": "keyword is not valid"})
    )
    with pytest.raises(ConnectorError, match="市場の商品検索に失敗しました（wrong_parameter）"):
        await rakuten.search_items(keyword="x")


@respx.mock
async def test_gateway_error_shape_hints_at_the_registration(rakuten):
    body = {"errors": {"errorCode": 403, "errorMessage": "Invalid Access Key"}}
    respx.get(ICHIBA).mock(return_value=httpx.Response(403, json=body))
    with pytest.raises(ConnectorError, match="Invalid Access Key") as e:
        await rakuten.search_items(keyword="x")
    assert "許可された Web サイト" in str(e.value)


@pytest.mark.parametrize(
    "body",
    [
        {"errors": {"errorCode": 400, "errorMessage": f"Invalid Access Key {ACCESS_KEY}"}},
        {"errors": {"errorCode": 400, "errorMessage": f"bad request {ICHIBA}?accessKey={ACCESS_KEY}"}},
        {"errors": {"errorCode": 400, "errorMessage": "applicationId%3De5e2671a-b454-4e6f-aaaa-bbbbccccdddd"}},
        {"error": f"{HOST}/?applicationId={APP_ID}", "error_description": ACCESS_KEY},
    ],
)
async def test_error_messages_never_carry_keys_or_urls(rakuten, body):
    with respx.mock:
        respx.get(ICHIBA).mock(return_value=httpx.Response(400, json=body))
        with pytest.raises(ConnectorError) as e:
            await rakuten.search_items(keyword="x")
    message = str(e.value)
    assert ACCESS_KEY not in message and APP_ID not in message and "http" not in message and "%3D" not in message


@respx.mock
async def test_rate_limit_asks_to_wait(rakuten):
    respx.get(BOOKS).mock(return_value=httpx.Response(429, json={"error": "too_many_requests"}))
    with pytest.raises(ConnectorError, match="時間をおいて"):
        await rakuten.search_books(keyword="x")


@respx.mock
async def test_connection_error_hides_the_url_with_keys(rakuten):
    respx.get(ICHIBA).mock(side_effect=httpx.ConnectError(f"failed {ICHIBA}?accessKey={ACCESS_KEY}"))
    with pytest.raises(ConnectorError, match="接続できませんでした") as e:
        await rakuten.search_items(keyword="x")
    assert ACCESS_KEY not in str(e.value) and e.value.__cause__ is None


@respx.mock
async def test_non_json_answer(rakuten):
    respx.get(ICHIBA).mock(return_value=httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(ConnectorError, match="HTTP 502"):
        await rakuten.search_items(keyword="x")


async def test_requests_need_both_keys(tmp_path, masker):
    connector = RakutenConnector({"rakuten_application_id": APP_ID}, masker, tmp_path / "s.json")
    assert not connector.configured
    with pytest.raises(ConnectorError, match="API キーが登録されていません"):
        await connector.search_items(keyword="x")


async def test_every_api_shares_one_interval(rakuten, monkeypatch):
    """Rakuten limits requests per application ID, so different APIs must still be spaced out."""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    rakuten.min_interval_seconds = 5
    with respx.mock:
        respx.get(ICHIBA).mock(return_value=httpx.Response(200, json={"count": 0, "Items": []}))
        respx.get(BOOKS).mock(return_value=httpx.Response(200, json={"count": 0, "Items": []}))
        await rakuten.search_items(keyword="x")
        assert waits == []
        await rakuten.search_books(keyword="x")
    assert waits and all(0 < w <= 5 for w in waits)
    assert RakutenConnector.min_interval_seconds >= 1


async def test_interval_is_shared_with_other_processes(tmp_path, masker, monkeypatch):
    """The web app and the automation job are separate processes that share the data volume and the keys."""
    waits: list[float] = []
    clock = [1_000.0]

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(time, "time", lambda: clock[0])
    keys = {"rakuten_application_id": APP_ID, "rakuten_access_key": ACCESS_KEY}
    web, job = (RakutenConnector(keys, masker, tmp_path / "connectors.json") for _ in range(2))
    with respx.mock:
        respx.get(ICHIBA).mock(return_value=httpx.Response(200, json={"count": 0, "Items": []}))
        await web.search_items(keyword="x")
        assert waits == []
        clock[0] += 0.4
        await job.search_items(keyword="x")
    assert waits == [pytest.approx(web.min_interval_seconds - 0.4)]
    assert not (tmp_path / "locks" / "rakuten-throttle.lock").exists()


async def test_the_shared_timestamp_is_the_actual_send_time(tmp_path, masker, monkeypatch):
    """After its own interval wait, a reused connector records the moment it really sends, not an earlier one."""
    clock = [1_000.0]
    sent: list[float] = []
    fake_time = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    def slow_answer(request):
        sent.append(clock[0])
        clock[0] += 2  # the request takes 2 seconds
        return httpx.Response(200, json={"count": 0, "Items": []})

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(base, "time", fake_time)
    monkeypatch.setattr(rakuten_module, "time", fake_time)
    keys = {"rakuten_application_id": APP_ID, "rakuten_access_key": ACCESS_KEY}
    web, job = (RakutenConnector(keys, masker, tmp_path / "connectors.json") for _ in range(2))
    shared = tmp_path / "rakuten-throttle.json"
    with respx.mock:
        respx.get(ICHIBA).mock(side_effect=slow_answer)
        await web.search_items(keyword="x")
        await web.search_items(keyword="x")  # waits 1.5 s after the end of the first request
        assert json.loads(shared.read_text(encoding="utf-8"))["last_request_at"] == sent[-1]
        await job.search_items(keyword="x")
    assert sent == [1_000.0, 1_003.5, 1_005.5]
    assert all(b - a >= web.min_interval_seconds for a, b in itertools.pairwise(sent))


@respx.mock
async def test_the_lock_is_not_held_during_the_request(rakuten, tmp_path):
    """A slow answer must not block (or outlive the lock of) the other process: only start times are coordinated."""
    held: list[bool] = []

    def answer(request):
        held.append((tmp_path / "locks" / "rakuten-throttle.lock").exists())
        return httpx.Response(200, json={"count": 0, "Items": []})

    respx.get(ICHIBA).mock(side_effect=answer)
    await rakuten.search_items(keyword="x")
    assert held == [False]
    assert json.loads((tmp_path / "rakuten-throttle.json").read_text(encoding="utf-8"))["last_request_at"] > 0


@respx.mock
async def test_timestamp_write_failure_aborts_request_and_releases_lock(rakuten, tmp_path, monkeypatch):
    route = respx.get(ICHIBA).mock(return_value=httpx.Response(200, json={"count": 0, "Items": []}))

    def fail_write(path, content):
        raise OSError(f"cannot write {path}: {ACCESS_KEY}")

    with monkeypatch.context() as patch:
        patch.setattr(rakuten_module, "atomic_write", fail_write)
        with pytest.raises(ConnectorError, match="呼び出し間隔を記録できませんでした") as e:
            await rakuten.search_items(keyword="x")
    assert not route.called
    assert ACCESS_KEY not in str(e.value) and str(tmp_path) not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert not (tmp_path / "locks" / "rakuten-throttle.lock").exists()
    assert rakuten.last_used() is None

    await rakuten.search_items(keyword="x")
    assert route.call_count == 1
    assert json.loads((tmp_path / "rakuten-throttle.json").read_text(encoding="utf-8"))["last_request_at"] > 0


@respx.mock
async def test_busy_lock_is_reported(rakuten, tmp_path):
    other = FileLock(tmp_path / "locks" / "rakuten-throttle.lock", ttl_seconds=60)
    assert other.try_acquire()
    rakuten.lock_wait_seconds = 0.25
    route = respx.get(ICHIBA)
    with pytest.raises(ConnectorError, match="混み合っています"):
        await rakuten.search_items(keyword="x")
    assert not route.called
    other.release()


def test_last_used_falls_back_to_the_rakuten_travel_record(tmp_path, masker):
    state = tmp_path / "connectors.json"
    state.write_text(json.dumps({"rakuten_travel": {"last_used": "2026-09-01T00:00:00+00:00"}}), encoding="utf-8")
    connector = RakutenConnector({}, masker, state)
    assert connector.last_used() == "2026-09-01T00:00:00+00:00"
    connector._record_use()
    assert connector.last_used() != "2026-09-01T00:00:00+00:00"


@respx.mock
async def test_endpoint_override(tmp_path, masker):
    newer = f"{HOST}/ichibams/api/IchibaItem/Search/20270101"
    connector = RakutenConnector(
        {"rakuten_application_id": APP_ID, "rakuten_access_key": ACCESS_KEY},
        masker,
        tmp_path / "s.json",
        endpoints={"ichiba_item_search": newer},
    )
    assert connector.endpoints["books_total_search"] == BOOKS
    route = respx.get(newer).mock(return_value=httpx.Response(200, json={"count": 0, "Items": []}))
    await connector.search_items(keyword="x")
    assert route.called


@pytest.mark.parametrize(
    "endpoints",
    [
        {"ichiba_item_search": "https://evil.example/ichibams/api/IchibaItem/Search/20260701"},
        {"ichiba_item_search": "http://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"},
        {"ichiba_item_serch": ICHIBA},
    ],
)
def test_endpoint_override_is_checked(tmp_path, masker, endpoints):
    with pytest.raises(ValueError):
        RakutenConnector({}, masker, tmp_path / "s.json", endpoints=endpoints)


def test_endpoint_override_from_the_environment(monkeypatch, tmp_path):
    newer = f"{HOST}/services/api/BooksTotal/Search/20270101"
    monkeypatch.setenv("LH_RAKUTEN_ENDPOINTS", json.dumps({"books_total_search": newer}))
    settings = Settings(environment="development", data_dir=tmp_path)
    assert settings.rakuten_endpoint_overrides == {"books_total_search": newer}
    monkeypatch.setenv("LH_RAKUTEN_ENDPOINTS", "")
    assert Settings(environment="development", data_dir=tmp_path).rakuten_endpoint_overrides == {}


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        '["https://openapi.rakuten.co.jp/x"]',
        '{"ichiba_item_serch": "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20270101"}',
        '{"ichiba_item_search": "https://evil.example/ichibams/api/IchibaItem/Search/20270101"}',
    ],
)
def test_bad_endpoint_override_fails_at_startup(monkeypatch, tmp_path, value):
    monkeypatch.setenv("LH_RAKUTEN_ENDPOINTS", value)
    with pytest.raises(ValidationError, match="Rakuten|RAKUTEN"):
        Settings(environment="development", data_dir=tmp_path)


def test_endpoint_override_reaches_the_connector(ctx, settings):
    newer = f"{HOST}/services/api/Kobo/EbookSearch/20270101"
    settings.rakuten_endpoints = json.dumps({"kobo_ebook_search": newer})
    ctx.extras.pop("connectors", None)
    assert get_connectors(ctx)["rakuten"].endpoints["kobo_ebook_search"] == newer


# -- 楽天市場以外の検索 ---------------------------------------------------------------------------------------


@respx.mock
async def test_price_range_is_checked_before_calling(rakuten):
    route = respx.get(ICHIBA)
    with pytest.raises(ConnectorError, match="下限価格"):
        await rakuten.search_items(keyword="x", min_price=5000, max_price=1000)
    assert not route.called


HOTEL = {
    "hotelNo": 1234,
    "hotelName": "テストホテル",
    "address1": "東京都",
    "address2": "港区1-1",
    "hotelMinCharge": 9800,
}


@pytest.mark.parametrize(
    "payload",
    [
        # formatVersion 2: every hotel is a list of info objects.
        {
            "pagingInfo": {"recordCount": 42},
            "hotels": [[{"hotelBasicInfo": HOTEL}, {"hotelRatingInfo": {"serviceAverage": 4.2}}]],
        },
        # formatVersion 1: {"hotel": [...]} wrappers.
        {
            "pagingInfo": {"recordCount": 42},
            "hotels": [{"hotel": [{"hotelBasicInfo": HOTEL}, {"hotelRatingInfo": {"serviceAverage": 4.2}}]}],
        },
    ],
)
async def test_hotel_keyword_search(rakuten, payload):
    with respx.mock:
        route = respx.get(HOTELS).mock(return_value=httpx.Response(200, json=payload))
        result = await rakuten.search_hotels(keyword="品川 シーサイド")
    assert route.calls.last.request.url.params["keyword"] == "品川 シーサイド"
    assert result["total_count"] == 42
    assert result["hotels"] == [
        {
            "hotel_no": 1234,
            "name": "テストホテル",
            "address": "東京都港区1-1",
            "access": None,
            "nearest_station": None,
            "min_charge": 9800,
            "review_average": None,
            "review_count": None,
            "service_average": 4.2,
            "url": None,
            "plan_list_url": None,
        }
    ]


@respx.mock
async def test_books_search_by_isbn(rakuten):
    book = {
        "title": "テストの本",
        "author": "著者",
        "publisherName": "出版社",
        "salesDate": "2026年09月25日",
        "itemPrice": 1650,
        "isbn": "9784062938615",
        "itemUrl": "https://books.rakuten.co.jp/rb/1/",
    }
    route = respx.get(BOOKS).mock(return_value=httpx.Response(200, json={"count": 1, "Items": [book]}))
    result = await rakuten.search_books(isbn_jan="978-4-06-293861-5", sort="newest")
    params = route.calls.last.request.url.params
    assert (params["isbnjan"], params["sort"]) == ("9784062938615", "-releaseDate") and "keyword" not in params
    assert result["items"][0]["title"] == "テストの本" and result["items"][0]["isbn_jan"] == "9784062938615"
    assert result["signal"] == {"book_count": 1, "book_min_price": 1650}


async def test_books_search_needs_a_valid_query(rakuten):
    with pytest.raises(ConnectorError, match="キーワードか ISBN"):
        await rakuten.search_books()
    with pytest.raises(ConnectorError, match="13 桁"):
        await rakuten.search_books(isbn_jan="12345")


@respx.mock
async def test_kobo_search(rakuten):
    ebook = {"title": "電子書籍", "author": "著者", "itemPrice": 990, "itemUrl": "https://books.rakuten.co.jp/rk/1/"}
    route = respx.get(KOBO).mock(return_value=httpx.Response(200, json={"count": 5, "Items": [{"Item": ebook}]}))
    result = await rakuten.search_kobo(author="著者", sort="price_asc")
    params = route.calls.last.request.url.params
    assert (params["author"], params["sort"]) == ("著者", "+itemPrice") and "keyword" not in params
    assert result["items"][0]["title"] == "電子書籍" and result["signal"] == {"ebook_count": 5, "ebook_min_price": 990}
    with pytest.raises(ConnectorError, match="キーワード・タイトル・著者名"):
        await rakuten.search_kobo()


@respx.mock
async def test_golf_course_search(rakuten):
    course = {
        "golfCourseId": 80001,
        "golfCourseName": "テストカントリークラブ",
        "address": "千葉県",
        "evaluation": 4.1,
        "golfCourseDetailUrl": "https://gora.golf.rakuten.co.jp/guide/80001/",
    }
    route = respx.get(GOLF_COURSES).mock(return_value=httpx.Response(200, json={"count": 1, "Items": [course]}))
    result = await rakuten.search_golf_courses(area_code=12)
    params = route.calls.last.request.url.params
    assert (params["areaCode"], params["sort"]) == ("12", "rating") and "keyword" not in params
    assert result["courses"][0]["golf_course_id"] == 80001 and result["courses"][0]["url"].startswith("https://")
    with pytest.raises(ConnectorError, match="都道府県コード"):
        await rakuten.search_golf_courses()


def golf_plan(plan_id: int, status: int, price: int, stock_key: str = "callInfo") -> dict:
    return {
        "planId": plan_id,
        "planName": f"プラン{plan_id}",
        "price": price,
        "playerNumMin": 2,
        "playerNumMax": 4,
        "lunch": 1,
        stock_key: {
            "playDate": "2026-10-10",
            "stockStatus": status,
            "stockCount": 3,
            "reservePageUrlPC": f"https://gora.golf.rakuten.co.jp/r/{plan_id}",
        },
    }


@pytest.mark.parametrize("wrap", [False, True])
async def test_golf_plan_search_counts_available_plans(rakuten, wrap):
    plans = [golf_plan(1, 6, 9000), golf_plan(2, 1, 12000), golf_plan(3, 3, 8000)]
    course = {"golfCourseId": 80001, "golfCourseName": "テストCC", "planInfo": plans}
    if wrap:  # formatVersion 1
        course = {"Item": course | {"planInfo": [{"plan": p} for p in plans]}}
    with respx.mock:
        route = respx.get(GOLF_PLANS).mock(return_value=httpx.Response(200, json={"count": 1, "Items": [course]}))
        result = await rakuten.search_golf_plans(
            play_date=date(2026, 10, 10), golf_course_id=80001, lunch_included=True, today=date(2026, 9, 25)
        )
    params = route.calls.last.request.url.params
    assert (params["playDate"], params["golfCourseId"], params["planLunch"]) == ("2026-10-10", "80001", "1")
    assert result["signal"] == {"plan_count": 2} and result["plan_count"] == 2
    # Plans with a free slot come first; the waiting-list plan stays visible after them.
    assert [p["plan_id"] for p in result["plans"]] == [2, 3, 1]
    assert result["plans"][0]["stock_status"] == "空きあり（リクエスト予約可）"
    assert result["plans"][2]["available"] is False and result["plans"][2]["stock_status"] == "キャンセル待ち"
    assert result["plans"][0]["players"] == "2〜4 名" and result["plans"][0]["lunch_included"] is True
    assert result["plans"][0]["reserve_url"] == "https://gora.golf.rakuten.co.jp/r/2"


def test_golf_plans_also_read_the_documented_cal_info_name():
    payload = {"Items": [{"golfCourseId": 1, "planInfo": [golf_plan(7, 2, 5000, stock_key="calInfo")]}]}
    (plan,) = parse_golf_plans(payload)
    assert plan["available"] is True and plan["stock_status"] == "空きあり" and plan["stock_count"] == 3


async def test_golf_plan_search_checks_its_arguments(rakuten):
    with pytest.raises(ConnectorError, match="今日以降"):
        await rakuten.search_golf_plans(play_date=date(2026, 9, 24), area_code=12, today=date(2026, 9, 25))
    with pytest.raises(ConnectorError, match="ゴルフ場 ID"):
        await rakuten.search_golf_plans(play_date=date(2026, 9, 25), today=date(2026, 9, 25))


RECIPE_CATEGORIES = {
    "result": {
        "large": [
            {"categoryId": "30", "categoryName": "人気メニュー", "categoryUrl": "https://recipe.rakuten.co.jp/c/30/"},
            {"categoryId": "10", "categoryName": "肉", "categoryUrl": "https://recipe.rakuten.co.jp/c/10/"},
        ],
        "medium": [
            {"categoryId": 275, "categoryName": "鶏肉", "parentCategoryId": "10"},
            {"categoryId": 276, "categoryName": "豚肉", "parentCategoryId": "10"},
        ],
        "small": [
            {"categoryId": 516, "categoryName": "鶏むね肉", "parentCategoryId": "275"},
            {"categoryId": 517, "categoryName": "鶏肉料理その他", "parentCategoryId": "275"},
            {"categoryId": 999, "categoryName": "親のない小カテゴリ", "parentCategoryId": "9999"},
        ],
    }
}
RANKING = {
    "result": [
        {
            "rank": "1",
            "recipeTitle": "鶏むね肉のソテー",
            "recipeDescription": "簡単",
            "recipeMaterial": ["鶏むね肉", "塩"],
            "recipeIndication": "約15分",
            "recipeCost": "300円前後",
            "nickname": "作者",
            "recipeUrl": "https://recipe.rakuten.co.jp/recipe/1/",
        }
    ]
}


@respx.mock
async def test_recipe_ranking_by_category_name(rakuten):
    listing = respx.get(RECIPE_LIST).mock(return_value=httpx.Response(200, json=RECIPE_CATEGORIES))
    ranking = respx.get(RECIPE_RANKING).mock(return_value=httpx.Response(200, json=RANKING))
    result = await rakuten.recipe_ranking(category="鶏肉")
    # The exact name wins over the longer partial match, and the id joins the parents with hyphens.
    assert result["category"] == {"id": "10-275", "name": "鶏肉", "path": "肉 > 鶏肉"}
    assert result["other_categories"] == [{"id": "10-275-517", "path": "肉 > 鶏肉 > 鶏肉料理その他"}]
    assert ranking.calls.last.request.url.params["categoryId"] == "10-275"
    assert result["recipes"][0]["title"] == "鶏むね肉のソテー" and result["recipes"][0]["rank"] == 1
    assert result["recipes"][0]["materials"] == ["鶏むね肉", "塩"]

    result = await rakuten.recipe_ranking(category="むね")
    assert result["category"]["id"] == "10-275-516"
    assert listing.call_count == 1  # the category list is cached


@respx.mock
async def test_recipe_ranking_by_id_or_overall(rakuten):
    listing = respx.get(RECIPE_LIST)
    ranking = respx.get(RECIPE_RANKING).mock(return_value=httpx.Response(200, json=RANKING))
    result = await rakuten.recipe_ranking(category="10-275")
    assert ranking.calls.last.request.url.params["categoryId"] == "10-275" and result["category"] == {"id": "10-275"}
    await rakuten.recipe_ranking()
    assert "categoryId" not in ranking.calls.last.request.url.params
    assert not listing.called


@respx.mock
async def test_recipe_ranking_unknown_category(rakuten):
    respx.get(RECIPE_LIST).mock(return_value=httpx.Response(200, json=RECIPE_CATEGORIES))
    ranking = respx.get(RECIPE_RANKING)
    with pytest.raises(ConnectorError, match="見つかりませんでした"):
        await rakuten.recipe_ranking(category="宇宙食")
    assert not ranking.called


# -- ツール -------------------------------------------------------------------------------------------------


@pytest.fixture
def rakuten_tools(ctx, settings):
    settings.rakuten_application_id = SecretStr(APP_ID)
    settings.rakuten_access_key = SecretStr(ACCESS_KEY)
    ctx.extras.pop("connectors", None)
    get_connectors(ctx)["rakuten"].min_interval_seconds = 0
    return {spec.tool.name: spec.tool for spec in build_tools(ctx)}


@respx.mock
async def test_tool_returns_results_without_keys(rakuten_tools):
    route = respx.get(ICHIBA).mock(return_value=httpx.Response(200, json={"count": 1, "Items": [item("A", 500)]}))
    result = await rakuten_tools["search_rakuten_items"].handler(
        ToolInvocation(arguments={"keyword": "水", "sort": "price_asc"})
    )
    data = json.loads(result.text_result_for_llm)
    assert data["signal"] == {"item_count": 1, "min_price": 500}
    assert route.calls.last.request.url.params["sort"] == "+itemPrice"
    assert APP_ID not in result.text_result_for_llm and ACCESS_KEY not in result.text_result_for_llm


@respx.mock
async def test_tool_reports_connector_errors_as_results(rakuten_tools):
    respx.get(GOLF_PLANS)
    result = await rakuten_tools["search_rakuten_golf_plans"].handler(
        ToolInvocation(arguments={"play_date": "2000-01-01", "area_code": 12})
    )
    assert "今日以降" in json.loads(result.text_result_for_llm)["error"]


async def test_tool_rejects_unknown_sort(rakuten_tools):
    result = await rakuten_tools["search_rakuten_items"].handler(
        ToolInvocation(arguments={"keyword": "水", "sort": "+itemPrice"})
    )
    assert result.result_type == "failure"
