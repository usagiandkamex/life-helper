"""Rakuten Travel vacancy search connector (Rakuten Web Service; free).

Rakuten puts ``applicationId`` / ``accessKey`` in the query string, so the model must never call this API with
web_fetch; it only calls the ``search_rakuten_vacancy`` tool with key-free arguments.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlparse

from .base import Connector, ConnectorError, ConnectorInfo

DEFAULT_ENDPOINT = "https://openapi.rakuten.co.jp/engine/api/Travel/VacantHotelSearch/20170426"
MAX_PLANS = 10


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


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


class RakutenTravelConnector(Connector):
    info = ConnectorInfo(
        name="rakuten_travel",
        label="楽天トラベル空室検索",
        hosts=("openapi.rakuten.co.jp",),
        secret_names=("rakuten_application_id", "rakuten_access_key"),
        cost="無料（楽天ウェブサービスのアプリ登録が必要）",
    )
    min_interval_seconds = 1.5

    def __init__(self, *args, endpoint: str = DEFAULT_ENDPOINT, referer: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.endpoint = endpoint
        self.referer = referer
        if (urlparse(endpoint).hostname or "") not in self.info.hosts:
            raise ValueError("Rakuten endpoint host is not allowed")

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
        params: dict[str, Any] = {
            "applicationId": self.secret("rakuten_application_id"),
            "accessKey": self.secret("rakuten_access_key"),
            "format": "json",
            "formatVersion": 2,
            "hotelNo": hotel_no,
            "checkinDate": checkin.isoformat(),
            "checkoutDate": checkout.isoformat(),
            "adultNum": adults,
            "roomNum": rooms,
        }
        if max_charge:
            params["maxCharge"] = max_charge
        headers = {"Referer": self.referer, "Origin": self.referer.rstrip("/")} if self.referer else None
        response = await self.get(self.endpoint, params=params, headers=headers)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 404 and payload.get("error") == "not_found":
            plans: list[dict] = []
        elif response.status_code != 200:
            code = payload.get("error") or f"HTTP {response.status_code}"
            raise ConnectorError(f"楽天トラベルの検索に失敗しました（{code}）")
        else:
            plans = parse_vacancies(payload)
        return {
            "hotel_no": hotel_no,
            "checkin": checkin.isoformat(),
            "checkout": checkout.isoformat(),
            "adults": adults,
            "rooms": rooms,
            "vacant": bool(plans),
            "vacancy_count": len(plans),
            "plans": self.mask(plans[:MAX_PLANS]),
            "signal": {"vacancy_count": len(plans)},
            "note": "空室状況は確認時点のものです。予約は楽天トラベルのページで行ってください。",
        }
