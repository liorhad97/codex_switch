from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


STATUS_LABELS = {
    "connected": "Connected",
    "disconnected": "Disconnected",
    "pending_oauth": "Waiting for sign-in",
    "error": "Needs attention",
    "available": "Available",
    "unknown": "Unknown",
}


def format_status(status: str) -> str:
    normalized = (status or "unknown").strip().lower()
    if normalized in STATUS_LABELS:
        return STATUS_LABELS[normalized]
    return normalized.replace("_", " ").replace("-", " ").title()


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "Unknown"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


@dataclass(slots=True)
class AccountRecord:
    id: str
    label: str
    home_dir: Path
    status: str = "unknown"
    enabled: bool = True
    flutty_primary: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    avatar_path: Path | None = None
    mapped_codex_profile: Path | None = None
    app_primary: bool = False
    source: str = "scan"
    identity: dict[str, Any] | None = None
    rate_limits: dict[str, Any] | None = None
    auth_mode: str | None = None
    oauth: dict[str, Any] | None = None
    last_error: str | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        title = self.label.strip()
        return title or self.id

    @property
    def subtitle(self) -> str:
        parts: list[str] = [format_status(self.status)]
        if not self.enabled:
            parts.append("Disabled")
        return " • ".join(dict.fromkeys(parts))

    @property
    def profile_root(self) -> Path:
        return self.home_dir.parent if self.home_dir.name == "home" else self.home_dir

    @property
    def initial(self) -> str:
        value = self.title.strip()
        return value[:1].upper() if value else "?"


@dataclass(slots=True)
class SwitcherConfig:
    primary_account_id: str | None
    last_selected_account_id: str | None
    codex_app_path: Path
    launch_profiles: dict[str, Path]


@dataclass(slots=True)
class OAuthFlowSnapshot:
    account_id: str
    status: str
    verification_uri: str | None = None
    user_code: str | None = None
    error: str | None = None
    settings_url: str | None = None
    help_url: str | None = None
    transcript: list[str] = field(default_factory=list)
