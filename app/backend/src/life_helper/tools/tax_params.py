"""Year-specific tax parameters loaded from YAML (kept separate from code so they can be updated yearly)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class TaxParams:
    year: int
    status: str
    sources: list[str]
    raw: dict[str, Any]
    requested_year: int

    @property
    def warnings(self) -> list[str]:
        out = []
        if self.year != self.requested_year:
            out.append(f"{self.requested_year} 年のパラメータが未登録のため、{self.year} 年の値で計算しました。")
        if self.status != "verified":
            out.append(f"{self.year} 年のパラメータは未検証（仮置き）です。税制改正の内容を確認してください。")
        return out

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]


def available_years(directory: Path) -> list[int]:
    return sorted(int(p.stem) for p in directory.glob("*.yaml") if p.stem.isdigit())


@lru_cache(maxsize=16)
def _load(path: str) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_tax_params(directory: Path, year: int) -> TaxParams:
    years = available_years(directory)
    if not years:
        raise FileNotFoundError("no tax parameter files found")
    chosen = max((y for y in years if y <= year), default=years[0])
    raw = _load(str(directory / f"{chosen}.yaml"))
    return TaxParams(
        year=chosen,
        status=raw.get("status", "provisional"),
        sources=raw.get("sources", []),
        raw=raw,
        requested_year=year,
    )
