"""Imports holdings from broker CSV exports using column mappings kept in YAML."""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from pathlib import Path

import yaml

from .clock import market_today
from .portfolio import Holding, Price


class BrokerCsvError(ValueError):
    pass


def load_mapping(directory: Path, broker: str) -> dict:
    if not re.fullmatch(r"[a-z0-9_]+", broker):
        raise BrokerCsvError("unknown broker")
    path = directory / f"{broker}.yaml"
    if not path.exists():
        raise BrokerCsvError(f"unknown broker: {broker}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def list_brokers(directory: Path) -> list[dict]:
    return [
        {"name": m["name"], "label": m["label"]}
        for m in (yaml.safe_load(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.yaml")))
    ]


def _decode(data: bytes, encodings: list[str]) -> str:
    for enc in encodings:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise BrokerCsvError("CSV の文字コードを判別できませんでした")


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = re.sub(r"[,\s円口株+]", "", value).replace("−", "-").replace("－", "-")
    if cleaned in ("", "-", "--", "---"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _match_account(text: str, accounts: dict[str, list[str]]) -> str | None:
    # Order matters: the NISA sub-accounts must be checked before the generic words.
    for account in ("nisa_tsumitate", "nisa_growth", "ideco", "tokutei", "ippan"):
        if any(word and word in text for word in accounts.get(account, [])):
            return account
    return None


def parse_broker_csv(data: bytes, mapping: dict, *, as_of: date | None = None) -> list[Holding]:
    text = _decode(data, mapping.get("encodings", ["utf-8-sig"]))
    rows = list(csv.reader(io.StringIO(text)))
    columns: dict[str, list[str]] = mapping["columns"]
    required: list[str] = mapping.get("required", [])
    accounts: dict[str, list[str]] = mapping.get("accounts", {})
    fund_markers: list[str] = mapping.get("fund_markers", [])
    as_of = as_of or market_today()

    header_index: dict[str, int] | None = None
    section_text = ""
    holdings: list[Holding] = []
    found_header = False
    for row in rows:
        cells = [c.strip() for c in row]
        if not any(cells):
            header_index = None
            continue
        index = {key: next((cells.index(n) for n in names if n in cells), -1) for key, names in columns.items()}
        if all(index.get(r, -1) >= 0 for r in required) and (
            index.get("name", -1) >= 0 or index.get("code_name", -1) >= 0
        ):
            header_index = {k: v for k, v in index.items() if v >= 0}
            found_header = True
            continue
        if header_index is None:
            # Rows outside a table are section titles such as "株式（特定預り）" or "投資信託（NISA預り(成長投資枠)）".
            section_text = " ".join(cells)
            continue

        def cell(key: str, _cells: list[str] = cells, _index: dict[str, int] = header_index) -> str | None:
            i = _index.get(key)
            return _cells[i] if i is not None and i < len(_cells) else None

        quantity = _number(cell("quantity"))
        valuation = _number(cell("valuation"))
        if quantity is None or valuation is None:
            continue
        name, code = cell("name") or "", cell("code") or ""
        combined = cell("code_name")
        if combined:
            m = re.match(r"^\s*([0-9][0-9A-Za-z]{3,4})\s+(.+)$", combined)
            code, name = (m.group(1), m.group(2)) if m else (code, combined)
        account_text = " ".join(filter(None, [cell("account"), section_text]))
        account = _match_account(account_text, accounts) or "tokutei"
        kind_text = " ".join(filter(None, [cell("kind"), section_text]))
        kind = "fund" if any(m in kind_text for m in fund_markers) else "stock"
        cost_total = _number(cell("cost_total"))
        avg_cost = _number(cell("avg_cost"))
        if cost_total is None and avg_cost is not None:
            cost_total = avg_cost * quantity / (10_000 if kind == "fund" else 1)
        price_value = _number(cell("price"))
        holdings.append(
            Holding(
                account=account,
                kind=kind,
                code=code.strip(),
                name=name.strip() or code.strip() or "（名称不明）",
                quantity=quantity,
                cost_total=cost_total or 0.0,
                price=Price(value=price_value, date=as_of.isoformat(), source="broker_csv") if price_value else None,
                valuation_yen=valuation,
            )
        )
    if not found_header:
        missing = ", ".join(required)
        raise BrokerCsvError(f"CSV の見出しが想定と違うため取り込みませんでした（必要な列: {missing}）")
    if not holdings:
        raise BrokerCsvError("取り込める保有銘柄が見つかりませんでした")
    return holdings
