"""Maps Copilot session events to the small JSON events sent to the browser and stored in run history."""

from __future__ import annotations

import json
import re
from typing import Any

from copilot.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    AssistantUsageData,
    AttachmentBlob,
    SessionErrorData,
    ToolExecutionCompleteData,
    ToolExecutionStartData,
    UserMessageData,
)

from ..security import SecretMasker
from .attachments import attached_files

ARG_PREVIEW_LIMIT = 600
RESULT_PREVIEW_LIMIT = 1200
SCREENSHOT_ID = re.compile(r"[0-9a-f]{32}")


def _preview(value: Any, limit: int, masker: SecretMasker) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = masker.mask_text(text)
    return text if len(text) <= limit else text[:limit] + "…"


def extract_chart(result: Any) -> dict | None:
    """Pulls compact chart data (only the x and series columns) out of a simulation tool result."""
    data = _as_dict(result)
    if data is None or not isinstance(data.get("chart"), dict):
        return None
    spec = data["chart"]
    rows = data.get("rows") or data.get("yearly") or []
    x, series = spec.get("x"), [s for s in spec.get("series", []) if isinstance(s, str)]
    if not isinstance(rows, list) or not x or not series:
        return None
    points = [{k: row.get(k) for k in [x, *series]} for row in rows[:200] if isinstance(row, dict)]
    return {"type": spec.get("type"), "x": x, "series": series, "data": points}


def extract_screenshot(result: Any) -> dict | None:
    """Turns a browser_screenshot result into the image URL the chat shows."""
    data = _as_dict(result)
    shot = data.get("screenshot") if data is not None else None
    shot_id = shot.get("id") if isinstance(shot, dict) else None
    if not isinstance(shot_id, str) or not SCREENSHOT_ID.fullmatch(shot_id):
        return None
    return {"url": f"/api/browser/screenshots/{shot_id}"}


def _as_dict(result: Any) -> dict | None:
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def map_event(event: Any, masker: SecretMasker) -> dict | None:
    data = getattr(event, "data", None)
    match data:
        case AssistantMessageDeltaData() if not data.parent_tool_call_id:
            return {"type": "delta", "text": masker.mask_text(data.delta_content or "")}
        case AssistantMessageData() if not data.parent_tool_call_id:
            return {"type": "message", "content": masker.mask_text(data.content or "")}
        case ToolExecutionStartData():
            return {
                "type": "tool_start",
                "id": data.tool_call_id,
                "name": data.tool_name,
                "args": _preview(data.arguments, ARG_PREVIEW_LIMIT, masker),
            }
        case ToolExecutionCompleteData():
            result = getattr(data.result, "content", None) if data.result is not None else None
            event = {
                "type": "tool_end",
                "id": data.tool_call_id,
                "success": bool(data.success),
                "error": _preview(getattr(data.error, "message", data.error), ARG_PREVIEW_LIMIT, masker),
                "result": _preview(result, RESULT_PREVIEW_LIMIT, masker),
            }
            chart = extract_chart(result)
            if chart:
                event["chart"] = chart
            screenshot = extract_screenshot(result)
            if screenshot:
                event["screenshot"] = screenshot
            return event
        case SessionErrorData():
            return {"type": "error", "message": masker.mask_text(data.message or "エラーが発生しました")}
        case AssistantUsageData():
            return {"type": "usage", "model": data.model}
    return None


def history_from_events(events: list[Any], masker: SecretMasker) -> list[dict]:
    """Rebuilds a readable transcript (user/assistant messages and tool calls) from stored session events."""
    messages: list[dict] = []
    for event in events:
        data = getattr(event, "data", None)
        match data:
            case UserMessageData():
                content = data.content or ""
                files = attached_files(content, data.transformed_content)
                message = {"role": "user", "content": masker.mask_text(content)}
                images = [
                    {"name": masker.mask_text(a.display_name or "画像"), "kind": "image"}
                    for a in data.attachments or []
                    if isinstance(a, AttachmentBlob) and a.mime_type.startswith("image/")
                ]
                if images or files:
                    message["attachments"] = images + [f | {"name": masker.mask_text(f["name"])} for f in files]
                messages.append(message)
            case AssistantMessageData() if not data.parent_tool_call_id and data.content:
                messages.append({"role": "assistant", "content": masker.mask_text(data.content)})
            case ToolExecutionStartData():
                messages.append(
                    {
                        "role": "tool",
                        "name": data.tool_name,
                        "args": _preview(data.arguments, ARG_PREVIEW_LIMIT, masker),
                    }
                )
    return messages
