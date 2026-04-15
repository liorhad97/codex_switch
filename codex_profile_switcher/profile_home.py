from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path
from typing import Any


def codex_home_path(home_dir: str | Path) -> Path:
    return Path(home_dir).expanduser().resolve().joinpath(".codex")


def sync_profile_home(home_dir: str | Path) -> Path:
    resolved_home_dir = Path(home_dir).expanduser().resolve()
    codex_dir = codex_home_path(resolved_home_dir)
    codex_dir.mkdir(parents=True, exist_ok=True)
    _sync_mcp_servers_config(codex_dir)
    return resolved_home_dir


class ProfileHomeManager:
    def __init__(self, profiles_root: Path) -> None:
        self._profiles_root = profiles_root.expanduser().resolve()
        self._profiles_root.mkdir(parents=True, exist_ok=True)

    def profile_root(self, account_id: str) -> Path:
        return self._profiles_root / account_id

    def expected_home_dir(self, account_id: str) -> Path:
        return self.profile_root(account_id) / "home"

    def ensure_profile_home(self, account_id: str) -> Path:
        return sync_profile_home(self.expected_home_dir(account_id))

    def delete_profile(self, account_id: str) -> None:
        profile_root = self.profile_root(account_id)
        if profile_root.exists():
            shutil.rmtree(profile_root)


def _sync_mcp_servers_config(codex_dir: Path) -> None:
    mcp_servers = _read_global_mcp_servers()
    if not mcp_servers:
        return

    rendered = _render_toml_document({"mcp_servers": mcp_servers})
    config_path = codex_dir / "config.toml"
    try:
        if config_path.exists() and config_path.read_text(encoding="utf-8") == rendered:
            return
    except OSError:
        pass
    config_path.write_text(rendered, encoding="utf-8")


def _read_global_mcp_servers() -> dict[str, Any] | None:
    global_config_path = Path.home() / ".codex" / "config.toml"
    if not global_config_path.exists():
        return None

    try:
        payload = tomllib.loads(global_config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None

    mcp_servers = payload.get("mcp_servers")
    return mcp_servers if isinstance(mcp_servers, dict) and mcp_servers else None


def _render_toml_document(payload: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Synced from ~/.codex/config.toml",
        "",
    ]
    _append_toml_tables(lines, (), payload)
    return "\n".join(lines).rstrip() + "\n"


def _append_toml_tables(lines: list[str], prefix: tuple[str, ...], payload: dict[str, Any]) -> None:
    scalar_items = [(key, value) for key, value in payload.items() if not isinstance(value, dict)]
    table_items = [(key, value) for key, value in payload.items() if isinstance(value, dict)]

    should_emit_header = bool(prefix) and (bool(scalar_items) or len(prefix) > 1)
    if should_emit_header:
        lines.append(f"[{'.'.join(_quote_toml_key(part) for part in prefix)}]")
    for key, value in scalar_items:
        if value is None:
            continue
        lines.append(f"{_quote_toml_key(key)} = {_toml_literal(value)}")
    if scalar_items and table_items:
        lines.append("")

    for index, (key, value) in enumerate(table_items):
        _append_toml_tables(lines, (*prefix, key), value)
        if index != len(table_items) - 1:
            lines.append("")


def _quote_toml_key(value: str) -> str:
    if value.replace("_", "").replace("-", "").isalnum():
        return value
    return json.dumps(value)


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_literal(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value: {value!r}")
