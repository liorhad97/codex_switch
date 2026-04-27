from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_profile_switcher.store import AppPaths, ProfileStore


class ProfileStoreTests(unittest.TestCase):
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

    def test_import_accounts_writes_snapshot_and_loads_from_managed_profiles_root(self) -> None:
        self._write_auth(self.paths.prepared_profiles_root / "alpha" / "home", "alpha-token")

        imported = self.store.import_accounts()

        self.assertEqual(len(imported), 1)
        self.assertTrue(self.paths.accounts_snapshot_path.exists())
        self.assertEqual(imported[0].id, "alpha")
        self.assertEqual(imported[0].source, "managed_profile")
        self.assertEqual(imported[0].status, "connected")
        self.assertEqual(imported[0].home_dir, (self.paths.prepared_profiles_root / "alpha" / "home").resolve())

        accounts, _config = self.store.load_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].id, "alpha")
        self.assertFalse(accounts[0].app_primary)
        self.assertFalse(accounts[0].flutty_primary)
        self.assertEqual(accounts[0].home_dir, (self.paths.prepared_profiles_root / "alpha" / "home").resolve())

    def test_import_preserves_snapshot_metadata_for_existing_managed_profiles(self) -> None:
        self._write_auth(self.paths.prepared_profiles_root / "alpha" / "home", "alpha-token")
        self.paths.accounts_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.accounts_snapshot_path.write_text(
            json.dumps(
                [
                    {
                        "id": "alpha",
                        "label": "alpha@example.com",
                        "home_dir": str(self.paths.prepared_profiles_root / "alpha" / "home"),
                        "status": "connected",
                        "enabled": True,
                        "source": "imported_db",
                        "identity": {"email": "alpha@example.com"},
                    }
                ]
            ),
            encoding="utf-8",
        )

        imported = self.store.import_accounts()

        self.assertEqual(imported[0].label, "alpha@example.com")
        self.assertEqual(imported[0].identity, {"email": "alpha@example.com"})
        self.assertEqual(imported[0].source, "managed_profile")

    def test_load_accounts_prefers_switcher_primary_not_scan_order(self) -> None:
        self._write_auth(self.paths.prepared_profiles_root / "alpha" / "home", "alpha-token")
        self._write_auth(self.paths.prepared_profiles_root / "beta" / "home", "beta-token")
        self.store.import_accounts()

        config = self.store.load_config()
        self.store.set_primary(config, "beta")

        accounts, loaded_config = self.store.load_accounts()
        self.assertEqual(loaded_config.primary_account_id, "beta")
        self.assertEqual(accounts[0].id, "beta")
        self.assertTrue(accounts[0].app_primary)
        self.assertFalse(accounts[1].app_primary)

    def test_set_primary_copies_account_auth_to_main_codex_home(self) -> None:
        source_auth = self._write_auth(self.paths.prepared_profiles_root / "alpha" / "home", "source-token")
        destination_auth = self._write_main_auth("old-token")

        config = self.store.load_config()
        self.store.import_accounts()
        updated = self.store.set_primary(config, "alpha")

        self.assertEqual(updated.primary_account_id, "alpha")
        self.assertEqual(
            json.loads(destination_auth.read_text(encoding="utf-8")),
            json.loads(source_auth.read_text(encoding="utf-8")),
        )
        backup_path = destination_auth.with_name("auth.json.codex-switch-backup")
        self.assertEqual(
            json.loads(backup_path.read_text(encoding="utf-8")),
            {"tokens": {"id_token": "old-token"}},
        )

    def test_set_primary_requires_account_auth(self) -> None:
        (self.paths.prepared_profiles_root / "alpha" / "home").mkdir(parents=True, exist_ok=True)
        self.store.import_accounts()

        with self.assertRaisesRegex(ValueError, "no Codex auth file"):
            self.store.set_primary(self.store.load_config(), "alpha")

        self.assertIsNone(self.store.load_config().primary_account_id)

    def test_add_local_account_creates_prepared_profile_and_marks_it_primary(self) -> None:
        account, config = self.store.add_local_account("Local alpha")

        self.assertEqual(account.label, "Local alpha")
        self.assertEqual(account.source, "local_created")
        self.assertTrue(account.home_dir.exists())
        self.assertTrue(account.home_dir.is_dir())
        self.assertEqual(config.primary_account_id, account.id)
        self.assertEqual(config.last_selected_account_id, account.id)
        self.assertEqual(config.launch_profiles[account.id], account.profile_root.resolve())

        accounts, loaded_config = self.store.load_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].id, account.id)
        self.assertTrue(accounts[0].app_primary)
        self.assertEqual(accounts[0].mapped_codex_profile, account.profile_root.resolve())
        self.assertEqual(loaded_config.primary_account_id, account.id)

    def test_import_preserves_local_accounts_when_profiles_exist(self) -> None:
        local_account, _config = self.store.add_local_account("Local alpha")
        self._write_auth(self.paths.prepared_profiles_root / "beta" / "home", "beta-token")

        imported = self.store.import_accounts()
        imported_ids = {account.id for account in imported}

        self.assertIn(local_account.id, imported_ids)
        self.assertIn("beta", imported_ids)
        merged_local = next(account for account in imported if account.id == local_account.id)
        self.assertEqual(merged_local.source, "local_created")
        self.assertEqual(merged_local.status, "pending_oauth")

    def test_persist_local_oauth_account_updates_snapshot_and_primary(self) -> None:
        account = self.store.persist_local_oauth_account(
            account_id="oauth-local-1",
            label="oauth@example.com",
            home_dir=self.paths.prepared_profiles_root / "oauth-local-1" / "home",
            auth_mode="chatgpt_oauth",
            identity={"email": "oauth@example.com"},
            rate_limits={"primary": {"usedPercent": 18}},
        )

        self.assertEqual(account.id, "oauth-local-1")
        self.assertEqual(account.source, "local_oauth")
        self.assertEqual(account.auth_mode, "chatgpt_oauth")

        accounts, config = self.store.load_accounts()
        self.assertEqual(accounts[0].id, "oauth-local-1")
        self.assertTrue(accounts[0].app_primary)
        self.assertEqual(accounts[0].identity["email"], "oauth@example.com")
        self.assertEqual(accounts[0].rate_limits["primary"]["usedPercent"], 18)
        self.assertEqual(config.primary_account_id, "oauth-local-1")
        self.assertEqual(config.launch_profiles["oauth-local-1"], account.profile_root.resolve())

    def test_remove_account_deletes_snapshot_profile_and_config_links(self) -> None:
        account, _config = self.store.add_local_account("Local alpha")

        updated_config = self.store.remove_account(account.id)

        accounts, config = self.store.load_accounts()
        self.assertEqual(accounts, [])
        self.assertFalse(account.profile_root.exists())
        self.assertIsNone(updated_config.primary_account_id)
        self.assertIsNone(updated_config.last_selected_account_id)
        self.assertNotIn(account.id, updated_config.launch_profiles)
        self.assertIsNone(config.primary_account_id)
        self.assertIsNone(config.last_selected_account_id)

    def test_load_accounts_drops_snapshot_accounts_outside_managed_profiles_root(self) -> None:
        legacy_home = self.root / "flutty_orc_data" / "profiles" / "alpha" / "home"
        self._write_auth(legacy_home, "legacy-token")
        self.paths.accounts_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.accounts_snapshot_path.write_text(
            json.dumps(
                [
                    {
                        "id": "alpha",
                        "label": "alpha@example.com",
                        "home_dir": str(legacy_home),
                        "status": "connected",
                        "enabled": True,
                        "flutty_primary": False,
                        "source": "imported_db",
                    }
                ]
            ),
            encoding="utf-8",
        )

        accounts, _config = self.store.load_accounts()

        self.assertEqual(accounts, [])
        snapshot = json.loads(self.paths.accounts_snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot, [])

    def test_load_config_normalizes_home_launch_profile_to_profile_root(self) -> None:
        self.paths.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.config_path.write_text(
            """
{
  "codex_app_path": "/Applications/Codex.app",
  "launch_profiles": {
    "local-1": "%s"
  },
  "last_selected_account_id": "local-1",
  "primary_account_id": "local-1"
}
"""
            % str(self.paths.prepared_profiles_root / "local-1" / "home"),
            encoding="utf-8",
        )

        config = self.store.load_config()

        self.assertEqual(
            config.launch_profiles["local-1"],
            (self.paths.prepared_profiles_root / "local-1").resolve(),
        )

    def _write_auth(self, home_dir: Path, token: str) -> Path:
        auth_path = home_dir / ".codex" / "auth.json"
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text(json.dumps({"tokens": {"id_token": token}}), encoding="utf-8")
        return auth_path

    def _write_main_auth(self, token: str) -> Path:
        auth_path = self.paths.main_codex_home / "auth.json"
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text(json.dumps({"tokens": {"id_token": token}}), encoding="utf-8")
        return auth_path


if __name__ == "__main__":
    unittest.main()
