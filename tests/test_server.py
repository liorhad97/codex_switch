from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from urllib import error as urllib_error
from urllib import request as urllib_request

from codex_profile_switcher.models import OAuthFlowSnapshot, SwitcherConfig
from codex_profile_switcher.server import (
    SwitcherServer,
    _FLUTTY_LIVE_STATE_CACHE,
    _list_codex_switch_backend_processes,
    _resolve_codex_binary,
    _terminate_stale_backend_processes,
    _windows_appx_codex_cli_candidates,
)
from codex_profile_switcher.store import AppPaths, ProfileStore


class FakeOAuthManager:
    def __init__(self, pending_flow: OAuthFlowSnapshot | None = None, store: ProfileStore | None = None) -> None:
        self.pending_flow = pending_flow
        self.store = store
        self.live_by_account_id: dict[str, dict[str, object]] = {}
        self.cached_by_account_id: dict[str, dict[str, object]] = {}
        self.errors_by_account_id: dict[str, Exception] = {}
        self.refreshed_account_ids: list[str] = []
        self.persist_account_flags: list[bool] = []
        self.cancelled_account_ids: list[str] = []
        self.cancelled_pending = False
        self.start_error: Exception | None = None

    def get_pending_flow(self) -> OAuthFlowSnapshot | None:
        return self.pending_flow

    def refresh_account_state(  # noqa: ANN001, ARG002
        self,
        account,
        *,
        refresh_rate_limits: bool = False,
        persist_account: bool = True,
    ):
        self.refreshed_account_ids.append(account.id)
        self.persist_account_flags.append(persist_account)
        if account.id in self.errors_by_account_id:
            raise self.errors_by_account_id[account.id]
        return self.live_by_account_id.get(account.id, {})

    def cached_account_state(self, account):  # noqa: ANN001
        return self.cached_by_account_id.get(account.id, {})

    def start_temporary(self) -> OAuthFlowSnapshot:
        if self.start_error is not None:
            raise self.start_error
        if self.pending_flow is None:
            self.pending_flow = OAuthFlowSnapshot(
                account_id="temp-account-1",
                status="awaiting_browser",
                verification_uri="https://chat.openai.com/auth/mock",
            )
        return self.pending_flow

    def start(self, account):  # noqa: ANN001
        flow = OAuthFlowSnapshot(
            account_id=account.id,
            status="awaiting_browser",
            verification_uri="https://chat.openai.com/auth/mock",
        )
        self.live_by_account_id[account.id] = {"oauth": asdict(flow)}
        return flow

    def cancel_pending(self) -> bool:
        if self.pending_flow is None:
            return False
        self.cancelled_pending = True
        self.pending_flow = None
        return True

    def cancel(self, account_id: str) -> bool:
        self.cancelled_account_ids.append(account_id)
        self.live_by_account_id.pop(account_id, None)
        if self.store is not None:
            self.store.update_local_account(
                account_id,
                status="disconnected",
                enabled=False,
                auth_mode=None,
                last_error=None,
                oauth=None,
            )
        return True

    def close(self, account_id: str) -> None:
        self.cancelled_account_ids.append(account_id)
        self.live_by_account_id.pop(account_id, None)

    def close_all(self) -> None:
        return None


class SwitcherServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = AppPaths(
            data_root=self.root / "codex_switch_data",
            config_path=self.root / "codex_switch_data" / "config.json",
            accounts_snapshot_path=self.root / "codex_switch_data" / "accounts.json",
            prepared_profiles_root=self.root / "llm_accounts_profiles" / "codex" / "profiles",
            main_codex_home=self.root / "main_codex_home",
        )
        self.store = ProfileStore(self.paths)
        self.static_root = self.root / "web-dist"
        self.static_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_build_state_merges_live_flutty_rate_limits(self) -> None:
        self._write_auth(self.paths.prepared_profiles_root / "alpha" / "home")
        self.store.import_accounts()

        server = SwitcherServer(("127.0.0.1", 0), static_root=self.static_root, store=self.store, oauth_manager=FakeOAuthManager())
        try:
            with patch(
                "codex_profile_switcher.server._load_flutty_live_accounts",
                return_value={
                    "alpha": {
                        "id": "alpha",
                        "identity": {"email": "alpha@example.com"},
                        "rate_limits": {
                            "primary": {
                                "usedPercent": 25,
                                "resetsAt": 1_800_000_000,
                                "windowDurationMins": 300,
                            }
                        },
                        "last_error": "Temporary warning",
                    }
                },
            ):
                payload = server.build_state()
        finally:
            server.server_close()

        self.assertEqual(len(payload["accounts"]), 1)
        self.assertEqual(payload["accounts"][0]["identity"]["email"], "alpha@example.com")
        self.assertEqual(payload["accounts"][0]["rate_limits"]["primary"]["usedPercent"], 25)
        self.assertEqual(payload["accounts"][0]["last_error"], "Temporary warning")

    def test_build_state_refreshes_all_auth_backed_managed_account_rate_limits_directly(self) -> None:
        for account_id in ("alpha", "beta"):
            self._write_auth(self.paths.prepared_profiles_root / account_id / "home")
        self.store.import_accounts()
        self.store.set_selected_account(self.store.load_config(), "beta")
        oauth_manager = FakeOAuthManager()
        oauth_manager.live_by_account_id["alpha"] = {
            "email": "alpha@example.com",
            "auth_mode": "chatgpt",
            "rate_limits": {
                "primary": {
                    "usedPercent": 9,
                    "resetsAt": 1_800_000_111,
                    "windowDurationMins": 300,
                },
            },
        }
        oauth_manager.live_by_account_id["beta"] = {
            "email": "beta@example.com",
            "auth_mode": "chatgpt",
            "rate_limits": {
                "primary": {
                    "usedPercent": 14,
                    "resetsAt": 1_800_000_123,
                    "windowDurationMins": 300,
                },
                "secondary": {
                    "usedPercent": 40,
                    "resetsAt": 1_800_001_234,
                    "windowDurationMins": 10080,
                },
            },
        }
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=oauth_manager,
        )
        try:
            payload = server.build_state()
        finally:
            server.server_close()

        by_id = {account["id"]: account for account in payload["accounts"]}
        self.assertEqual(oauth_manager.refreshed_account_ids, ["alpha", "beta"])
        self.assertEqual(oauth_manager.persist_account_flags, [False, False])
        self.assertEqual(by_id["alpha"]["rate_limits"]["primary"]["usedPercent"], 9)
        self.assertEqual(by_id["beta"]["rate_limits"]["primary"]["usedPercent"], 14)
        self.assertEqual(by_id["beta"]["auth_mode"], "chatgpt")

    def test_build_state_exposes_pending_oauth_flow(self) -> None:
        pending_flow = OAuthFlowSnapshot(
            account_id="temp-account-1",
            status="awaiting_browser",
            verification_uri="https://chat.openai.com/auth/mock",
        )
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=FakeOAuthManager(pending_flow),
        )
        try:
            payload = server.build_state()
        finally:
            server.server_close()

        self.assertEqual(payload["pending_oauth_flow"]["account_id"], "temp-account-1")
        self.assertEqual(payload["pending_oauth_flow"]["verification_uri"], "https://chat.openai.com/auth/mock")

    def test_cancel_pending_account_clears_pending_flow(self) -> None:
        pending_flow = OAuthFlowSnapshot(
            account_id="temp-account-1",
            status="awaiting_browser",
            verification_uri="https://chat.openai.com/auth/mock",
        )
        oauth_manager = FakeOAuthManager(pending_flow)
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=oauth_manager,
        )
        try:
            server.cancel_pending_account()
            payload = server.build_state()
        finally:
            server.server_close()

        self.assertTrue(oauth_manager.cancelled_pending)
        self.assertIsNone(payload["pending_oauth_flow"])

    def test_add_account_endpoint_returns_json_error_when_oauth_start_fails(self) -> None:
        oauth_manager = FakeOAuthManager()
        oauth_manager.start_error = RuntimeError("Codex app-server did not return a ChatGPT sign-in URL.")
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=oauth_manager,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib_request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/accounts/add",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib_error.HTTPError) as raised:
                urllib_request.urlopen(request, timeout=2)
            error_response = raised.exception
            try:
                payload = json.loads(error_response.read().decode("utf-8"))
            finally:
                error_response.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(raised.exception.code, 500)
        self.assertEqual(payload["error"], "Codex app-server did not return a ChatGPT sign-in URL.")

    def test_api_state_is_available_without_activation(self) -> None:
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=FakeOAuthManager(),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib_request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/api/state", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(payload["accounts"], [])
        self.assertIsNone(payload["selected_account_id"])

    def test_remove_account_clears_selection_and_deletes_profile(self) -> None:
        account, _config = self.store.add_local_account("Local alpha")
        oauth_manager = FakeOAuthManager(store=self.store)
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=oauth_manager,
        )
        try:
            server.set_selected(account.id)
            server.remove_account(account.id)
            payload = server.build_state()
        finally:
            server.server_close()

        self.assertEqual(payload["accounts"], [])
        self.assertIsNone(payload["selected_account_id"])
        self.assertIn(account.id, oauth_manager.cancelled_account_ids)
        self.assertFalse(account.profile_root.exists())

    def test_build_state_uses_local_oauth_manager_state_for_local_accounts(self) -> None:
        self.store.persist_local_oauth_account(
            account_id="local-1",
            label="local@example.com",
            home_dir=self.paths.prepared_profiles_root / "local-1" / "home",
            auth_mode="chatgpt_oauth",
        )
        oauth_manager = FakeOAuthManager()
        oauth_manager.live_by_account_id["local-1"] = {
            "email": "local@example.com",
            "name": "Local User",
            "auth_mode": "chatgpt_oauth",
            "rate_limits": {
                "primary": {
                    "usedPercent": 12,
                    "resetsAt": 1_800_000_123,
                    "windowDurationMins": 300,
                }
            },
            "oauth": {
                "account_id": "local-1",
                "status": "awaiting_browser",
                "verification_uri": "https://chat.openai.com/auth/mock",
            },
        }
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=oauth_manager,
        )
        try:
            payload = server.build_state()
        finally:
            server.server_close()

        self.assertEqual(payload["accounts"][0]["identity"]["email"], "local@example.com")
        self.assertEqual(payload["accounts"][0]["rate_limits"]["primary"]["usedPercent"], 12)
        self.assertEqual(payload["accounts"][0]["oauth"]["status"], "awaiting_browser")

    def test_build_state_surfaces_usage_refresh_errors_when_limits_are_missing(self) -> None:
        self.store.persist_local_oauth_account(
            account_id="local-1",
            label="local@example.com",
            home_dir=self.paths.prepared_profiles_root / "local-1" / "home",
            auth_mode="chatgpt_oauth",
        )
        self._write_auth(self.paths.prepared_profiles_root / "local-1" / "home")
        oauth_manager = FakeOAuthManager()
        oauth_manager.errors_by_account_id["local-1"] = RuntimeError("token invalidated")
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=oauth_manager,
        )
        try:
            payload = server.build_state(refresh_usage=True)
        finally:
            server.server_close()

        self.assertEqual(payload["accounts"][0]["last_error"], "Usage refresh failed: token invalidated")

    def test_fix_common_issues_clears_cache_refreshes_accounts_and_stops_stale_backends(self) -> None:
        self._write_auth(self.paths.prepared_profiles_root / "alpha" / "home")
        skeleton_root = self.paths.prepared_profiles_root / "skeleton-1"
        (skeleton_root / "home" / ".codex").mkdir(parents=True)
        _FLUTTY_LIVE_STATE_CACHE.update(
            {
                "api_base": "http://127.0.0.1:9999",
                "accounts": {"alpha": {"id": "alpha"}},
                "fetched_at": 10.0,
                "cooldown_until": 20.0,
            }
        )
        oauth_manager = FakeOAuthManager()
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=oauth_manager,
        )
        current_pid = os.getpid()
        stale_pid = current_pid + 1000
        try:
            with (
                patch(
                    "codex_profile_switcher.server._list_codex_switch_backend_processes",
                    return_value=[
                        {"pid": current_pid, "ppid": 1, "command": "current", "port": server.server_address[1]},
                        {"pid": stale_pid, "ppid": 1, "command": "stale", "port": 8765},
                    ],
                ),
                patch("codex_profile_switcher.server._terminate_process_tree") as terminate_process,
            ):
                payload = server.fix_common_issues()
        finally:
            server.server_close()

        terminate_process.assert_called_once_with(stale_pid)
        self.assertEqual(_FLUTTY_LIVE_STATE_CACHE["accounts"], {})
        self.assertIn("Cleared cached live usage state.", payload["fixed"])
        self.assertIn("Removed 1 skeleton profile: skeleton-1.", payload["fixed"])
        self.assertIn("Refreshed account discovery snapshot.", payload["fixed"])
        self.assertIn(f"Stopped stale backend process {stale_pid} on port 8765.", payload["fixed"])
        self.assertFalse(skeleton_root.exists())
        self.assertEqual(payload["state"]["accounts"][0]["id"], "alpha")

    def test_fix_common_issues_repairs_codex_binary_path(self) -> None:
        class RepairingOAuthManager(FakeOAuthManager):
            def __init__(self) -> None:
                super().__init__()
                self.codex_binary = "bad-codex.exe"

            def set_codex_binary(self, codex_binary: str) -> None:
                self.codex_binary = codex_binary

        oauth_manager = RepairingOAuthManager()
        with (
            patch("codex_profile_switcher.server._resolve_codex_binary", side_effect=["bad-codex.exe", "good-codex.exe"]),
            patch("codex_profile_switcher.server._list_codex_switch_backend_processes", return_value=[]),
        ):
            server = SwitcherServer(
                ("127.0.0.1", 0),
                static_root=self.static_root,
                store=self.store,
                oauth_manager=oauth_manager,
            )
            try:
                payload = server.fix_common_issues()
            finally:
                server.server_close()

        self.assertEqual(oauth_manager.codex_binary, "good-codex.exe")
        self.assertIn("Updated Codex CLI path to good-codex.exe.", payload["fixed"])

    def test_stale_backend_cleanup_protects_packaged_backend_wrapper_process(self) -> None:
        current_pid = os.getpid()
        wrapper_pid = current_pid + 1000
        stale_pid = current_pid + 2000

        with (
            patch(
                "codex_profile_switcher.server._list_codex_switch_backend_processes",
                return_value=[
                    {"pid": wrapper_pid, "ppid": 1, "command": "codex-switch-backend.exe", "port": 18765},
                    {"pid": current_pid, "ppid": wrapper_pid, "command": "codex-switch-backend.exe", "port": 18765},
                    {"pid": stale_pid, "ppid": 1, "command": "codex-switch-backend.exe", "port": 8765},
                ],
            ),
            patch("codex_profile_switcher.server._terminate_process_tree") as terminate_process,
        ):
            payload = _terminate_stale_backend_processes(current_pid=current_pid, current_port=18765)

        terminate_process.assert_called_once_with(stale_pid)
        self.assertIn(
            f"Protected current packaged backend process group: {current_pid}, {wrapper_pid}.",
            payload["checks"],
        )
        self.assertIn(f"Stopped stale backend process {stale_pid} on port 8765.", payload["fixed"])

    def test_cancel_account_sign_in_marks_local_account_disconnected(self) -> None:
        self.store.persist_local_oauth_account(
            account_id="local-1",
            label="local@example.com",
            home_dir=self.paths.prepared_profiles_root / "local-1" / "home",
            auth_mode="chatgpt_oauth",
        )
        self.store.update_local_account(
            "local-1",
            status="pending_oauth",
            enabled=True,
            oauth={
                "account_id": "local-1",
                "status": "awaiting_browser",
                "verification_uri": "https://chat.openai.com/auth/mock",
            },
        )
        oauth_manager = FakeOAuthManager(store=self.store)
        oauth_manager.live_by_account_id["local-1"] = {
            "oauth": {
                "account_id": "local-1",
                "status": "awaiting_browser",
                "verification_uri": "https://chat.openai.com/auth/mock",
            }
        }
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=oauth_manager,
        )
        try:
            server.cancel_account_sign_in("local-1")
            payload = server.build_state()
        finally:
            server.server_close()

        self.assertEqual(oauth_manager.cancelled_account_ids, ["local-1"])
        self.assertEqual(payload["accounts"][0]["status"], "disconnected")
        self.assertFalse(payload["accounts"][0]["enabled"])
        self.assertIsNone(payload["accounts"][0]["oauth"])

    def test_launch_account_sets_primary_and_launches_codex_normally(self) -> None:
        self.store.persist_local_oauth_account(
            account_id="local-1",
            label="local@example.com",
            home_dir=self.paths.prepared_profiles_root / "local-1" / "home",
            auth_mode="chatgpt_oauth",
        )
        self._write_auth(self.paths.prepared_profiles_root / "local-1" / "home")
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=FakeOAuthManager(),
        )
        try:
            with (
                patch(
                    "codex_profile_switcher.server.build_codex_launch_command",
                    return_value=["/Applications/Codex.app/Contents/MacOS/Codex"],
                ) as build_command,
                patch("codex_profile_switcher.server.launch_codex") as launch_codex,
            ):
                command = server.launch_account("local-1")
        finally:
            server.server_close()

        self.assertEqual(command, ["/Applications/Codex.app/Contents/MacOS/Codex"])
        build_command.assert_called_once()
        launch_codex.assert_called_once()
        self.assertEqual(build_command.call_args.args, (self.store.load_config().codex_app_path,))
        self.assertEqual(launch_codex.call_args.args, (self.store.load_config().codex_app_path,))
        self.assertEqual(server.store.load_config().primary_account_id, "local-1")

    def test_launch_account_isolated_uses_profile_without_changing_primary(self) -> None:
        self._write_auth(self.paths.prepared_profiles_root / "alpha" / "home")
        self._write_auth(self.paths.prepared_profiles_root / "beta" / "home")
        self.store.import_accounts()
        self.store.set_primary(self.store.load_config(), "beta")
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=FakeOAuthManager(),
        )
        try:
            accounts, config = self.store.load_accounts()
            alpha = next(account for account in accounts if account.id == "alpha")
            with (
                patch(
                    "codex_profile_switcher.server.build_codex_isolated_launch_command",
                    return_value=["/Applications/Codex.app/Contents/MacOS/Codex", "--user-data-dir=/tmp/alpha"],
                ) as build_command,
                patch("codex_profile_switcher.server.launch_codex_isolated") as launch_codex_isolated,
            ):
                command = server.launch_account_isolated("alpha")
        finally:
            server.server_close()

        self.assertEqual(command, ["/Applications/Codex.app/Contents/MacOS/Codex", "--user-data-dir=/tmp/alpha"])
        build_command.assert_called_once_with(config.codex_app_path, alpha.profile_root.resolve(), alpha.home_dir)
        launch_codex_isolated.assert_called_once_with(config.codex_app_path, alpha.profile_root.resolve(), alpha.home_dir)
        loaded_config = self.store.load_config()
        self.assertEqual(loaded_config.primary_account_id, "beta")
        self.assertEqual(loaded_config.last_selected_account_id, "alpha")

    def test_set_account_for_codex_vscode_sets_primary_and_opens_extension_uri(self) -> None:
        self.store.persist_local_oauth_account(
            account_id="local-1",
            label="local@example.com",
            home_dir=self.paths.prepared_profiles_root / "local-1" / "home",
            auth_mode="chatgpt_oauth",
        )
        self._write_auth(self.paths.prepared_profiles_root / "local-1" / "home")
        server = SwitcherServer(
            ("127.0.0.1", 0),
            static_root=self.static_root,
            store=self.store,
            oauth_manager=FakeOAuthManager(),
        )
        try:
            with (
                patch(
                    "codex_profile_switcher.server.build_codex_vscode_extension_command",
                    return_value=["open", "vscode://openai.chatgpt/"],
                ) as build_command,
                patch("codex_profile_switcher.server.launch_codex_vscode_extension") as launch_extension,
            ):
                command = server.set_account_for_codex_vscode("local-1")
        finally:
            server.server_close()

        self.assertEqual(command, ["open", "vscode://openai.chatgpt/"])
        build_command.assert_called_once_with()
        launch_extension.assert_called_once_with()
        self.assertEqual(server.store.load_config().primary_account_id, "local-1")

    def test_resolve_codex_binary_uses_configured_app_bundle_when_path_is_minimal(self) -> None:
        app_path = self.root / "Codex.app"
        binary_path = app_path / "Contents" / "Resources" / "codex"
        binary_path.parent.mkdir(parents=True)
        binary_path.write_text("#!/bin/sh\n", encoding="utf-8")
        binary_path.chmod(0o755)
        self.store.set_codex_app_path(self.store.load_config(), app_path)

        with (
            patch.dict(os.environ, {"CODEX_BINARY": ""}, clear=False),
            patch("codex_profile_switcher.server._is_windows", return_value=False),
            patch("codex_profile_switcher.server.shutil.which", return_value=None),
        ):
            self.assertEqual(_resolve_codex_binary(self.store), str(binary_path.resolve()))

    def test_resolve_codex_binary_uses_windows_resources_binary(self) -> None:
        app_path = self.root / "Programs" / "Codex" / "Codex.exe"
        binary_path = app_path.parent / "resources" / "codex.exe"
        binary_path.parent.mkdir(parents=True)
        app_path.write_text("gui", encoding="utf-8")
        binary_path.write_text("cli", encoding="utf-8")
        config = SwitcherConfig(
            primary_account_id=None,
            last_selected_account_id=None,
            codex_app_path=app_path,
            launch_profiles={},
        )

        with (
            patch.dict(os.environ, {"CODEX_BINARY": "", "LOCALAPPDATA": str(self.root)}, clear=False),
            patch("codex_profile_switcher.server._is_windows", return_value=True),
            patch("codex_profile_switcher.server.shutil.which", return_value=None),
            patch.object(self.store, "load_config", return_value=config),
        ):
            self.assertEqual(_resolve_codex_binary(self.store), str(binary_path.resolve()))

    def test_resolve_codex_binary_uses_windows_app_asar_unpacked_binary(self) -> None:
        app_path = self.root / "Programs" / "Codex" / "Codex.exe"
        binary_path = app_path.parent / "resources" / "app.asar.unpacked" / "bin" / "codex.exe"
        binary_path.parent.mkdir(parents=True)
        app_path.write_text("gui", encoding="utf-8")
        binary_path.write_text("cli", encoding="utf-8")
        config = SwitcherConfig(
            primary_account_id=None,
            last_selected_account_id=None,
            codex_app_path=app_path,
            launch_profiles={},
        )

        with (
            patch.dict(os.environ, {"CODEX_BINARY": "", "LOCALAPPDATA": str(self.root)}, clear=False),
            patch("codex_profile_switcher.server._is_windows", return_value=True),
            patch("codex_profile_switcher.server.shutil.which", return_value=None),
            patch.object(self.store, "load_config", return_value=config),
        ):
            self.assertEqual(_resolve_codex_binary(self.store), str(binary_path.resolve()))

    def test_resolve_codex_binary_checks_windows_roaming_npm_shim(self) -> None:
        appdata = self.root / "AppData" / "Roaming"
        binary_path = appdata / "npm" / "codex.cmd"
        binary_path.parent.mkdir(parents=True)
        binary_path.write_text("@echo off\n", encoding="utf-8")

        with (
            patch.dict(
                os.environ,
                {"CODEX_BINARY": "", "LOCALAPPDATA": str(self.root), "APPDATA": str(appdata)},
                clear=False,
            ),
            patch("codex_profile_switcher.server._is_windows", return_value=True),
            patch("codex_profile_switcher.server.shutil.which", return_value=None),
        ):
            self.assertEqual(Path(_resolve_codex_binary(self.store)).resolve(), binary_path.resolve())

    def test_resolve_codex_binary_checks_windows_vscode_extension_cli(self) -> None:
        binary_path = (
            self.root
            / ".vscode"
            / "extensions"
            / "openai.chatgpt-26.422.62136-win32-x64"
            / "bin"
            / "windows-x86_64"
            / "codex.exe"
        )
        binary_path.parent.mkdir(parents=True)
        binary_path.write_text("extension cli", encoding="utf-8")

        with (
            patch.dict(os.environ, {"CODEX_BINARY": "", "USERPROFILE": str(self.root)}, clear=False),
            patch("codex_profile_switcher.server._is_windows", return_value=True),
            patch("codex_profile_switcher.server.shutil.which", return_value=None),
            patch("codex_profile_switcher.server._windows_appx_codex_cli_candidates", return_value=[]),
            patch("codex_profile_switcher.server.Path.home", return_value=self.root),
        ):
            self.assertEqual(Path(_resolve_codex_binary(self.store)).resolve(), binary_path.resolve())

    def test_resolve_codex_binary_caches_windows_store_cli(self) -> None:
        package_root = self.root / "WindowsApps" / "OpenAI.Codex_test"
        source_cli = package_root / "app" / "resources" / "codex.exe"
        source_cli.parent.mkdir(parents=True)
        source_cli.write_text("store cli", encoding="utf-8")

        with (
            patch.dict(os.environ, {"CODEX_BINARY": ""}, clear=False),
            patch("codex_profile_switcher.server._is_windows", return_value=True),
            patch("codex_profile_switcher.server.shutil.which", return_value=None),
            patch("codex_profile_switcher.server.subprocess.check_output", return_value=f"{package_root}\n"),
            patch("codex_profile_switcher.server.Path.home", return_value=self.root),
        ):
            resolved = Path(_resolve_codex_binary(self.store))

        cached_cli = self.root / "codex_switch_data" / "codex_cli" / "codex.exe"
        self.assertEqual(resolved.resolve(), cached_cli.resolve())
        self.assertEqual(cached_cli.read_text(encoding="utf-8"), "store cli")

    def test_resolve_codex_binary_caches_windows_store_cli_with_powershell_copy(self) -> None:
        package_root = self.root / "WindowsApps" / "OpenAI.Codex_test"
        source_cli = package_root / "app" / "resources" / "codex.exe"
        cached_cli = self.root / "codex_switch_data" / "codex_cli" / "codex.exe"

        def fake_check_output(args, **_kwargs):  # noqa: ANN001
            script = args[-1]
            if "Get-AppxPackage" in script:
                return f"{package_root}\n"
            if "Copy-Item" in script:
                cached_cli.write_text("copied store cli", encoding="utf-8")
                return ""
            raise AssertionError(f"Unexpected PowerShell command: {args}")

        with (
            patch.dict(os.environ, {"CODEX_BINARY": ""}, clear=False),
            patch("codex_profile_switcher.server._is_windows", return_value=True),
            patch("codex_profile_switcher.server.shutil.which", return_value=None),
            patch("codex_profile_switcher.server.subprocess.check_output", side_effect=fake_check_output),
        ):
            resolved = Path(_resolve_codex_binary(self.store))

        self.assertEqual(resolved.resolve(), cached_cli.resolve())
        self.assertEqual(cached_cli.read_text(encoding="utf-8"), "copied store cli")
        self.assertFalse(source_cli.exists())

    def test_windows_appx_codex_cli_candidates_do_not_return_protected_source(self) -> None:
        package_root = self.root / "WindowsApps" / "OpenAI.Codex_test"
        source_cli = package_root / "app" / "resources" / "codex.exe"
        source_cli.parent.mkdir(parents=True)
        source_cli.write_text("store cli", encoding="utf-8")

        with (
            patch("codex_profile_switcher.server._is_windows", return_value=True),
            patch("codex_profile_switcher.server.subprocess.check_output", return_value=f"{package_root}\n"),
            patch("codex_profile_switcher.server._cache_windows_appx_codex_cli", return_value=None),
        ):
            self.assertEqual(_windows_appx_codex_cli_candidates(self.store), [])

    def test_add_account_retries_after_windows_codex_binary_repair(self) -> None:
        class RepairingOAuthManager(FakeOAuthManager):
            def __init__(self) -> None:
                super().__init__()
                self.codex_binary = "bad-codex.exe"
                self.start_calls = 0

            def set_codex_binary(self, codex_binary: str) -> None:
                self.codex_binary = codex_binary

            def start_temporary(self) -> OAuthFlowSnapshot:
                self.start_calls += 1
                if self.codex_binary == "bad-codex.exe":
                    raise RuntimeError("Could not start Codex app-server. Tried: bad-codex.exe.")
                return super().start_temporary()

        oauth_manager = RepairingOAuthManager()
        with (
            patch("codex_profile_switcher.server._is_windows", return_value=True),
            patch("codex_profile_switcher.server._resolve_codex_binary", side_effect=["bad-codex.exe", "good-codex.exe"]),
        ):
            server = SwitcherServer(
                ("127.0.0.1", 0),
                static_root=self.static_root,
                store=self.store,
                oauth_manager=oauth_manager,
            )
            try:
                flow = server.start_pending_account()
            finally:
                server.server_close()

        self.assertEqual(flow.verification_uri, "https://chat.openai.com/auth/mock")
        self.assertEqual(oauth_manager.codex_binary, "good-codex.exe")
        self.assertEqual(oauth_manager.start_calls, 2)

    def test_list_backend_processes_parses_windows_process_listing(self) -> None:
        output = json.dumps(
            [
                {
                    "ProcessId": 111,
                    "ParentProcessId": 1,
                    "CommandLine": "C:\\\\Other\\\\python.exe unrelated.py",
                },
                {
                    "ProcessId": 222,
                    "ParentProcessId": 1,
                    "CommandLine": (
                        "C:\\\\Program Files\\\\codex switch\\\\resources\\\\backend\\\\codex-switch-backend.exe "
                        "--host 127.0.0.1 --port 8765 --static-root C:\\\\Users\\\\Me\\\\AppData\\\\Local\\\\Temp"
                    ),
                },
            ]
        )

        with (
            patch("codex_profile_switcher.server._is_windows", return_value=True),
            patch("codex_profile_switcher.server.shutil.which", return_value="powershell"),
            patch("codex_profile_switcher.server.subprocess.check_output", return_value=output),
        ):
            processes = _list_codex_switch_backend_processes()

        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0]["pid"], 222)
        self.assertEqual(processes[0]["ppid"], 1)
        self.assertEqual(processes[0]["host"], "127.0.0.1")
        self.assertEqual(processes[0]["port"], 8765)

    def _write_auth(self, home_dir: Path) -> Path:
        auth_path = home_dir / ".codex" / "auth.json"
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text('{"tokens": {"id_token": "test-token"}}', encoding="utf-8")
        return auth_path


if __name__ == "__main__":
    unittest.main()
