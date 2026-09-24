"""Fund NAV connectors: the daily 基準価額 of Japanese funds, from each manager's own public API or CSV.

Only officially published endpoints are used. Broker screens, private endpoints and URLs typed by the user are
never fetched: every connector knows its own host and URL shape. All connectors return the same result, so the
refresh service does not need to know which manager a fund belongs to.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher

from ..market.portfolio import DEFAULT_PRICE_UNIT, FundProvider
from .base import Connector, ConnectorError, ConnectorInfo

MAX_CANDIDATES = 20
MIN_SCORE = 0.6
MUFG_API = "https://developer.am.mufg.jp"
# The fund code shape tells the API which code was given; guessing is not possible, so it is derived here.
MUFG_CODE_TYPES = (("isin_cd", 12), ("association_fund_cd", 8), ("fund_cd", 6))


class FundNotFoundError(ConnectorError):
    """The provider answered, but has no fund for that code."""


def normalize_name(name: str) -> str:
    """Fund names differ between brokers and managers (ｅＭＡＸＩＳ vs eMAXIS, spaces, brackets)."""
    folded = unicodedata.normalize("NFKC", name).casefold()
    return re.sub(r"[\s()\[\]<>・,.'\"\-‐‑–—―_/]", "", folded)


def match_score(query: str, candidate: str) -> float:
    """How close two fund names are. Only used to offer candidates, never to link a fund on its own."""
    a, b = normalize_name(query), normalize_name(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def nav_amount(raw: object, *, label: str = "基準価額") -> float:
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        raise ConnectorError(f"取得した{label}が不正です") from None
    if not math.isfinite(value) or value <= 0:
        raise ConnectorError(f"取得した{label}が不正です")
    return value


def nav_date(raw: object, *, latest: date, label: str = "基準日") -> str:
    text = str(raw or "").strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            day = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue
    else:
        raise ConnectorError(f"取得した{label}が不正です")
    if day > latest:
        raise ConnectorError(f"取得した{label}が未来の日付です")
    return day.isoformat()


class FundNavConnector(Connector):
    """Common interface for the per-manager NAV sources."""

    provider: FundProvider
    manager: str
    # Japanese funds quote the NAV per 10,000 units; a manager that uses another unit overrides this.
    price_unit: float = DEFAULT_PRICE_UNIT

    async def fund_nav(self, fund_code: str, *, today: date | None = None) -> dict:
        """The latest NAV: ``fund_code``, ``name``, ``nav``, ``price_unit``, ``date``, ``source``, ``source_url``."""
        raise NotImplementedError

    async def search_funds(self, name: str) -> list[dict]:
        """Funds of this manager whose name is close to ``name``, as candidates for the user to confirm."""
        raise NotImplementedError


class MufgFundApiConnector(FundNavConnector):
    """三菱UFJアセットマネジメント 投信情報 API (https://www.am.mufg.jp/tool/webapi/). No API key."""

    info = ConnectorInfo(
        name="mufg_api",
        label="三菱UFJアセットマネジメント 投信情報 API（基準価額）",
        hosts=("developer.am.mufg.jp",),
        secret_names=(),
        cost="無料（API キー不要。利用規約への同意が必要）",
    )
    provider: FundProvider = "mufg_api"
    manager = "三菱UFJアセットマネジメント"
    min_interval_seconds = 1.0

    @staticmethod
    def code_path(fund_code: str) -> str:
        """``0331418A`` -> ``association_fund_cd/0331418A``. Also keeps the code out of the URL path if invalid."""
        code = fund_code.strip().upper()
        if not re.fullmatch(r"[0-9A-Z]+", code):
            raise ConnectorError(f"ファンドコードの形式が正しくありません: {fund_code}")
        for code_type, length in MUFG_CODE_TYPES:
            if len(code) == length and (code_type != "fund_cd" or code.isdigit()):
                return f"{code_type}/{code}"
        raise ConnectorError(f"ファンドコードの形式が正しくありません: {fund_code}")

    async def fund_nav(self, fund_code: str, *, today: date | None = None) -> dict:
        path = f"/fund_information_latest/{self.code_path(fund_code)}"
        datasets = await self._datasets(path)
        if not datasets:
            raise FundNotFoundError(f"{fund_code} のファンド情報が見つかりませんでした")
        return self._nav(datasets[0], fund_code, MUFG_API + path, today or date.today())

    async def search_funds(self, name: str) -> list[dict]:
        candidates = []
        for data in await self._datasets("/code_list"):
            fund_name = str(data.get("fund_name") or "").strip()
            code = self._code(data)
            score = match_score(name, fund_name)
            if fund_name and code and score >= MIN_SCORE:
                candidates.append(
                    {
                        "provider": self.provider,
                        "provider_label": self.info.label,
                        "manager": self.manager,
                        "fund_code": code,
                        "name": fund_name,
                        "isin": str(data.get("isin_cd") or "").strip() or None,
                        "association_code": str(data.get("association_fund_cd") or "").strip() or None,
                        "price_unit": self.price_unit,
                        "score": round(score, 3),
                    }
                )
        candidates.sort(key=lambda c: (-c["score"], c["name"]))
        return candidates[:MAX_CANDIDATES]

    @staticmethod
    def _code(data: dict) -> str:
        """協会コード first: it is the code brokers and the industry use, so it stays valid across providers."""
        for key in ("association_fund_cd", "fund_cd", "isin_cd"):
            code = str(data.get(key) or "").strip()
            if code:
                return code
        return ""

    def _nav(self, data: dict, requested: str, source_url: str, today: date) -> dict:
        known = {str(data.get(k) or "").strip().upper() for k in ("fund_cd", "isin_cd", "association_fund_cd")}
        if requested.strip().upper() not in known:
            # Never value a holding with another fund's NAV, whatever the API answered.
            raise ConnectorError(f"照会した {requested} とは別のファンドの情報が返りました")
        return {
            "fund_code": self._code(data),
            "name": str(data.get("fund_name") or "").strip(),
            "nav": nav_amount(data.get("nav")),
            "price_unit": self.price_unit,
            "date": nav_date(data.get("base_date"), latest=today),
            "source": self.provider,
            "source_url": source_url,
            "manager": self.manager,
            "isin": str(data.get("isin_cd") or "").strip() or None,
            "association_code": str(data.get("association_fund_cd") or "").strip() or None,
        }

    async def _datasets(self, path: str) -> list[dict]:
        response = await self.get(MUFG_API + path, params={})
        if response.status_code == 403:
            raise ConnectorError(
                f"{self.manager} の API に拒否されました（HTTP 403）。基準価額は公式サイトで確認して手入力してください"
            )
        if response.status_code != 200:
            raise ConnectorError(f"{self.manager} から基準価額を取得できませんでした（HTTP {response.status_code}）")
        try:
            payload = response.json()
        except ValueError:
            raise ConnectorError(f"{self.manager} から想定外の応答が返りました（JSON ではありません）") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("datasets"), list):
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if isinstance(errors, dict) and errors.get("count"):
                raise ConnectorError(f"{self.manager} の API がエラーを返しました")
            raise ConnectorError(f"{self.manager} から想定外の応答が返りました（datasets がありません）")
        return [d for d in payload["datasets"] if isinstance(d, dict)]
