from __future__ import annotations

import argparse
import json
import os
import re
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
            flow = self.switcher.start_pending_account(label=label if isinstance(label, str) else None)
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
        codex_binary = os.getenv("CODEX_BINARY") or shutil.which("codex") or "codex"
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
        pending_flow = self.oauth.get_pending_flow()
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
            except Exception:
                direct_live = self.oauth.cached_account_state(account)
            return _merge_live_state(upstream_live, direct_live)
        return upstream_live if isinstance(upstream_live, dict) else None

    def import_accounts(self) -> None:
        self.store.import_accounts()

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


def _has_codex_auth(account: AccountRecord) -> bool:
    try:
        return (codex_home_path(account.home_dir) / "auth.json").is_file()
    except OSError:
        return False


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


def _detect_flutty_api_base() -> str | None:
    explicit = os.getenv("FLUTTY_ORC_API_BASE")
    if explicit:
        return explicit.rstrip("/")

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
