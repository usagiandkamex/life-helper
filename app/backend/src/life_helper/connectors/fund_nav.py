"""Fund NAV connectors: the daily 基準価額 of Japanese funds, from each manager's own public API or CSV.

Only officially published endpoints are used. Broker screens, private endpoints and URLs typed by the user are
never fetched: every connector knows its own host and URL shape. All connectors return the same result, so the
refresh service does not need to know which manager a fund belongs to.
"""

from __future__ import annotations

import csv
import io
import math
import re
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import unquote

from ..market.portfolio import DEFAULT_PRICE_UNIT, FundProvider
from .base import Connector, ConnectorError, ConnectorInfo

MAX_CANDIDATES = 20
MIN_SCORE = 0.6
# A NAV per price unit above this is not a real fund price; refusing it keeps unusable numbers out of the file.
MAX_NAV = 100_000_000
# Managers write the 基準日 in whichever of these shapes they prefer.
DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日")
MUFG_API = "https://developer.am.mufg.jp"
# The fund code shape tells the API which code was given; guessing is not possible, so it is derived here.
MUFG_CODE_TYPES = (("isin_cd", 12), ("association_fund_cd", 8), ("fund_cd", 6))
# Header names used by the official NAV history CSVs.
CSV_DATE_HEADERS = ("基準日", "年月日", "日付")
CSV_NAV_HEADERS = ("基準価額", "基準価格")
# The CSVs are Shift_JIS as often as UTF-8; UTF-8 is tried first because it is the one that fails loudly.
CSV_ENCODINGS = ("utf-8-sig", "cp932")
# A NAV history is long, but a fund page CSV that big is not one: it stops a hostile answer from filling memory.
MAX_CSV_BYTES = 4_000_000


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
    if not math.isfinite(value) or value <= 0 or value > MAX_NAV:
        raise ConnectorError(f"取得した{label}が不正です")
    return value


def nav_date(raw: object, *, latest: date, label: str = "基準日") -> str:
    day = parse_day(raw)
    if day is None:
        raise ConnectorError(f"取得した{label}が不正です")
    if day > latest:
        raise ConnectorError(f"取得した{label}が未来の日付です")
    return day.isoformat()


def parse_day(raw: object) -> date | None:
    """``2026/09/24``, ``20260924``, ``2026年9月24日`` and full-width digits all mean the same 基準日."""
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def header_key(cell: object) -> str:
    """``基準価額（円）`` and ``基準価額`` are the same column; managers annotate the unit differently."""
    return re.sub(r"[\s()\[\]]|円", "", unicodedata.normalize("NFKC", str(cell)).strip())


def attachment_name(disposition: str) -> str:
    """The fund name a manager puts in the CSV download name, or empty when it sends none.

    Only a name with Japanese characters is used: a file named after the code says nothing the user could
    check the link against, and showing it as the official name would be misleading.
    """
    encoded = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", disposition)
    if encoded:
        name = unquote(encoded.group(1))
    else:
        plain = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition)
        name = plain.group(1) if plain else ""
        try:
            # httpx decodes headers as latin-1, so a Shift_JIS file name has to be put back together.
            name = name.encode("latin-1").decode("cp932")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    name = re.sub(r"\.csv$", "", name.strip(), flags=re.IGNORECASE).strip()
    return "" if name.isascii() else name


def column_index(header: list[str], names: tuple[str, ...], label: str) -> int:
    """Finds a column by name, so a manager adding or reordering columns cannot shift the values read."""
    keys = [header_key(c) for c in header]
    for name in names:
        if name in keys:
            return keys.index(name)
    raise ConnectorError(f"公式 CSV に{label}の列が見つかりません")


def _is_header(row: list[str]) -> bool:
    keys = [header_key(c) for c in row]
    return any(n in keys for n in CSV_DATE_HEADERS) and any(n in keys for n in CSV_NAV_HEADERS)


def _csv_rows(text: str) -> list[list[str]]:
    """The non-empty rows of ``text``. A CSV Python cannot read is this fund's error, not the whole refresh's."""
    try:
        return [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
    except csv.Error:
        raise ConnectorError("公式 CSV を読み取れませんでした（形式が想定と異なります）") from None


def latest_csv_nav(text: str) -> tuple[date, str]:
    """The newest (基準日, 基準価額) of a NAV history CSV.

    The newest row is picked by date instead of by position: managers order the history differently, and a
    reversed file would otherwise value a holding with the oldest NAV in the history.
    """
    rows = _csv_rows(text)
    # The header is not always the first line: managers put a title or a note above it.
    header_at = next((i for i, row in enumerate(rows) if _is_header(row)), None)
    if header_at is None:
        raise ConnectorError("公式 CSV の形式が想定と異なります（基準日・基準価額の列が見つかりません）")
    date_at = column_index(rows[header_at], CSV_DATE_HEADERS, "基準日")
    nav_at = column_index(rows[header_at], CSV_NAV_HEADERS, "基準価額")
    found: tuple[date, str] | None = None
    for row in rows[header_at + 1 :]:
        if len(row) <= max(date_at, nav_at):
            continue
        day = parse_day(row[date_at])
        # Rows that do not start with a date are the notes and totals managers add around the history.
        if day is not None and row[nav_at].strip() and (found is None or day > found[0]):
            found = (day, row[nav_at])
    if found is None:
        raise ConnectorError("公式 CSV に基準価額の行がありません")
    return found


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


class FundCsvConnector(FundNavConnector):
    """Base for managers that publish their NAV history as an official CSV, one file per fund.

    Only the CSV a manager links from its own fund page is fetched, and the fund code is checked against the
    shape that manager uses, so nothing the user types can change which host or path is requested.
    """

    # The shape of the fund code the manager puts in the CSV URL. Also keeps a stray code out of the path.
    code_shape = re.compile(r"[0-9]{4,8}")

    def fund_code(self, raw: str) -> str:
        code = unicodedata.normalize("NFKC", str(raw)).strip()
        if not self.code_shape.fullmatch(code):
            raise ConnectorError(f"ファンドコードの形式が正しくありません: {raw}")
        return code

    def csv_url(self, code: str) -> str:
        """The official CSV of the NAV history of ``code``."""
        raise NotImplementedError

    def csv_params(self, code: str) -> dict[str, str]:
        return {}

    def page_url(self, code: str) -> str:
        """The official page the NAV comes from, shown as the source. Defaults to the CSV itself."""
        return self.csv_url(code)

    async def fund_nav(self, fund_code: str, *, today: date | None = None) -> dict:
        code = self.fund_code(fund_code)
        response = await self.get(self.csv_url(code), params=self.csv_params(code), max_bytes=MAX_CSV_BYTES)
        day, nav = latest_csv_nav(self._text(response))
        return {
            "fund_code": code,
            # The managers do not put the name in the CSV, so the download name is the only official name here.
            "name": attachment_name(response.headers.get("content-disposition", "")),
            "nav": nav_amount(nav),
            "price_unit": self.price_unit,
            "date": nav_date(day.isoformat(), latest=today or date.today()),
            "source": self.provider,
            "source_url": self.page_url(code),
            "manager": self.manager,
            "isin": None,
            "association_code": None,
        }

    async def search_funds(self, name: str) -> list[dict]:
        """Nothing: neither manager publishes a machine-readable fund list, so a name cannot resolve to a code.

        The user reads the code off the official fund page instead, which is a stronger confirmation than a
        name that merely looks similar.
        """
        return []

    def _text(self, response) -> str:
        if response.status_code == 404:
            raise FundNotFoundError(
                f"{self.manager} に該当する公式 CSV がありません（ファンドコードを確認してください）"
            )
        if response.status_code != 200:
            raise ConnectorError(f"{self.manager} から基準価額を取得できませんでした（HTTP {response.status_code}）")
        body = response.content
        for encoding in CSV_ENCODINGS:
            try:
                text = body.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ConnectorError(f"{self.manager} の公式 CSV の文字コードを判別できませんでした")
        if text.lstrip()[:16].lower().startswith(("<!doctype", "<html")):
            # A fund page instead of the CSV means the code is unknown, or the download moved.
            raise ConnectorError(f"{self.manager} から想定外の応答が返りました（CSV ではありません）")
        return text


class RakutenFundCsvConnector(FundCsvConnector):
    """楽天投信投資顧問 の基準価額 CSV (https://www.rakuten-toushin.co.jp/fund/nav/). No API key."""

    info = ConnectorInfo(
        name="rakuten_csv",
        label="楽天投信投資顧問 基準価額 CSV",
        hosts=("www.rakuten-toushin.co.jp",),
        secret_names=(),
        cost="無料（公式サイトの CSV。API キー不要）",
    )
    provider: FundProvider = "rakuten_csv"
    manager = "楽天投信投資顧問"
    # The CSV is filed under a 6-digit chart id, which is the number in the CSV link on the fund page.
    code_shape = re.compile(r"[0-9]{6}")

    def csv_url(self, code: str) -> str:
        return f"https://www.rakuten-toushin.co.jp/assets/csv/chart_{code}.csv"


class DaiwaFundCsvConnector(FundCsvConnector):
    """大和アセットマネジメント の基準価額 CSV (https://www.daiwa-am.co.jp/funds/). No API key."""

    info = ConnectorInfo(
        name="daiwa_csv",
        label="大和アセットマネジメント 基準価額 CSV",
        hosts=("www.daiwa-am.co.jp",),
        secret_names=(),
        cost="無料（公式サイトの CSV。API キー不要）",
    )
    provider: FundProvider = "daiwa_csv"
    manager = "大和アセットマネジメント"
    # The 4-digit code 大和 uses for a fund, the same one that appears in its fund page URL.
    code_shape = re.compile(r"[0-9]{4}")

    def csv_url(self, code: str) -> str:
        return "https://www.daiwa-am.co.jp/funds/detail/csv_out.php"

    def csv_params(self, code: str) -> dict[str, str]:
        # type=1 is the 基準価額 history; the CSV carries the whole history and takes no date range.
        return {"code": code, "type": "1"}

    def page_url(self, code: str) -> str:
        return f"https://www.daiwa-am.co.jp/funds/detail/{code}/detail_top.html"


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
    def code_field(fund_code: str) -> tuple[str, str]:
        """``0331418A`` -> ``("association_fund_cd", "0331418A")``. Also keeps an invalid code out of the URL."""
        code = fund_code.strip().upper()
        if not re.fullmatch(r"[0-9A-Z]+", code):
            raise ConnectorError(f"ファンドコードの形式が正しくありません: {fund_code}")
        for code_type, length in MUFG_CODE_TYPES:
            if len(code) == length and (code_type != "fund_cd" or code.isdigit()):
                return code_type, code
        raise ConnectorError(f"ファンドコードの形式が正しくありません: {fund_code}")

    @classmethod
    def code_path(cls, fund_code: str) -> str:
        code_type, code = cls.code_field(fund_code)
        return f"{code_type}/{code}"

    async def fund_nav(self, fund_code: str, *, today: date | None = None) -> dict:
        code_type, code = self.code_field(fund_code)
        path = f"/fund_information_latest/{code_type}/{code}"
        datasets = await self._datasets(path)
        if not datasets:
            raise FundNotFoundError(f"{fund_code} のファンド情報が見つかりませんでした")
        return self._nav(datasets[0], (code_type, code), MUFG_API + path, today or date.today())

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

    def _nav(self, data: dict, requested: tuple[str, str], source_url: str, today: date) -> dict:
        code_type, code = requested
        # Never value a holding with another fund's NAV: the code must come back in the field it was asked for.
        if str(data.get(code_type) or "").strip().upper() != code:
            raise ConnectorError(f"照会した {code} とは別のファンドの情報が返りました")
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
        if not isinstance(payload, dict):
            raise ConnectorError(f"{self.manager} から想定外の応答が返りました（JSON ではありません）")
        errors = payload.get("errors")
        if isinstance(errors, dict) and errors.get("count"):
            raise ConnectorError(f"{self.manager} の API がエラーを返しました")
        if not isinstance(payload.get("datasets"), list):
            raise ConnectorError(f"{self.manager} から想定外の応答が返りました（datasets がありません）")
        return [d for d in payload["datasets"] if isinstance(d, dict)]
