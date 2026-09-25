"""Rakuten Web Service connector: Ichiba, Travel, Books, Kobo, GORA and Recipe (free; one app registration).

The app is registered with all six API scopes, so one ``applicationId`` / ``accessKey`` pair serves every API.
Rakuten limits requests per application ID, so every API shares one interval, also across the web app and the
automation job (a lock and a timestamp on the shared volume). Rakuten puts the keys in the query string, so the model
must never call these APIs with web_fetch or the browser; it only calls the rakuten tools with key-free arguments.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from ..automation.locks import FileLock, wait_acquire
from ..knowledge.store import atomic_write
from .base import Connector, ConnectorError, ConnectorInfo

BASE_URL = "https://openapi.rakuten.co.jp"
# Rakuten raises API versions from time to time: update these (LH_RAKUTEN_ENDPOINTS overrides them for local trials).
DEFAULT_ENDPOINTS: dict[str, str] = {
    "ichiba_item_search": f"{BASE_URL}/ichibams/api/IchibaItem/Search/20260701",
    "travel_vacant_hotel_search": f"{BASE_URL}/engine/api/Travel/VacantHotelSearch/20170426",
    "travel_keyword_hotel_search": f"{BASE_URL}/engine/api/Travel/KeywordHotelSearch/20260731",
    "books_total_search": f"{BASE_URL}/services/api/BooksTotal/Search/20170404",
    "kobo_ebook_search": f"{BASE_URL}/services/api/Kobo/EbookSearch/20170426",
    "gora_golf_course_search": f"{BASE_URL}/engine/api/Gora/GoraGolfCourseSearch/20170623",
    "gora_plan_search": f"{BASE_URL}/engine/api/Gora/GoraPlanSearch/20170623",
    "recipe_category_list": f"{BASE_URL}/recipems/api/Recipe/CategoryList/20170426",
    "recipe_category_ranking": f"{BASE_URL}/recipems/api/Recipe/CategoryRanking/20170426",
}
ALLOWED_HOSTS = ("openapi.rakuten.co.jp",)
LEGACY_NAME = "rakuten_travel"
MAX_RESULTS = 10
MAX_TEXT = 200
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
RECIPE_CATEGORY_TTL_SECONDS = 24 * 60 * 60
RECIPE_CATEGORY_ID = re.compile(r"\d+(-\d+){0,2}")
# Held only while waiting for the interval (at most min_interval_seconds), so a crashed holder frees it soon.
THROTTLE_LOCK_TTL_SECONDS = 30
# Only codes and plain messages from Rakuten are shown: nothing that could carry a URL or an encoded key.
SAFE_ERROR_CODE = re.compile(r"[a-z_]{1,40}")
SAFE_ERROR_MESSAGE = re.compile(r"[A-Za-z0-9 _.,:'()-]{1,120}")
JST = ZoneInfo("Asia/Tokyo")

ItemSort = Literal["standard", "price_asc", "price_desc", "review_count", "review_average", "newest"]
BookSort = Literal["standard", "sales", "newest", "oldest", "price_asc", "price_desc", "review_count", "review_average"]
KoboSort = Literal["standard", "newest", "oldest", "price_asc", "price_desc", "review_count", "review_average"]
GolfCourseSort = Literal[
    "rating",
    "reservation",
    "evaluation",
    "costperformance",
    "course",
    "facility",
    "meal",
    "staff",
    "beginner",
    "normal",
    "senior",
    "woman",
]
GolfPlanSort = Literal["reservation", "price", "evaluation", "costperformance"]

ITEM_SORTS: dict[str, str] = {
    "standard": "standard",
    "price_asc": "+itemPrice",
    "price_desc": "-itemPrice",
    "review_count": "-reviewCount",
    "review_average": "-reviewAverage",
    "newest": "-updateTimestamp",
}
BOOK_SORTS: dict[str, str] = {
    "standard": "standard",
    "sales": "sales",
    "newest": "-releaseDate",
    "oldest": "+releaseDate",
    "price_asc": "+itemPrice",
    "price_desc": "-itemPrice",
    "review_count": "reviewCount",
    "review_average": "reviewAverage",
}
KOBO_SORTS = {k: v for k, v in BOOK_SORTS.items() if k != "sales"}
GOLF_STOCK_STATUS = {
    1: "空きあり（リクエスト予約可）",
    2: "空きあり",
    3: "在庫あり（お得プラン）",
    4: "在庫あり（GORA 限定プラン）",
    5: "リクエスト予約のみ",
    6: "キャンセル待ち",
}
GOLF_AVAILABLE = {1, 2, 3, 4}
# formatVersion=1 wraps every entry in a single-key object; formatVersion=2 returns the entry itself.
WRAPPER_KEYS = ("Item", "item", "plan", "Plan")


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _unwrap(node: Any) -> dict:
    if isinstance(node, dict):
        for key in WRAPPER_KEYS:
            if len(node) == 1 and isinstance(node.get(key), dict):
                return node[key]
        return node
    return {}


def _records(payload: Any, *keys: str) -> list[dict]:
    """Entries of the first list found under ``keys``, in either response format version."""
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [entry for entry in (_unwrap(v) for v in value) if entry]
    return []


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return None
    return None


def _url(value: Any) -> str | None:
    return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None


def _total(payload: Any, fallback: int) -> int:
    if isinstance(payload, dict):
        for source in (payload, payload.get("pagingInfo")):
            if isinstance(source, dict):
                count = _number(source.get("count", source.get("recordCount")))
                if isinstance(count, int):
                    return count
    return fallback


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip().casefold()


def _price_signal(count_name: str, price_name: str, total: int, entries: list[dict]) -> dict:
    signal: dict[str, int | float] = {count_name: total}
    prices = [e["price"] for e in entries if isinstance(e.get("price"), (int, float))]
    if prices:
        signal[price_name] = min(prices)
    return signal


def check_endpoints(overrides: dict[str, Any]) -> dict[str, str]:
    """Every endpoint with ``overrides`` applied; raises ValueError for unknown names and non-Rakuten URLs."""
    unknown = sorted(set(overrides) - set(DEFAULT_ENDPOINTS))
    if unknown:
        raise ValueError(f"unknown Rakuten endpoints: {', '.join(unknown)}")
    endpoints = DEFAULT_ENDPOINTS | overrides
    for name, url in endpoints.items():
        parsed = urlparse(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != "https" or (parsed.hostname or "") not in ALLOWED_HOSTS:
            raise ValueError(f"Rakuten endpoint {name} must be https://{ALLOWED_HOSTS[0]}/…")
    return endpoints


def parse_vacancies(payload: dict) -> list[dict]:
    """Extracts hotel/plan/charge/reserve-URL entries from either response format version."""
    plans: list[dict] = []
    for hotel in payload.get("hotels", []) or []:
        hotel_info: dict = {}
        rooms: list[dict] = []
        for node in _walk(hotel):
            if "hotelBasicInfo" in node and isinstance(node["hotelBasicInfo"], dict):
                hotel_info = node["hotelBasicInfo"]
            if "roomInfo" in node and isinstance(node["roomInfo"], list):
                room: dict = {}
                for item in _walk(node["roomInfo"]):
                    if "roomBasicInfo" in item:
                        room = {"basic": item["roomBasicInfo"]}
                        rooms.append(room)
                    if "dailyCharge" in item and room:
                        room["charge"] = item["dailyCharge"]
        for room in rooms:
            basic, charge = room.get("basic", {}), room.get("charge", {})
            plans.append(
                {
                    "hotel_name": hotel_info.get("hotelName"),
                    "plan_name": basic.get("planName"),
                    "room_name": basic.get("roomName"),
                    "total_charge": charge.get("total") or charge.get("rakutenCharge"),
                    "reserve_url": basic.get("reserveUrl") or hotel_info.get("planListUrl"),
                }
            )
    return plans


def parse_hotels(payload: dict) -> list[dict]:
    hotels: list[dict] = []
    for hotel in payload.get("hotels", []) or []:
        basic: dict = {}
        rating: dict = {}
        for node in _walk(hotel):
            if isinstance(node.get("hotelBasicInfo"), dict) and not basic:
                basic = node["hotelBasicInfo"]
            if isinstance(node.get("hotelRatingInfo"), dict) and not rating:
                rating = node["hotelRatingInfo"]
        if not basic:
            continue
        address = "".join(str(basic.get(k) or "") for k in ("address1", "address2"))
        hotels.append(
            {
                "hotel_no": _number(basic.get("hotelNo")),
                "name": _text(basic.get("hotelName"), 100),
                "address": _text(address, 100),
                "access": _text(basic.get("access")),
                "nearest_station": _text(basic.get("nearestStation"), 50),
                "min_charge": _number(basic.get("hotelMinCharge")),
                "review_average": _number(basic.get("reviewAverage")),
                "review_count": _number(basic.get("reviewCount")),
                "service_average": _number(rating.get("serviceAverage")),
                "url": _url(basic.get("hotelInformationUrl")),
                "plan_list_url": _url(basic.get("planListUrl")),
            }
        )
    return hotels


def parse_golf_plans(payload: dict) -> list[dict]:
    plans: list[dict] = []
    for course in _records(payload, "Items", "items"):
        for plan in _records(course, "planInfo"):
            # JSON responses name the stock block "callInfo"; the XML documentation calls it <calInfo>.
            calendar = plan.get("callInfo", plan.get("calInfo"))
            if isinstance(calendar, list):
                calendar = next((_unwrap(c) for c in calendar if isinstance(c, dict)), {})
            if not isinstance(calendar, dict):
                calendar = {}
            status = _number(calendar.get("stockStatus"))
            plans.append(
                {
                    "golf_course_id": _number(course.get("golfCourseId")),
                    "golf_course_name": _text(course.get("golfCourseName"), 100),
                    "plan_id": _number(plan.get("planId")),
                    "plan_name": _text(plan.get("planName")),
                    "price": _number(plan.get("price")),
                    "players": f"{plan['playerNumMin']}〜{plan['playerNumMax']} 名"
                    if plan.get("playerNumMin") is not None and plan.get("playerNumMax") is not None
                    else None,
                    "start_time_zone": _text(plan.get("startTimeZone"), 20),
                    "lunch_included": plan.get("lunch") == 1 if plan.get("lunch") is not None else None,
                    "play_date": _text(calendar.get("playDate"), 20),
                    "stock_status": GOLF_STOCK_STATUS.get(status) if isinstance(status, int) else None,
                    "stock_count": _number(calendar.get("stockCount")),
                    "available": status in GOLF_AVAILABLE,
                    "reserve_url": _url(calendar.get("reservePageUrlPC")) or _url(course.get("reserveCalUrlPC")),
                }
            )
    return plans


def _error_code(payload: Any, status: int) -> str:
    fallback = f"HTTP {status}"
    if not isinstance(payload, dict):
        return fallback
    code = payload.get("error")
    if isinstance(code, str) and code:
        return code if SAFE_ERROR_CODE.fullmatch(code) else fallback
    # The API gateway answers authentication failures in its own shape, e.g. "Invalid Access Key".
    errors = payload.get("errors")
    if isinstance(errors, dict):
        message = str(errors.get("errorMessage") or errors.get("errorCode") or "").strip()
        if SAFE_ERROR_MESSAGE.fullmatch(message):
            return message
    return fallback


class RakutenConnector(Connector):
    info = ConnectorInfo(
        name="rakuten",
        label="楽天ウェブサービス",
        hosts=ALLOWED_HOSTS,
        secret_names=("rakuten_application_id", "rakuten_access_key"),
        cost="無料（楽天ウェブサービスのアプリ登録が必要。市場・トラベル・ブックス・Kobo・GORA・レシピ）",
    )
    # Rakuten allows about one request per second per application ID; every API shares this interval.
    min_interval_seconds = 1.5
    # How long a request waits while other processes (web app or automation job) wait for their own turn.
    lock_wait_seconds = 15.0

    def __init__(self, *args, endpoints: dict[str, str] | None = None, referer: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.endpoints = check_endpoints(dict(endpoints or {}))
        self.referer = referer
        self._recipe_categories: tuple[float, list[dict]] | None = None
        state_dir = self._state_path.parent
        self._throttle_path = state_dir / "rakuten-throttle.json"
        self._throttle_lock_path = state_dir / "locks" / "rakuten-throttle.lock"

    def last_used(self) -> str | None:
        # Shown in Settings; before the rename the same keys were recorded as the Rakuten Travel connector.
        used = super().last_used()
        if used:
            return used
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            return state.get(LEGACY_NAME, {}).get("last_used")
        except (OSError, ValueError, AttributeError):
            return None

    async def _before_send(self) -> None:
        """Starts this request at least ``min_interval_seconds`` after the last Rakuten request of any process that
        shares the data volume (web app and automation job). Only start times are coordinated: the lock is held for
        the wait alone, never during the request, so a slow response cannot outlive it."""
        lock = FileLock(self._throttle_lock_path, ttl_seconds=THROTTLE_LOCK_TTL_SECONDS)
        if not await wait_acquire(lock, self.lock_wait_seconds):
            raise ConnectorError("楽天ウェブサービスへの問い合わせが混み合っています。時間をおいてから試してください")
        try:
            try:
                last = float(json.loads(self._throttle_path.read_text(encoding="utf-8"))["last_request_at"])
            except (OSError, ValueError, KeyError, TypeError):
                last = 0.0
            wait = self.min_interval_seconds - (time.time() - last)
            if wait > 0:
                await asyncio.sleep(min(wait, self.min_interval_seconds))
            try:
                atomic_write(self._throttle_path, json.dumps({"last_request_at": time.time()}))
            except OSError:
                raise ConnectorError(
                    "楽天ウェブサービスの呼び出し間隔を記録できませんでした。時間をおいてから試してください"
                ) from None
        finally:
            lock.release()

    async def _call(self, api: str, what: str, params: dict[str, Any]) -> dict | None:
        """Returns the JSON body, or None when Rakuten answers 404 not_found (that means no results)."""
        query: dict[str, Any] = {
            "applicationId": self.secret("rakuten_application_id"),
            "accessKey": self.secret("rakuten_access_key"),
            "format": "json",
            "formatVersion": 2,
        }
        query |= {k: v for k, v in params.items() if v is not None and v != ""}
        headers = None
        if self.referer:
            parsed = urlparse(self.referer)
            headers = {"Referer": self.referer, "Origin": f"{parsed.scheme}://{parsed.netloc}"}
        response = await self.get(self.endpoints[api], params=query, headers=headers, max_bytes=MAX_RESPONSE_BYTES)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 404 and isinstance(payload, dict) and payload.get("error") == "not_found":
            return None
        if response.status_code != 200:
            raise ConnectorError(self._failure(what, response.status_code, payload))
        if not isinstance(payload, dict):
            raise ConnectorError(f"楽天{what}の応答を読み取れませんでした")
        return payload

    def _failure(self, what: str, status: int, payload: Any) -> str:
        code = self.mask(_error_code(payload, status))
        if status == 429 or code == "too_many_requests":
            return f"楽天{what}の利用回数の上限に達しました。時間をおいてから試してください"
        if status in (401, 403):
            return (
                f"楽天{what}に失敗しました（{code}）。アプリ ID・アクセスキーと、楽天に登録した"
                "「許可された Web サイト」がアプリの URL と一致しているかを確認してください"
            )
        if status == 503:
            return f"楽天{what}はメンテナンス中です（{code}）。時間をおいてから試してください"
        return f"楽天{what}に失敗しました（{code}）"

    # -- 楽天市場 ------------------------------------------------------------------------------------------

    async def search_items(
        self,
        *,
        keyword: str,
        min_price: int | None = None,
        max_price: int | None = None,
        sort: ItemSort = "standard",
        in_stock_only: bool = True,
        postage_included_only: bool = False,
        exclude_keyword: str | None = None,
        page: int = 1,
    ) -> dict:
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ConnectorError("下限価格は上限価格以下にしてください")
        payload = await self._call(
            "ichiba_item_search",
            "市場の商品検索",
            {
                "keyword": keyword,
                "minPrice": min_price,
                "maxPrice": max_price,
                "sort": ITEM_SORTS[sort],
                "availability": 1 if in_stock_only else 0,
                "postageFlag": 1 if postage_included_only else None,
                "NGKeyword": exclude_keyword,
                "hits": MAX_RESULTS,
                "page": page,
            },
        )
        items = [
            {
                "name": _text(r.get("itemName")),
                "price": _number(r.get("itemPrice")),
                "shop": _text(r.get("shopName"), 80),
                "postage_included": r.get("postageFlag") == 0 if r.get("postageFlag") is not None else None,
                "in_stock": r.get("availability") == 1 if r.get("availability") is not None else None,
                "review_average": _number(r.get("reviewAverage")),
                "review_count": _number(r.get("reviewCount")),
                "point_rate": _number(r.get("pointRate")),
                "url": _url(r.get("itemUrl")),
            }
            for r in _records(payload, "Items", "items")[:MAX_RESULTS]
        ]
        total = _total(payload, len(items))
        return {
            "keyword": keyword,
            "sort": sort,
            "page": page,
            "total_count": total,
            "items": self.mask(items),
            "signal": _price_signal("item_count", "min_price", total, items),
            "note": "価格・在庫は確認時点のものです。購入は楽天市場のページで行ってください。"
            "min_price はこのページ内の最安値です。",
        }

    # -- 楽天トラベル ------------------------------------------------------------------------------------------

    async def search_vacancy(
        self,
        *,
        hotel_no: int,
        checkin: date,
        checkout: date,
        adults: int = 2,
        rooms: int = 1,
        max_charge: int | None = None,
    ) -> dict:
        if checkout <= checkin:
            raise ConnectorError("チェックアウト日はチェックイン日より後にしてください")
        payload = await self._call(
            "travel_vacant_hotel_search",
            "トラベルの空室検索",
            {
                "hotelNo": hotel_no,
                "checkinDate": checkin.isoformat(),
                "checkoutDate": checkout.isoformat(),
                "adultNum": adults,
                "roomNum": rooms,
                "maxCharge": max_charge or None,
            },
        )
        plans = parse_vacancies(payload) if payload else []
        return {
            "hotel_no": hotel_no,
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
            "adults": adults,
            "rooms": rooms,
            "vacant": bool(plans),
            "vacancy_count": len(plans),
            "plans": self.mask(plans[:MAX_RESULTS]),
            "signal": {"vacancy_count": len(plans)},
            "note": "空室状況は確認時点のものです。予約は楽天トラベルのページで行ってください。",
        }

    async def search_hotels(self, *, keyword: str, page: int = 1) -> dict:
        payload = await self._call(
            "travel_keyword_hotel_search",
            "トラベルの施設検索",
            {"keyword": keyword, "hits": MAX_RESULTS, "page": page},
        )
        hotels = parse_hotels(payload)[:MAX_RESULTS] if payload else []
        return {
            "keyword": keyword,
            "page": page,
            "total_count": _total(payload, len(hotels)),
            "hotels": self.mask(hotels),
            "note": "hotel_no を search_rakuten_vacancy の施設番号に使えます。",
        }

    # -- 楽天ブックス・楽天Kobo ------------------------------------------------------------------------------

    async def search_books(
        self,
        *,
        keyword: str | None = None,
        isbn_jan: str | None = None,
        sort: BookSort = "standard",
        page: int = 1,
    ) -> dict:
        code = re.sub(r"[\s-]", "", isbn_jan or "")
        if code and not re.fullmatch(r"\d{13}", code):
            raise ConnectorError("ISBN・JAN コードは 13 桁の数字で指定してください")
        if not keyword and not code:
            raise ConnectorError("キーワードか ISBN・JAN コードを指定してください")
        payload = await self._call(
            "books_total_search",
            "ブックスの検索",
            {
                "keyword": keyword,
                "isbnjan": code or None,
                "sort": BOOK_SORTS[sort],
                "hits": MAX_RESULTS,
                "page": page,
            },
        )
        books = [
            {
                "title": _text(r.get("title")),
                "author": _text(r.get("author") or r.get("artistName"), 100),
                "publisher": _text(r.get("publisherName") or r.get("label"), 80),
                "sales_date": _text(r.get("salesDate"), 30),
                "price": _number(r.get("itemPrice")),
                "isbn_jan": _text(r.get("isbn") or r.get("jan"), 20),
                "review_average": _number(r.get("reviewAverage")),
                "review_count": _number(r.get("reviewCount")),
                "url": _url(r.get("itemUrl")),
            }
            for r in _records(payload, "Items", "items")[:MAX_RESULTS]
        ]
        total = _total(payload, len(books))
        return {
            "keyword": keyword,
            "isbn_jan": code or None,
            "sort": sort,
            "page": page,
            "total_count": total,
            "items": self.mask(books),
            "signal": _price_signal("book_count", "book_min_price", total, books),
            "note": "価格・在庫は確認時点のものです。購入は楽天ブックスのページで行ってください。",
        }

    async def search_kobo(
        self,
        *,
        keyword: str | None = None,
        title: str | None = None,
        author: str | None = None,
        sort: KoboSort = "standard",
        page: int = 1,
    ) -> dict:
        if not (keyword or title or author):
            raise ConnectorError("キーワード・タイトル・著者名のどれかを指定してください")
        payload = await self._call(
            "kobo_ebook_search",
            "Kobo の電子書籍検索",
            {
                "keyword": keyword,
                "title": title,
                "author": author,
                "sort": KOBO_SORTS[sort],
                "hits": MAX_RESULTS,
                "page": page,
            },
        )
        ebooks = [
            {
                "title": _text(r.get("title")),
                "author": _text(r.get("author"), 100),
                "publisher": _text(r.get("publisherName"), 80),
                "sales_date": _text(r.get("salesDate"), 30),
                "price": _number(r.get("itemPrice")),
                "review_average": _number(r.get("reviewAverage")),
                "review_count": _number(r.get("reviewCount")),
                "url": _url(r.get("itemUrl")),
            }
            for r in _records(payload, "Items", "items")[:MAX_RESULTS]
        ]
        total = _total(payload, len(ebooks))
        return {
            "keyword": keyword,
            "title": title,
            "author": author,
            "sort": sort,
            "page": page,
            "total_count": total,
            "items": self.mask(ebooks),
            "signal": _price_signal("ebook_count", "ebook_min_price", total, ebooks),
            "note": "価格は確認時点のものです。購入は楽天Kobo のページで行ってください。",
        }

    # -- 楽天GORA --------------------------------------------------------------------------------------------

    async def search_golf_courses(
        self,
        *,
        keyword: str | None = None,
        area_code: int | None = None,
        sort: GolfCourseSort = "rating",
        page: int = 1,
    ) -> dict:
        if not keyword and area_code is None:
            raise ConnectorError("キーワードか都道府県コードを指定してください")
        payload = await self._call(
            "gora_golf_course_search",
            "GORA のゴルフ場検索",
            {"keyword": keyword, "areaCode": area_code, "sort": sort, "hits": MAX_RESULTS, "page": page},
        )
        courses = [
            {
                "golf_course_id": _number(r.get("golfCourseId")),
                "name": _text(r.get("golfCourseName"), 100),
                "address": _text(r.get("address"), 100),
                "highway": _text(r.get("highway"), 80),
                "evaluation": _number(r.get("evaluation")),
                "caption": _text(r.get("golfCourseCaption"), 120),
                "url": _url(r.get("golfCourseDetailUrl")),
                "reserve_calendar_url": _url(r.get("reserveCalUrl")),
            }
            for r in _records(payload, "Items", "items")[:MAX_RESULTS]
        ]
        return {
            "keyword": keyword,
            "area_code": area_code,
            "sort": sort,
            "page": page,
            "total_count": _total(payload, len(courses)),
            "courses": self.mask(courses),
            "note": "golf_course_id を search_rakuten_golf_plans に使えます。",
        }

    async def search_golf_plans(
        self,
        *,
        play_date: date,
        golf_course_id: int | None = None,
        golf_course_name: str | None = None,
        area_code: int | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        lunch_included: bool = False,
        sort: GolfPlanSort = "reservation",
        page: int = 1,
        today: date | None = None,
    ) -> dict:
        if play_date < (today or datetime.now(JST).date()):
            raise ConnectorError("プレー日は今日以降の日付にしてください")
        if golf_course_id is None and not golf_course_name and area_code is None:
            raise ConnectorError("ゴルフ場 ID・ゴルフ場名・都道府県コードのどれかを指定してください")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ConnectorError("下限料金は上限料金以下にしてください")
        payload = await self._call(
            "gora_plan_search",
            "GORA のプラン検索",
            {
                "playDate": play_date.isoformat(),
                "golfCourseId": golf_course_id,
                "golfCourseName": golf_course_name,
                "areaCode": area_code,
                "minPrice": min_price,
                "maxPrice": max_price,
                "planLunch": 1 if lunch_included else None,
                "sort": sort,
                "hits": MAX_RESULTS,
                "page": page,
            },
        )
        plans = parse_golf_plans(payload) if payload else []
        available = [p for p in plans if p["available"]]
        shown = (available + [p for p in plans if not p["available"]])[:MAX_RESULTS]
        return {
            "play_date": play_date.isoformat(),
            "golf_course_id": golf_course_id,
            "golf_course_name": golf_course_name,
            "area_code": area_code,
            "page": page,
            "course_count": _total(payload, len({p["golf_course_id"] for p in plans})),
            "plan_count": len(available),
            "plans": self.mask(shown),
            "signal": {"plan_count": len(available)},
            "note": "空き状況は確認時点のものです（plan_count は空き・在庫のあるプラン数）。"
            "予約は楽天GORA のページで行ってください。",
        }

    # -- 楽天レシピ ------------------------------------------------------------------------------------------

    async def recipe_categories(self) -> list[dict]:
        """Every category with the id the ranking API expects: "10", "10-276" or "10-276-824"."""
        cached = self._recipe_categories
        if cached and time.monotonic() - cached[0] < RECIPE_CATEGORY_TTL_SECONDS:
            return cached[1]
        payload = await self._call("recipe_category_list", "レシピのカテゴリ一覧の取得", {})
        result = payload.get("result") if payload else None
        if not isinstance(result, dict):
            raise ConnectorError("楽天レシピのカテゴリ一覧を読み取れませんでした")

        def level(name: str) -> list[dict]:
            return [c for c in result.get(name) or [] if isinstance(c, dict) and c.get("categoryId") is not None]

        large_names = {str(c["categoryId"]): str(c.get("categoryName") or "") for c in level("large")}
        mediums = {str(c["categoryId"]): c for c in level("medium")}
        categories: list[dict] = [{"id": cid, "name": name, "path": name} for cid, name in large_names.items()]
        for mid, medium in mediums.items():
            parent = str(medium.get("parentCategoryId") or "")
            if parent in large_names:
                name = str(medium.get("categoryName") or "")
                categories.append({"id": f"{parent}-{mid}", "name": name, "path": f"{large_names[parent]} > {name}"})
        for small in level("small"):
            medium = mediums.get(str(small.get("parentCategoryId") or ""))
            parent = str(medium.get("parentCategoryId") or "") if medium else ""
            if medium and parent in large_names:
                name = str(small.get("categoryName") or "")
                categories.append(
                    {
                        "id": f"{parent}-{medium['categoryId']}-{small['categoryId']}",
                        "name": name,
                        "path": f"{large_names[parent]} > {medium.get('categoryName')} > {name}",
                    }
                )
        self._recipe_categories = (time.monotonic(), categories)
        return categories

    async def find_recipe_category(self, query: str) -> tuple[dict, list[dict]]:
        """The best category for ``query`` (exact name first, then the shortest partial match) and other matches."""
        wanted = _normalize(query)
        categories = await self.recipe_categories()
        # Broader categories (fewer "-" in the id) win among names that match equally well.
        ranked = sorted(
            (c for c in categories if wanted and wanted in _normalize(c["name"])),
            key=lambda c: (_normalize(c["name"]) != wanted, len(c["name"]), c["id"].count("-")),
        )
        if not ranked:
            raise ConnectorError(
                f"楽天レシピのカテゴリ「{_text(query, 50)}」が見つかりませんでした。別の言葉で試してください"
            )
        return ranked[0], ranked[1:MAX_RESULTS]

    async def recipe_ranking(self, *, category: str | None = None) -> dict:
        wanted = (category or "").strip()
        matched: dict | None = None
        others: list[dict] = []
        if wanted and not RECIPE_CATEGORY_ID.fullmatch(wanted):
            matched, others = await self.find_recipe_category(wanted)
            category_id: str | None = matched["id"]
        else:
            category_id = wanted or None
        payload = await self._call("recipe_category_ranking", "レシピのランキング取得", {"categoryId": category_id})
        recipes = [
            {
                "rank": _number(r.get("rank")),
                "title": _text(r.get("recipeTitle"), 100),
                "description": _text(r.get("recipeDescription")),
                "materials": [m for m in (_text(x, 40) for x in (r.get("recipeMaterial") or [])[:20]) if m]
                if isinstance(r.get("recipeMaterial"), list)
                else [],
                "time": _text(r.get("recipeIndication"), 30),
                "cost": _text(r.get("recipeCost"), 30),
                "author": _text(r.get("nickname"), 50),
                "url": _url(r.get("recipeUrl")),
            }
            for r in _records(payload, "result")[:MAX_RESULTS]
        ]
        return self.mask(
            {
                "category": {"id": category_id, "name": matched["name"], "path": matched["path"]}
                if matched
                else {"id": category_id},
                "other_categories": [{"id": c["id"], "path": c["path"]} for c in others],
                "recipes": recipes,
                "note": "楽天レシピのカテゴリ別ランキングです（上位のみ）。作り方はレシピのページで確認してください。",
            }
        )
