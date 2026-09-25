"""Fund NAV connectors: the daily 基準価額 of Japanese funds, from public official sources.

The main source is the fund library of the Investment Trusts Association, Japan (投資信託協会), which covers
every manager. The CSVs of two managers are kept as a fallback. Broker screens, private endpoints and URLs typed
by the user are never fetched: every connector knows its own host and URL shape. All connectors return the same
result, so the refresh service does not need to know where a NAV came from.
"""

from __future__ import annotations

import csv
import html
import io
import math
import re
import unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qs, unquote, urlencode

from ..market.clock import market_today
from ..market.portfolio import DEFAULT_PRICE_UNIT, ISIN_SHAPE, FundProvider
from .base import Connector, ConnectorError, ConnectorInfo

MAX_CANDIDATES = 20
MIN_SCORE = 0.6
# A NAV per price unit above this is not a real fund price; refusing it keeps unusable numbers out of the file.
MAX_NAV = 100_000_000
# Managers write the 基準日 in whichever of these shapes they prefer; the library search adds a time of day.
DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y-%m-%d %H:%M:%S")
# Header names used by the official NAV history CSVs.
CSV_DATE_HEADERS = ("基準日", "年月日", "日付")
CSV_NAV_HEADERS = ("基準価額", "基準価格")
# The CSVs are Shift_JIS as often as UTF-8; UTF-8 is tried first because it is the one that fails loudly.
CSV_ENCODINGS = ("utf-8-sig", "cp932")
# A NAV history is long, but a fund page CSV that big is not one: it stops a hostile answer from filling memory.
MAX_CSV_BYTES = 4_000_000

TOUSHIN_LIB = "https://toushin-lib.fwg.ne.jp"
TOUSHIN_SEARCH_URL = f"{TOUSHIN_LIB}/FdsWeb/FDST999900/fundDataSearch"
TOUSHIN_DETAIL_URL = f"{TOUSHIN_LIB}/FdsWeb/FDST030000"
TOUSHIN_CSV_URL = f"{TOUSHIN_LIB}/FdsWeb/FDST030000/csv-file-download"
ASSOCIATION_CODE_SHAPE = re.compile(r"[0-9A-Z]{8}")
# The library answers 20 funds per page. Five pages is far more than one fund name matches, so a keyword that
# goes past it is too vague to decide anything on.
SEARCH_PAGE_SIZE = 20
MAX_SEARCH_PAGES = 5
MAX_SEARCH_BYTES = 2_000_000
MAX_DETAIL_BYTES = 1_000_000
# Brokers append the nickname or share class in brackets, e.g. "…(四半期決算型)(楽天・SCHD)", which the library
# search does not find; the name is searched again with those groups removed, at most this many times.
MAX_KEYWORD_VARIANTS = 3
TRAILING_BRACKET = re.compile(r"\s*[(\[《【「][^()\[\]《》【】「」]*[)\]》】」]\s*$")


class FundNotFoundError(ConnectorError):
    """The provider answered, but has no fund for that code."""


class TooManyFundsError(ConnectorError):
    """The search matched more funds than can be checked, so it cannot tell whether a name is unique."""


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


def same_fund_name(query: str, name: str, nickname: str = "") -> bool:
    """True when ``query`` is the official name, or the official name followed by the nickname, apart from notation.

    Brokers write "楽天・シュワブ・高配当株式・米国ファンド(四半期決算型)(楽天・SCHD)" for the official name
    "楽天・シュワブ・高配当株式・米国ファンド（四半期決算型）" with the nickname "楽天・ＳＣＨＤ".
    """
    wanted = normalize_name(query)
    names = {normalize_name(name)} | ({normalize_name(name + nickname)} if nickname else set())
    return bool(wanted) and wanted in names


def keyword_variants(name: str) -> list[str]:
    """``name``, then ``name`` without its trailing bracket groups one at a time, for a search not fuzzy enough."""
    variants: list[str] = []
    current = unicodedata.normalize("NFKC", name).strip()
    while current and len(variants) < MAX_KEYWORD_VARIANTS:
        variants.append(current)
        stripped = TRAILING_BRACKET.sub("", current).strip()
        if stripped == current:
            break
        current = stripped
    return variants


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

    async def search_funds(self, name: str, *, exhaustive: bool = False) -> list[dict]:
        """Funds whose name is close to ``name``, as candidates for the user to confirm.

        Each candidate says whether it is ``exact`` (the same name apart from notation). With ``exhaustive``,
        every match is returned, or ``TooManyFundsError`` is raised, so a caller can rely on uniqueness.
        """
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

    async def search_funds(self, name: str, *, exhaustive: bool = False) -> list[dict]:
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


class ToushinLibConnector(FundCsvConnector):
    """投資信託協会 投信総合検索ライブラリー (https://toushin-lib.fwg.ne.jp/). Every manager's funds, no API key.

    A fund is identified by its ISIN. The NAV CSV also needs the 協会コード, and answers with whichever fund that
    code names even when it belongs to another ISIN, so the code is always read from the fund page of the ISIN
    itself (and remembered only once confirmed) instead of being taken from a search result or the saved link.
    """

    info = ConnectorInfo(
        name="toushin_lib",
        label="投資信託協会 投信総合検索ライブラリー（基準価額）",
        hosts=("toushin-lib.fwg.ne.jp",),
        secret_names=(),
        cost="無料（投資信託協会の公開サイト。API キー不要）",
    )
    provider: FundProvider = "toushin_lib"
    manager = "投資信託協会"
    code_shape = ISIN_SHAPE

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # ISIN -> (協会コード, official name), only ever filled from the ISIN's own fund page.
        self._identified: dict[str, tuple[str, str]] = {}
        # (normalized name, day) -> candidates of a complete search, so an unmatched name costs one search a day.
        self._searched: dict[tuple[str, date], list[dict]] = {}

    def fund_code(self, raw: str) -> str:
        code = unicodedata.normalize("NFKC", str(raw)).strip().upper()
        if not self.code_shape.fullmatch(code):
            raise ConnectorError(f"ISIN コードの形式が正しくありません（JP で始まる 12 桁）: {raw}")
        return code

    def page_url(self, code: str) -> str:
        return f"{TOUSHIN_DETAIL_URL}?{urlencode({'isinCd': code})}"

    async def fund_nav(self, fund_code: str, *, today: date | None = None) -> dict:
        isin = self.fund_code(fund_code)
        association_code, name = await self.identify(isin)
        response = await self.get(
            TOUSHIN_CSV_URL, params={"isinCd": isin, "associFundCd": association_code}, max_bytes=MAX_CSV_BYTES
        )
        if response.status_code == 500:
            raise FundNotFoundError(f"{isin} の基準価額が{self.manager}のライブラリーにありません")
        text = self._text(response)
        if text.lstrip().startswith("{"):
            # The library answers {"statusCode":null} instead of a CSV when it does not know the pair.
            raise ConnectorError(f"{self.manager}から基準価額の CSV が返りませんでした（ISIN を確認してください）")
        day, nav = latest_csv_nav(text)
        return {
            "fund_code": isin,
            "name": name,
            "nav": nav_amount(nav),
            "price_unit": self.price_unit,
            "date": nav_date(day.isoformat(), latest=today or market_today()),
            "source": self.provider,
            "source_url": self.page_url(isin),
            # The fund page names the manager only in free text, so it is left to the search result.
            "manager": "",
            "isin": isin,
            "association_code": association_code,
        }

    async def identify(self, isin: str) -> tuple[str, str]:
        """(協会コード, official name) of ``isin``, read from its fund page and checked to belong to it.

        The code is taken from the page's own CSV download link, which names the ISIN and the code together, and
        only when that link is for ``isin`` and every other 協会コード on the page agrees with it.
        """
        isin = self.fund_code(isin)
        if isin in self._identified:
            return self._identified[isin]
        response = await self.get(TOUSHIN_DETAIL_URL, params={"isinCd": isin}, max_bytes=MAX_DETAIL_BYTES)
        if response.status_code != 200:
            raise ConnectorError(f"{self.manager}のファンドページを取得できませんでした（HTTP {response.status_code}）")
        page = html.unescape(response.content.decode("utf-8", errors="replace"))
        title = re.search(r"<title>(.*?)</title>", page, re.DOTALL | re.IGNORECASE)
        name = title.group(1).strip() if title else ""
        pairs = set()
        for query in re.findall(r"csv-file-download\?([^\"'\s<>]+)", page):
            params = parse_qs(query)
            pairs.add((tuple(params.get("isinCd", ())), tuple(params.get("associFundCd", ()))))
        codes = set(re.findall(r"associFundCd=([0-9A-Za-z]+)", page))
        codes |= set(re.findall(r"""associFundCd["']\s+value=["']([^"']*)["']""", page))
        if not name and not pairs and not codes:
            raise FundNotFoundError(f"{isin} のファンドが{self.manager}のライブラリーに見つかりませんでした")
        if len(pairs) == 1:
            ((isins, found),) = pairs
            code = found[0] if len(isins) == 1 and len(found) == 1 and isins[0] == isin else ""
        else:
            code = ""
        if not name or not ASSOCIATION_CODE_SHAPE.fullmatch(code) or codes != {code}:
            raise ConnectorError(
                f"{self.manager}のファンドページの形式が想定と異なります（協会コードを確認できません）"
            )
        self._identified[isin] = (code, name)
        return self._identified[isin]

    async def search_funds(self, name: str, *, exhaustive: bool = False) -> list[dict]:
        key = (normalize_name(name), market_today())
        if exhaustive and key in self._searched:
            return list(self._searched[key])
        found: dict[str, dict] = {}
        for keyword in keyword_variants(name):
            for record in await self._search(keyword, exhaustive=exhaustive):
                candidate = self._candidate(record, name)
                if candidate is None:
                    # A record that has the name but no usable ISIN makes "exactly one such fund" unknowable.
                    if exhaustive and same_fund_name(name, _field(record, "fundNm"), _field(record, "fundNkNm")):
                        raise ConnectorError(f"{self.manager}の検索結果に ISIN の不正なファンドがあります")
                    continue
                seen = found.get(candidate["isin"])
                codes = {seen and seen["association_code"], candidate["association_code"]} - {None}
                if seen and len(codes) > 1:
                    raise ConnectorError(f"{self.manager}の検索結果に矛盾があります（同じ ISIN に別の協会コード）")
                found[candidate["isin"]] = candidate
            # A wider keyword only adds similar names; it is skipped when this one already found funds to choose.
            if found and not exhaustive:
                break
        candidates = sorted(found.values(), key=lambda c: (-c["score"], c["name"]))
        if not exhaustive:
            return candidates[:MAX_CANDIDATES]
        # A name that found no fund, or several, is searched again only the next day, not on every refresh.
        self._searched = {k: v for k, v in self._searched.items() if k[1] == key[1]} | {key: candidates}
        return list(candidates)

    async def _search(self, keyword: str, *, exhaustive: bool) -> list[dict]:
        """The records ``keyword`` finds. With ``exhaustive``, all of them or an error, never a partial list."""
        records: list[dict] = []
        seen: set[str] = set()
        expected: int | None = None
        for page in range(MAX_SEARCH_PAGES if exhaustive else 1):
            body = {"t_keyword": keyword, "t_kensakuKbn": "1", "startNo": page * SEARCH_PAGE_SIZE, "draw": page + 1}
            response = await self.post(TOUSHIN_SEARCH_URL, body=body, max_bytes=MAX_SEARCH_BYTES)
            if response.status_code != 200:
                raise ConnectorError(f"{self.manager}でファンドを検索できませんでした（HTTP {response.status_code}）")
            items, total = self._results(response)
            if not exhaustive:
                return items
            if expected is None:
                expected = total
            isins = [_field(i, "isinCd") for i in items]
            # The list changing between pages, a page that repeats funds, or one that stops short, would hide a
            # fund from the count, so none of them is taken as a complete answer.
            if total != expected or len(set(isins)) != len(isins) or seen & set(isins):
                raise ConnectorError(f"{self.manager}の検索結果がページの途中で変わりました（もう一度お試しください）")
            if not items and len(records) < expected:
                raise ConnectorError(f"{self.manager}の検索結果がページの途中で変わりました（もう一度お試しください）")
            seen.update(isins)
            records += items
            if len(records) > expected:
                raise ConnectorError(f"{self.manager}の検索結果がページの途中で変わりました（もう一度お試しください）")
            if len(records) == expected:
                return records
        raise TooManyFundsError(f"「{keyword}」に当てはまるファンドが多すぎるため、候補を絞り込めませんでした")

    def _results(self, response) -> tuple[list[dict], int]:
        try:
            payload = response.json()
        except ValueError:
            raise ConnectorError(f"{self.manager}から想定外の応答が返りました（JSON ではありません）") from None
        info = payload.get("searchResultInfo") if isinstance(payload, dict) else None
        items = info.get("resultInfoMapList") if isinstance(info, dict) else None
        try:
            total = int(info["recordsTotal"]) if isinstance(info, dict) else -1
        except (KeyError, TypeError, ValueError):
            total = -1
        if not isinstance(items, list) or total < 0:
            raise ConnectorError(f"{self.manager}から想定外の応答が返りました（検索結果の形式が違います）")
        return [i for i in items if isinstance(i, dict)], total

    def _candidate(self, record: dict, query: str) -> dict | None:
        """A search record as a candidate, or None when its ISIN or name is unusable.

        The 協会コード of the search is only shown and cross-checked: the NAV is always fetched with the code
        the fund page confirms, so a record with a broken code is still a usable candidate.
        """
        isin = _field(record, "isinCd").upper()
        association_code = _field(record, "associFundCd").upper()
        name = _field(record, "fundNm")
        nickname = _field(record, "fundNkNm")
        if not (ISIN_SHAPE.fullmatch(isin) and name):
            return None
        score = max(match_score(query, name), match_score(query, name + nickname) if nickname else 0.0)
        try:
            nav: float | None = nav_amount(record.get("standardPrice"))
            day: str | None = nav_date(record.get("standardDate"), latest=market_today())
        except ConnectorError:
            nav = day = None
        return {
            "provider": self.provider,
            "provider_label": self.info.label,
            "manager": unicodedata.normalize("NFKC", _field(record, "entrustCmpNm")),
            "fund_code": isin,
            "name": name,
            "nickname": nickname,
            "isin": isin,
            "association_code": association_code if ASSOCIATION_CODE_SHAPE.fullmatch(association_code) else None,
            "price_unit": self.price_unit,
            "score": round(score, 3),
            "exact": same_fund_name(query, name, nickname),
            "nav": nav,
            "date": day,
            "source_url": self.page_url(isin),
        }


def _field(record: dict, key: str) -> str:
    return str(record.get(key) or "").strip()
