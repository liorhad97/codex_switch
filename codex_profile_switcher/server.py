from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib.parse import parse_qs, urlsplit
from urllib import request as urllib_request

from .launcher import (
    build_codex_launch_command,
    build_codex_vscode_extension_command,
    launch_codex,
    launch_codex_vscode_extension,
)
from .models import AccountRecord
from .oauth import AccountOAuthManager
from .profile_home import codex_home_path
from .store import ProfileStore


def _is_windows() -> bool:
    return os.name == "nt"


_FLUTTY_LIVE_STATE_CACHE: dict[str, Any] = {
    "api_base": None,
    "accounts": {},
    "fetched_at": 0.0,
    "cooldown_until": 0.0,
}


def _serialize_account(account: AccountRecord) -> dict[str, Any]:
    return {
        "id": account.id,
        "label": account.label,
        "title": account.title,
        "subtitle": account.subtitle,
        "status": account.status,
        "enabled": account.enabled,
        "app_primary": account.app_primary,
        "source_primary": account.flutty_primary,
        "home_dir": str(account.home_dir),
        "avatar_path": str(account.avatar_path) if account.avatar_path else None,
        "mapped_codex_profile": str(account.mapped_codex_profile) if account.mapped_codex_profile else None,
        "created_at": account.created_at.isoformat() if account.created_at else None,
        "updated_at": account.updated_at.isoformat() if account.updated_at else None,
        "source": account.source,
        "identity": account.identity,
        "rate_limits": account.rate_limits,
        "auth_mode": account.auth_mode,
        "oauth": account.oauth,
        "last_error": account.last_error,
        "issues": list(account.issues),
    }


class SwitcherRequestHandler(SimpleHTTPRequestHandler):
    server_version = "CodexProfileSwitcher/1.0"

    def __init__(self, *args, directory: str | None = None, **kwargs) -> None:
        super().__init__(*args, directory=directory, **kwargs)

    @property
    def switcher(self) -> "SwitcherServer":
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            self._send_json(self.switcher.build_state(refresh_usage=_query_flag(query, "refresh_usage")))
            return
        if parsed.path.startswith("/api/"):
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/accounts/add":
            payload = self._read_json_body()
            label = payload.get("label")
            try:
                flow = self.switcher.start_pending_account(label=label if isinstance(label, str) else None)
            except Exception as error:  # noqa: BLE001
                self._send_json(
                    {"error": str(error).strip() or "Could not start ChatGPT sign-in."},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            response = self.switcher.build_state()
            response.update(
                {
                    "created_account_id": flow.account_id,
                    "oauth": asdict(flow),
                }
            )
            self._send_json(response)
            return

        if self.path == "/api/oauth/cancel":
            self.switcher.cancel_pending_account()
            self._send_json(self.switcher.build_state())
            return

        if self.path == "/api/import":
            self.switcher.import_accounts()
            self._send_json(self.switcher.build_state())
            return

        if self.path == "/api/diagnostics/fix":
            self._send_json(self.switcher.fix_common_issues())
            return

        account_match = re.fullmatch(r"/api/accounts/([^/]+)/primary", self.path)
        if account_match:
            try:
                self.switcher.set_primary(account_match.group(1))
            except ValueError as error:
                self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(self.switcher.build_state())
            return

        account_match = re.fullmatch(r"/api/accounts/([^/]+)/select", self.path)
        if account_match:
            self.switcher.set_selected(account_match.group(1))
            self._send_json(self.switcher.build_state())
            return

        account_match = re.fullmatch(r"/api/accounts/([^/]+)/launch-profile", self.path)
        if account_match:
            payload = self._read_json_body()
            path_value = payload.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                self._send_json({"error": "Missing path"}, status=HTTPStatus.BAD_REQUEST)
                return
            self.switcher.set_launch_profile(account_match.group(1), Path(path_value))
            self._send_json(self.switcher.build_state())
            return

        account_match = re.fullmatch(r"/api/accounts/([^/]+)/launch-suggested", self.path)
        if account_match:
            self.switcher.set_suggested_launch_profile(account_match.group(1))
            self._send_json(self.switcher.build_state())
            return

        account_match = re.fullmatch(r"/api/accounts/([^/]+)/launch", self.path)
        if account_match:
            try:
                command = self.switcher.launch_account(account_match.group(1))
            except ValueError as error:
                self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            response = self.switcher.build_state()
            response.update({"ok": True, "command": command})
            self._send_json(response)
            return

        account_match = re.fullmatch(r"/api/accounts/([^/]+)/launch-vscode", self.path)
        if account_match:
            try:
                command = self.switcher.set_account_for_codex_vscode(account_match.group(1))
            except ValueError as error:
                self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            response = self.switcher.build_state()
            response.update({"ok": True, "command": command})
            self._send_json(response)
            return

        account_match = re.fullmatch(r"/api/accounts/([^/]+)/connect", self.path)
        if account_match:
            try:
                flow = self.switcher.connect_account(account_match.group(1))
            except ValueError as error:
                self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            response = self.switcher.build_state()
            if flow is not None:
                response["oauth"] = asdict(flow)
            self._send_json(response)
            return

        account_match = re.fullmatch(r"/api/accounts/([^/]+)/connect/cancel", self.path)
        if account_match:
            try:
                self.switcher.cancel_account_sign_in(account_match.group(1))
            except ValueError as error:
                self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(self.switcher.build_state())
            return

        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:  # noqa: N802
        account_match = re.fullmatch(r"/api/accounts/([^/]+)", self.path)
        if account_match:
            try:
                self.switcher.remove_account(account_match.group(1))
            except ValueError as error:
                self._send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(self.switcher.build_state())
            return
        account_match = re.fullmatch(r"/api/accounts/([^/]+)/launch-profile", self.path)
        if account_match:
            self.switcher.clear_launch_profile(account_match.group(1))
            self._send_json(self.switcher.build_state())
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return None

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def send_head(self):  # type: ignore[override]
        if self.path.startswith("/api/"):
            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return None
        return super().send_head()

    def translate_path(self, path: str) -> str:
        translated = super().translate_path(path)
        resolved = Path(translated)
        if resolved.exists():
            return str(resolved)
        return str(self.switcher.static_root / "index.html")

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


class SwitcherServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        static_root: Path,
        store: ProfileStore,
        oauth_manager: AccountOAuthManager | None = None,
    ) -> None:
        self.static_root = static_root
        self.store = store
        codex_binary = _resolve_codex_binary(store)
        self.oauth = oauth_manager or AccountOAuthManager(
            store=store,
            workspace_root=Path.cwd(),
            codex_binary=codex_binary,
        )
        self._selected_account_id: str | None = None
        super().__init__(
            address,
            lambda *args, **kwargs: SwitcherRequestHandler(*args, directory=str(static_root), **kwargs),
        )

    def build_state(self, *, refresh_usage: bool = False) -> dict[str, Any]:
        accounts, config = self.store.load_accounts()
        pending_flow = self.oauth.get_pending_flow()
        if pending_flow is not None:
            accounts = [
                account
                for account in accounts
                if account.id != pending_flow.account_id or account.source.startswith("local_")
            ]
        selected = self._selected_account_id or config.last_selected_account_id or config.primary_account_id
        if selected and all(account.id != selected for account in accounts):
            selected = accounts[0].id if accounts else None
        live_accounts = _load_flutty_live_accounts()
        for account in accounts:
            live = self._load_account_live_state(account, live_accounts, refresh_usage=refresh_usage)
            if not isinstance(live, dict):
                continue
            identity = live.get("identity")
            if not isinstance(identity, dict):
                email = live.get("email")
                name = live.get("name")
                if isinstance(email, str) or isinstance(name, str):
                    identity = {}
                    if isinstance(email, str) and email:
                        identity["email"] = email
                    if isinstance(name, str) and name:
                        identity["name"] = name
            rate_limits = _live_value(live, "rate_limits", "rateLimits")
            last_error = _live_value(live, "last_error", "lastError")
            auth_mode = _live_value(live, "auth_mode", "authMode")
            oauth = live.get("oauth")
            if isinstance(identity, dict) and identity:
                account.identity = identity
            if isinstance(rate_limits, dict):
                account.rate_limits = rate_limits
            if isinstance(last_error, str) and last_error:
                account.last_error = last_error
            if isinstance(auth_mode, str) and auth_mode:
                account.auth_mode = auth_mode
            if isinstance(oauth, dict):
                account.oauth = oauth
        return {
            "accounts": [_serialize_account(account) for account in accounts],
            "selected_account_id": selected,
            "primary_account_id": config.primary_account_id,
            "pending_oauth_flow": asdict(pending_flow) if pending_flow else None,
            "data_root": str(self.store.paths.data_root),
            "prepared_profiles_root": str(self.store.paths.prepared_profiles_root),
            "main_codex_home": str(self.store.paths.main_codex_home),
            "codex_app_path": str(config.codex_app_path),
        }

    def _load_account_live_state(
        self,
        account: AccountRecord,
        live_accounts: dict[str, dict[str, Any]],
        *,
        refresh_usage: bool,
    ) -> dict[str, Any] | None:
        upstream_live = live_accounts.get(account.id)
        if account.source.startswith("local_") or _has_codex_auth(account):
            if not refresh_usage:
                cached_live = self.oauth.cached_account_state(account)
                if isinstance(_live_value(cached_live, "rate_limits", "rateLimits"), dict):
                    return _merge_live_state(upstream_live, cached_live)
            try:
                direct_live = self.oauth.refresh_account_state(
                    account,
                    refresh_rate_limits=refresh_usage,
                    persist_account=account.source.startswith("local_"),
                )
            except Exception as error:
                direct_live = self.oauth.cached_account_state(account)
                if not isinstance(direct_live, dict):
                    direct_live = {}
                if not isinstance(_live_value(direct_live, "rate_limits", "rateLimits"), dict):
                    direct_live = dict(direct_live)
                    direct_live["last_error"] = _format_live_state_error(error)
            return _merge_live_state(upstream_live, direct_live)
        return upstream_live if isinstance(upstream_live, dict) else None

    def import_accounts(self) -> None:
        self.store.import_accounts()

    def fix_common_issues(self) -> dict[str, Any]:
        fixed: list[str] = []
        checks: list[str] = []
        warnings: list[str] = []

        _reset_live_state_cache()
        fixed.append("Cleared cached live usage state.")

        pending_flow = self.oauth.get_pending_flow()
        excluded_skeleton_ids = {pending_flow.account_id} if pending_flow is not None else set()
        try:
            skeleton_report = self.store.cleanup_skeleton_profiles(exclude_account_ids=excluded_skeleton_ids)
            removed_skeletons = skeleton_report["removed"]
            if removed_skeletons:
                fixed.append(
                    "Removed "
                    f"{len(removed_skeletons)} skeleton profile"
                    f"{'' if len(removed_skeletons) == 1 else 's'}: "
                    f"{', '.join(removed_skeletons)}."
                )
            else:
                checks.append("No skeleton profiles needed cleanup.")
            warnings.extend(skeleton_report["warnings"])
        except Exception as error:  # noqa: BLE001
            warnings.append(f"Skeleton profile cleanup failed: {error}")

        try:
            self.store.import_accounts()
            fixed.append("Refreshed account discovery snapshot.")
        except Exception as error:  # noqa: BLE001
            warnings.append(f"Account discovery refresh failed: {error}")

        process_report = _terminate_stale_backend_processes(
            current_pid=os.getpid(),
            current_port=int(self.server_address[1]),
        )
        checks.extend(process_report["checks"])
        fixed.extend(process_report["fixed"])
        warnings.extend(process_report["warnings"])

        return {
            "ok": not warnings,
            "checks": checks,
            "fixed": fixed,
            "warnings": warnings,
            "state": self.build_state(),
        }

    def start_pending_account(self, label: str | None = None) -> Any:
        del label
        return self.oauth.start_temporary()

    def cancel_pending_account(self) -> None:
        self.oauth.cancel_pending()

    def set_primary(self, account_id: str) -> None:
        _find_account(self.store.load_accounts()[0], account_id)
        config = self.store.load_config()
        self.store.set_primary(config, account_id)
        self._selected_account_id = account_id

    def connect_account(self, account_id: str) -> Any:
        accounts = self.store.load_accounts()[0]
        account = _find_account(accounts, account_id)
        try:
            live = self.oauth.refresh_account_state(account, refresh_rate_limits=False)
        except Exception:
            live = self.oauth.cached_account_state(account)
        if isinstance(live.get("email"), str) and live.get("email"):
            self.store.update_local_account(
                account_id,
                status="connected",
                enabled=True,
                identity={"email": live.get("email"), "name": live.get("name")}
                if isinstance(live.get("email"), str) or isinstance(live.get("name"), str)
                else None,
                rate_limits=live.get("rate_limits") if isinstance(live.get("rate_limits"), dict) else None,
                auth_mode=live.get("auth_mode"),
                last_error=None,
            )
            self._selected_account_id = account_id
            return None
        flow = self.oauth.start(account)
        self._selected_account_id = account_id
        return flow

    def cancel_account_sign_in(self, account_id: str) -> None:
        accounts = self.store.load_accounts()[0]
        account = _find_account(accounts, account_id)
        if not account.source.startswith("local_"):
            raise ValueError("Only local switcher accounts support canceling sign-in.")
        self.oauth.cancel(account_id)
        self._selected_account_id = account_id

    def set_selected(self, account_id: str) -> None:
        _find_account(self.store.load_accounts()[0], account_id)
        config = self.store.load_config()
        self.store.set_selected_account(config, account_id)
        self._selected_account_id = account_id

    def set_launch_profile(self, account_id: str, profile_dir: Path) -> None:
        _find_account(self.store.load_accounts()[0], account_id)
        config = self.store.load_config()
        self.store.set_launch_profile(config, account_id, profile_dir)
        self._selected_account_id = account_id

    def set_suggested_launch_profile(self, account_id: str) -> None:
        _find_account(self.store.load_accounts()[0], account_id)
        suggested = (self.store.paths.prepared_profiles_root / account_id).resolve()
        suggested.mkdir(parents=True, exist_ok=True)
        config = self.store.load_config()
        self.store.set_launch_profile(config, account_id, suggested)
        self._selected_account_id = account_id

    def clear_launch_profile(self, account_id: str) -> None:
        _find_account(self.store.load_accounts()[0], account_id)
        config = self.store.load_config()
        self.store.clear_launch_profile(config, account_id)
        self._selected_account_id = account_id

    def remove_account(self, account_id: str) -> None:
        _find_account(self.store.load_accounts()[0], account_id)
        self.oauth.close(account_id)
        self.store.remove_account(account_id)
        if self._selected_account_id == account_id:
            self._selected_account_id = None

    def launch_account(self, account_id: str) -> list[str]:
        accounts, config = self.store.load_accounts()
        _find_account(accounts, account_id)
        updated_config = self.store.set_primary(config, account_id)
        command = build_codex_launch_command(updated_config.codex_app_path)
        launch_codex(updated_config.codex_app_path)
        self._selected_account_id = account_id
        return command

    def set_account_for_codex_vscode(self, account_id: str) -> list[str]:
        accounts, config = self.store.load_accounts()
        _find_account(accounts, account_id)
        self.store.set_primary(config, account_id)
        command = build_codex_vscode_extension_command()
        launch_codex_vscode_extension()
        self._selected_account_id = account_id
        return command

    def server_close(self) -> None:
        self.oauth.close_all()
        super().server_close()


def _find_account(accounts: list[AccountRecord], account_id: str) -> AccountRecord:
    for account in accounts:
        if account.id == account_id:
            return account
    raise ValueError(f"Unknown account: {account_id}")


def _query_flag(query: dict[str, list[str]], key: str) -> bool:
    values = query.get(key) or []
    return any(value.strip().lower() in {"1", "true", "yes", "on"} for value in values)


def _live_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _format_live_state_error(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    if len(message) > 240:
        message = f"{message[:237]}..."
    return f"Usage refresh failed: {message}"


def _has_codex_auth(account: AccountRecord) -> bool:
    try:
        return (codex_home_path(account.home_dir) / "auth.json").is_file()
    except OSError:
        return False


def _resolve_codex_binary(store: ProfileStore) -> str:
    explicit = os.getenv("CODEX_BINARY")
    if explicit:
        return explicit

    path_binary = shutil.which("codex")
    if path_binary:
        return path_binary

    candidates: list[Path] = []
    if _is_windows():
        candidates.extend(_windows_appx_codex_cli_candidates())
    try:
        candidates.extend(_codex_app_server_candidates(store.load_config().codex_app_path))
    except (OSError, ValueError):
        pass

    for candidate in candidates:
        try:
            if candidate.is_file() and (_is_windows() or os.access(candidate, os.X_OK)):
                return str(candidate)
        except OSError:
            continue

    if _is_windows():
        return "codex.exe"
    return "codex"


def _codex_app_server_candidates(codex_app_path: Path) -> list[Path]:
    configured = codex_app_path.expanduser().resolve()
    candidates: list[Path] = []
    if _is_windows():
        configured_dir = configured.parent if configured.suffix.lower() == ".exe" else configured
        relative_cli_locations = (
            Path("resources") / "codex.exe",
            Path("resources") / "bin" / "codex.exe",
            Path("resources") / "app" / "codex.exe",
            Path("resources") / "app.asar.unpacked" / "codex.exe",
            Path("resources") / "app.asar.unpacked" / "bin" / "codex.exe",
            Path("codex.exe"),
        )
        candidates.extend(
            configured_dir / relative_path
            for relative_path in relative_cli_locations
        )
        for root in _windows_program_roots():
            for app_dir in _windows_codex_app_dirs(root):
                candidates.extend(app_dir / relative_path for relative_path in relative_cli_locations)
        appdata = os.getenv("APPDATA")
        if appdata:
            candidates.extend(
                [
                    Path(appdata) / "npm" / "codex.cmd",
                    Path(appdata) / "npm" / "codex.exe",
                ]
            )
        return _dedupe_paths(candidates)

    candidates.append(configured / "Contents" / "Resources" / "codex")
    candidates.append(Path("/Applications/Codex.app/Contents/Resources/codex"))
    return _dedupe_paths(candidates)


def _windows_program_roots() -> list[Path]:
    roots: list[Path] = []
    for env_name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.getenv(env_name)
        if value:
            roots.append(Path(value))
    if not roots:
        roots.append(Path.home() / "AppData" / "Local")
    return roots


def _windows_codex_app_dirs(root: Path) -> list[Path]:
    return [
        root / "Programs" / "Codex",
        root / "Programs" / "OpenAI Codex",
        root / "Codex",
        root / "OpenAI" / "Codex",
    ]


def _windows_appx_codex_cli_candidates() -> list[Path]:
    powershell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    script = "Get-AppxPackage -Name 'OpenAI.Codex' | Select-Object -ExpandProperty InstallLocation"
    try:
        output = subprocess.check_output(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return []

    candidates: list[Path] = []
    for line in output.splitlines():
        install_location = line.strip()
        if not install_location:
            continue
        source_cli = Path(install_location) / "app" / "resources" / "codex.exe"
        cached_cli = _cache_windows_appx_codex_cli(source_cli)
        if cached_cli is not None:
            candidates.append(cached_cli)
        candidates.append(source_cli)
    return _dedupe_paths(candidates)


def _cache_windows_appx_codex_cli(source_cli: Path) -> Path | None:
    try:
        if not source_cli.is_file():
            return None
        target_dir = Path.home().expanduser().resolve() / "codex_switch_data" / "codex_cli"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_cli = target_dir / "codex.exe"
        if _should_refresh_cached_cli(source_cli, target_cli):
            shutil.copy2(source_cli, target_cli)
        return target_cli if target_cli.is_file() else None
    except OSError:
        return None


def _should_refresh_cached_cli(source_cli: Path, target_cli: Path) -> bool:
    try:
        if not target_cli.exists():
            return True
        source_stat = source_cli.stat()
        target_stat = target_cli.stat()
    except OSError:
        return True
    return source_stat.st_size != target_stat.st_size or source_stat.st_mtime > target_stat.st_mtime


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    deduped: list[Path] = []
    for path in paths:
        key = str(path).casefold() if _is_windows() else str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _merge_live_state(
    upstream_live: dict[str, Any] | None,
    direct_live: dict[str, Any] | None,
) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    if isinstance(upstream_live, dict):
        merged.update(upstream_live)
    if isinstance(direct_live, dict):
        for key, value in direct_live.items():
            if value is not None:
                merged[key] = value
    return merged or None


def _load_flutty_live_accounts() -> dict[str, dict[str, Any]]:
    now = time.monotonic()
    cached_accounts = _FLUTTY_LIVE_STATE_CACHE.get("accounts")
    if isinstance(cached_accounts, dict) and cached_accounts and now - float(_FLUTTY_LIVE_STATE_CACHE.get("fetched_at") or 0.0) < 15:
        return cached_accounts
    if now < float(_FLUTTY_LIVE_STATE_CACHE.get("cooldown_until") or 0.0):
        return cached_accounts if isinstance(cached_accounts, dict) else {}

    api_base = _FLUTTY_LIVE_STATE_CACHE.get("api_base") or _detect_flutty_api_base()
    if not api_base:
        _FLUTTY_LIVE_STATE_CACHE["cooldown_until"] = now + 15
        return {}

    try:
        with urllib_request.urlopen(f"{api_base}/api/state", timeout=0.75) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, ValueError, urllib_error.URLError):
        _FLUTTY_LIVE_STATE_CACHE["api_base"] = api_base
        _FLUTTY_LIVE_STATE_CACHE["cooldown_until"] = now + 15
        return cached_accounts if isinstance(cached_accounts, dict) else {}

    if not isinstance(payload, dict):
        _FLUTTY_LIVE_STATE_CACHE["api_base"] = api_base
        _FLUTTY_LIVE_STATE_CACHE["cooldown_until"] = now + 15
        return cached_accounts if isinstance(cached_accounts, dict) else {}

    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        _FLUTTY_LIVE_STATE_CACHE["api_base"] = api_base
        _FLUTTY_LIVE_STATE_CACHE["cooldown_until"] = now + 15
        return cached_accounts if isinstance(cached_accounts, dict) else {}

    results: dict[str, dict[str, Any]] = {}
    for entry in accounts:
        if not isinstance(entry, dict):
            continue
        account_id = entry.get("id")
        if isinstance(account_id, str) and account_id:
            results[account_id] = entry
    _FLUTTY_LIVE_STATE_CACHE["api_base"] = api_base
    _FLUTTY_LIVE_STATE_CACHE["accounts"] = results
    _FLUTTY_LIVE_STATE_CACHE["fetched_at"] = now
    _FLUTTY_LIVE_STATE_CACHE["cooldown_until"] = 0.0
    return results


def _reset_live_state_cache() -> None:
    _FLUTTY_LIVE_STATE_CACHE.update(
        {
            "api_base": None,
            "accounts": {},
            "fetched_at": 0.0,
            "cooldown_until": 0.0,
        }
    )


def _detect_flutty_api_base() -> str | None:
    explicit = os.getenv("FLUTTY_ORC_API_BASE")
    if explicit:
        return explicit.rstrip("/")

    if _is_windows():
        return None

    try:
        output = subprocess.check_output(["ps", "-Ao", "command"], text=True)
    except (subprocess.SubprocessError, OSError):
        return None

    for line in output.splitlines():
        match = re.search(r"-m codex_chat\.serve --host (\S+) --port (\d+)", line)
        if match:
            host, port = match.groups()
            return f"http://{host}:{port}"
    return None


def _list_codex_switch_backend_processes() -> list[dict[str, Any]]:
    if _is_windows():
        return _list_codex_switch_backend_processes_windows()

    try:
        output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,command="], text=True)
    except (subprocess.SubprocessError, OSError):
        return []

    processes: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid_text, ppid_text, command = parts
        if "codex_profile_switcher.server" not in command and "codex-switch-backend" not in command:
            continue
        try:
            pid = int(pid_text)
            ppid = int(ppid_text)
        except ValueError:
            continue
        processes.append(
            {
                "pid": pid,
                "ppid": ppid,
                "command": command,
                "host": _process_arg_value(command, "--host"),
                "port": _process_arg_int(command, "--port"),
                "static_root": _process_arg_value(command, "--static-root"),
            }
        )
    return processes


def _list_codex_switch_backend_processes_windows() -> list[dict[str, Any]]:
    powershell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        output = subprocess.check_output(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        payload = json.loads(output or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    entries = payload if isinstance(payload, list) else [payload]
    processes: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        command = entry.get("CommandLine")
        if not isinstance(command, str):
            continue
        if "codex_profile_switcher.server" not in command and "codex-switch-backend" not in command:
            continue
        pid = _int_value(entry.get("ProcessId"))
        ppid = _int_value(entry.get("ParentProcessId"))
        if pid is None or ppid is None:
            continue
        processes.append(
            {
                "pid": pid,
                "ppid": ppid,
                "command": command,
                "host": _process_arg_value(command, "--host"),
                "port": _process_arg_int(command, "--port"),
                "static_root": _process_arg_value(command, "--static-root"),
            }
        )
    return processes


def _process_arg_value(command: str, name: str) -> str | None:
    marker = f"{name} "
    if marker not in command:
        return None
    value = command.split(marker, 1)[1].strip()
    next_arg = re.search(r"\s--[a-zA-Z0-9-]+(?:\s|$)", value)
    if next_arg and name != "--static-root":
        value = value[: next_arg.start()].strip()
    return value.strip("\"'") or None


def _process_arg_int(command: str, name: str) -> int | None:
    value = _process_arg_value(command, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _int_value(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _terminate_stale_backend_processes(*, current_pid: int, current_port: int) -> dict[str, list[str]]:
    checks: list[str] = []
    fixed: list[str] = []
    warnings: list[str] = []
    processes = _list_codex_switch_backend_processes()
    protected_process_ids = _current_backend_process_group_ids(processes, current_pid)
    stale_processes = [process for process in processes if process["pid"] not in protected_process_ids]

    checks.append(
        f"Found {len(processes)} codex switch backend process"
        f"{'' if len(processes) == 1 else 'es'}."
    )
    if len(protected_process_ids) > 1:
        protected_label = ", ".join(str(pid) for pid in sorted(protected_process_ids))
        checks.append(f"Protected current packaged backend process group: {protected_label}.")

    for process in stale_processes:
        pid = int(process["pid"])
        port = process.get("port")
        port_label = f" on port {port}" if port else ""
        try:
            _terminate_process_tree(pid)
            fixed.append(f"Stopped stale backend process {pid}{port_label}.")
        except OSError as error:
            warnings.append(f"Could not stop stale backend process {pid}{port_label}: {error}")

    if not stale_processes:
        checks.append(f"No stale backend process was found beside current port {current_port}.")
    return {"checks": checks, "fixed": fixed, "warnings": warnings}


def _current_backend_process_group_ids(processes: list[dict[str, Any]], current_pid: int) -> set[int]:
    parent_by_pid: dict[int, int] = {}
    children_by_parent_pid: dict[int, set[int]] = {}
    for process in processes:
        pid = _int_value(process.get("pid"))
        ppid = _int_value(process.get("ppid"))
        if pid is None or ppid is None:
            continue
        parent_by_pid[pid] = ppid
        children_by_parent_pid.setdefault(ppid, set()).add(pid)

    protected = {current_pid}

    pid = current_pid
    seen: set[int] = set()
    while pid not in seen:
        seen.add(pid)
        parent_pid = parent_by_pid.get(pid)
        if parent_pid is None or parent_pid not in parent_by_pid:
            break
        protected.add(parent_pid)
        pid = parent_pid

    pending = list(protected)
    while pending:
        parent_pid = pending.pop()
        for child_pid in children_by_parent_pid.get(parent_pid, set()):
            if child_pid in protected:
                continue
            protected.add(child_pid)
            pending.append(child_pid)

    return protected


def _terminate_process_tree(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if _is_windows():
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return
        time.sleep(0.05)
    if _process_exists(pid):
        os.kill(pid, signal.SIGKILL)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _is_address_in_use_error(error: OSError) -> bool:
    return getattr(error, "errno", None) in {48, 98} or "Address already in use" in str(error)


def _backend_is_healthy(host: str, port: int) -> bool:
    try:
        with urllib_request.urlopen(f"http://{host}:{port}/api/health", timeout=0.5) as response:
            return response.status == 200
    except (OSError, TimeoutError, ValueError, urllib_error.URLError):
        return False


def _port_conflict_message(host: str, port: int) -> str:
    if _backend_is_healthy(host, port):
        return (
            f"codex switch backend is already running at http://{host}:{port}. "
            "Quit the other app instance first, or start this copy with a different CODEX_SWITCH_PORT."
        )
    return (
        f"Port {port} on {host} is already in use by another process. "
        "Start codex switch with a different CODEX_SWITCH_PORT."
    )


def run_server(*, host: str, port: int, static_root: Path) -> None:
    static_root.mkdir(parents=True, exist_ok=True)
    server = SwitcherServer((host, port), static_root=static_root, store=ProfileStore())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the codex switch backend server.")
    parser.add_argument("--host", default=os.getenv("CODEX_SWITCH_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("CODEX_SWITCH_PORT", "8765")))
    parser.add_argument(
        "--static-root",
        default=str((Path(__file__).resolve().parents[1] / "web" / "dist")),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        run_server(host=args.host, port=args.port, static_root=Path(args.static_root).expanduser().resolve())
    except OSError as error:
        if _is_address_in_use_error(error):
            raise SystemExit(_port_conflict_message(args.host, args.port)) from None
        raise


if __name__ == "__main__":
    main()
