"""Serves browser screenshots to the signed-in user."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from ..auth import CurrentUser, require_user
from ..context import AppContext, get_ctx
from .service import is_expired, screenshot_dir

SCREENSHOT_ID = re.compile(r"[0-9a-f]{32}")

router = APIRouter(prefix="/api/browser")


@router.get("/screenshots/{shot_id}")
def get_screenshot(
    shot_id: str, user: CurrentUser = Depends(require_user), ctx: AppContext = Depends(get_ctx)
) -> FileResponse:
    path = screenshot_dir(ctx.settings) / f"{shot_id}.png"
    if not SCREENSHOT_ID.fullmatch(shot_id) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "screenshot not found")
    if is_expired(path):
        # Past the retention period: drop it here too, in case nothing has been saved since.
        path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "screenshot not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})
