"""Status entry for the headless browser, so it shows in Settings and can be selected for automations."""

from __future__ import annotations

from pathlib import Path

from ..security import SecretMasker
from .base import Connector, ConnectorInfo


class BrowserConnector(Connector):
    info = ConnectorInfo(
        name="browser",
        label="ブラウザ（Playwright）",
        hosts=(),
        secret_names=(),
        cost="無料（コンテナ内の Chromium で動作）",
    )

    def __init__(self, enabled: bool, masker: SecretMasker, state_path: Path) -> None:
        super().__init__({}, masker, state_path)
        self._enabled = enabled

    @property
    def configured(self) -> bool:
        return self._enabled

    def mark_used(self) -> None:
        self._record_use()
