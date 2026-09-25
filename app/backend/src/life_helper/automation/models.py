"""Automation definitions and schedule calculation (times are entered in Japan time)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator

JST = ZoneInfo("Asia/Tokyo")
MAX_RUNTIME_MINUTES = 20
# Connectors that were replaced, so automations saved with the old name keep their tools.
RENAMED_CONNECTORS = {"mufg_api": "toushin_lib"}


def normalize_connectors(names: list[str]) -> list[str]:
    """Current connector names, in the given order and without duplicates."""
    return list(dict.fromkeys(RENAMED_CONNECTORS.get(name, name) for name in names))


class Schedule(BaseModel):
    kind: Literal["daily", "weekly", "monthly", "yearly", "cron"] = "daily"
    time: str = Field(default="09:00", description="HH:MM（日本時間）")
    weekday: int = Field(default=0, ge=0, le=6, description="0=月曜 … 6=日曜")
    day: int = Field(default=1, ge=1, le=31)
    month: int = Field(default=1, ge=1, le=12)
    cron: str = Field(default="", description="cron 式（分 時 日 月 曜日、日本時間）")

    @field_validator("time")
    @classmethod
    def _time(cls, v: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time must be HH:MM")
        return v

    @model_validator(mode="after")
    def _cron(self) -> Schedule:
        if self.kind == "cron" and not croniter.is_valid(self.cron):
            raise ValueError("invalid cron expression")
        if self.kind == "cron" and self.cron.split()[0] in ("*", "*/1"):
            raise ValueError("running every minute is not allowed")
        return self

    def cron_expression(self) -> str:
        hour, minute = (int(x) for x in self.time.split(":"))
        match self.kind:
            case "daily":
                return f"{minute} {hour} * * *"
            case "weekly":
                # croniter: 0=Sunday. UI: 0=Monday.
                return f"{minute} {hour} * * {(self.weekday + 1) % 7}"
            case "monthly":
                return f"{minute} {hour} {self.day} * *"
            case "yearly":
                return f"{minute} {hour} {self.day} {self.month} *"
        return self.cron

    def next_after(self, moment: datetime) -> datetime:
        base = moment.astimezone(JST)
        return croniter(self.cron_expression(), base).get_next(datetime).astimezone(UTC)

    def occurrences_within(self, start: datetime, days: int) -> int:
        end = start + timedelta(days=days)
        it = croniter(self.cron_expression(), start.astimezone(JST))
        count = 0
        while count < 10_000:
            nxt = it.get_next(datetime)
            if nxt > end:
                break
            count += 1
        return count


class NotifySettings(BaseModel):
    github: bool = Field(default=False, description="GitHub Issue で通知する（既定はオフ）")
    condition: Literal["always", "report", "signal"] = Field(
        default="report",
        description="always=毎回 / report=Copilot が通知すべきと報告したとき / signal=ツール結果の値で判定",
    )
    signal_field: str = Field(default="", description="signal 判定に使う値の名前（例: vacancy_count）")
    signal_op: Literal[">", ">=", "==", "!=", "<", "<="] = ">"
    signal_value: float = 0
    only_on_change: bool = Field(default=False, description="前回は条件を満たさず今回満たしたときだけ通知する")
    include_summary: bool = Field(default=False, description="Issue に要約本文を含める（既定は含めない）")

    def signal_met(self, signals: dict[str, float]) -> bool | None:
        if self.signal_field not in signals:
            return None
        value, target = signals[self.signal_field], self.signal_value
        return {
            ">": value > target,
            ">=": value >= target,
            "==": value == target,
            "!=": value != target,
            "<": value < target,
            "<=": value <= target,
        }[self.signal_op]


class AutomationState(BaseModel):
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_condition_met: bool | None = None


class Automation(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    prompt: str = Field(min_length=1, max_length=8000)
    schedule: Schedule = Field(default_factory=Schedule)
    conversation_mode: Literal["new", "continue"] = "new"
    model: str = "auto"
    allow_write: bool = Field(
        default=False, description="メモリ・ノート・保有銘柄の書き換えを許可する（既定は読み取り専用）"
    )
    connectors: list[str] = Field(default_factory=list)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    max_runtime_minutes: int = Field(default=MAX_RUNTIME_MINUTES, ge=1, le=MAX_RUNTIME_MINUTES)
    state: AutomationState = Field(default_factory=AutomationState)

    @field_validator("connectors")
    @classmethod
    def _connectors(cls, v: list[str]) -> list[str]:
        return normalize_connectors(v)

    def is_due(self, now: datetime) -> bool:
        if not self.enabled or not self.state.next_run_at:
            return False
        return datetime.fromisoformat(self.state.next_run_at) <= now

    def schedule_next(self, now: datetime) -> None:
        self.state.next_run_at = self.schedule.next_after(now).isoformat()


WEEKDAYS = "月火水木金土日"


def expand_prompt(prompt: str, now: datetime) -> str:
    local = now.astimezone(JST)
    values = {
        "today": local.strftime("%Y-%m-%d"),
        "year": str(local.year),
        "month": str(local.month),
        "day": str(local.day),
        "weekday": WEEKDAYS[local.weekday()],
    }
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: values.get(m.group(1), m.group(0)), prompt)
