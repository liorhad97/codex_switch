from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_profile_switcher.oauth import AccountOAuthManager
from codex_profile_switcher.profile_home import is_pending_oauth_profile
from codex_profile_switcher.store import AppPaths, ProfileStore


class OAuthManagerTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_start_temporary_cleans_profile_when_login_start_fails(self) -> None:
        class FailingConnection:
            instances: list["FailingConnection"] = []

            def __init__(self, **_kwargs) -> None:
                self.closed = False
                self.instances.append(self)

            def request(self, *_args, **_kwargs):
                raise RuntimeError("login start failed")

            def close(self) -> None:
                self.closed = True

        manager = self._manager(connection_class=FailingConnection)

        with self.assertRaisesRegex(RuntimeError, "login start failed"):
            manager.start_temporary()

        self.assertIsNone(manager.get_pending_flow())
        self.assertEqual(list(self.paths.prepared_profiles_root.iterdir()), [])
        self.assertTrue(FailingConnection.instances[0].closed)

    def test_temporary_oauth_profile_is_hidden_until_login_completes(self) -> None:
        class SuccessfulConnection:
            def __init__(self, **_kwargs) -> None:
                self.closed = False

            def request(self, method, _params=None, **_kwargs):
                if method == "account/login/start":
                    return {"authUrl": "https://chat.openai.com/auth/mock", "userCode": "ABC-123"}
                return {}

            def close(self) -> None:
                self.closed = True

        manager = self._manager(connection_class=SuccessfulConnection)

        flow = manager.start_temporary()
        self.assertEqual(flow.verification_uri, "https://chat.openai.com/auth/mock")
        self.assertEqual(self.store.import_accounts(), [])

        manager._handle_notification(flow.account_id, "account/login/completed", {"success": True})
        accounts, _config = self.store.load_accounts()

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].id, flow.account_id)
        self.assertEqual(accounts[0].source, "local_oauth")
        self.assertFalse(is_pending_oauth_profile(accounts[0].profile_root))

    def _manager(self, *, connection_class) -> AccountOAuthManager:
        patcher = patch("codex_profile_switcher.oauth.CodexAppServerConnection", connection_class)
        patcher.start()
        self.addCleanup(patcher.stop)
        manager = AccountOAuthManager(
            store=self.store,
            workspace_root=self.root,
            codex_binary="codex",
        )
        self.addCleanup(manager.close_all)
        return manager


if __name__ == "__main__":
    unittest.main()
