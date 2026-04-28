from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from codex_profile_switcher.models import OAuthFlowSnapshot
from codex_profile_switcher.server import SwitcherServer, _resolve_codex_binary
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
            patch("codex_profile_switcher.server.shutil.which", return_value=None),
        ):
            self.assertEqual(_resolve_codex_binary(self.store), str(binary_path.resolve()))

    def _write_auth(self, home_dir: Path) -> Path:
        auth_path = home_dir / ".codex" / "auth.json"
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text('{"tokens": {"id_token": "test-token"}}', encoding="utf-8")
        return auth_path


if __name__ == "__main__":
    unittest.main()
