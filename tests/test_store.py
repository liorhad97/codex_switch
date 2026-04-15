from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_profile_switcher.store import AppPaths, ProfileStore


class ProfileStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = AppPaths(
            source_flutty_data_root=self.root / "flutty_orc_data",
            source_relay_db_path=self.root / "flutty_orc_data" / "codex_relay.db",
            source_profiles_dir=self.root / "flutty_orc_data" / "profiles",
            source_accounts_dir=self.root / "flutty_orc_data" / "accounts",
            data_root=self.root / "codex_switch_data",
            config_path=self.root / "codex_switch_data" / "config.json",
            accounts_snapshot_path=self.root / "codex_switch_data" / "accounts.json",
            prepared_profiles_root=self.root / "codex_switch_data" / "prepared_profiles",
            main_codex_home=self.root / "main_codex_home",
        )
        self.paths.source_flutty_data_root.mkdir(parents=True, exist_ok=True)
        self.paths.source_profiles_dir.mkdir(parents=True, exist_ok=True)
        self.store = ProfileStore(self.paths)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_import_accounts_writes_snapshot_and_loads_from_codex_switch_data(self) -> None:
        self._write_accounts_db(
            [
                {
                    "id": "alpha",
                    "label": "alpha@example.com",
                    "home_dir": str(self.paths.source_profiles_dir / "alpha" / "home"),
                    "status": "connected",
                    "enabled": 1,
                    "is_primary": 1,
                    "created_at": "2026-04-01T10:00:00+00:00",
                    "updated_at": "2026-04-01T10:05:00+00:00",
                }
            ]
        )
        (self.paths.source_profiles_dir / "alpha" / "home").mkdir(parents=True, exist_ok=True)

        imported = self.store.import_accounts()
        self.assertEqual(len(imported), 1)
        self.assertTrue(self.paths.accounts_snapshot_path.exists())

        accounts, _config = self.store.load_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].id, "alpha")
        self.assertFalse(accounts[0].app_primary)
        self.assertTrue(accounts[0].flutty_primary)

    def test_load_accounts_prefers_switcher_primary_not_flutty_primary(self) -> None:
        self._write_accounts_db(
            [
                {
                    "id": "alpha",
                    "label": "alpha@example.com",
                    "home_dir": str(self.paths.source_profiles_dir / "alpha" / "home"),
                    "status": "connected",
                    "enabled": 1,
                    "is_primary": 1,
                    "created_at": "2026-04-01T10:00:00+00:00",
                    "updated_at": "2026-04-01T10:05:00+00:00",
                },
                {
                    "id": "beta",
                    "label": "beta@example.com",
                    "home_dir": str(self.paths.source_profiles_dir / "beta" / "home"),
                    "status": "connected",
                    "enabled": 1,
                    "is_primary": 0,
                    "created_at": "2026-04-02T10:00:00+00:00",
                    "updated_at": "2026-04-02T10:05:00+00:00",
                },
            ]
        )
        for account_id in ("alpha", "beta"):
            (self.paths.source_profiles_dir / account_id / "home").mkdir(parents=True, exist_ok=True)
        self.store.import_accounts()
        self._write_auth(self.paths.source_profiles_dir / "beta" / "home", "beta-token")

        config = self.store.load_config()
        self.store.set_primary(config, "beta")

        accounts, loaded_config = self.store.load_accounts()
        self.assertEqual(loaded_config.primary_account_id, "beta")
        self.assertEqual(accounts[0].id, "beta")
        self.assertTrue(accounts[0].app_primary)
        self.assertFalse(accounts[1].app_primary)

    def test_set_primary_copies_account_auth_to_main_codex_home(self) -> None:
        self._write_accounts_db(
            [
                {
                    "id": "alpha",
                    "label": "alpha@example.com",
                    "home_dir": str(self.paths.source_profiles_dir / "alpha" / "home"),
                    "status": "connected",
                    "enabled": 1,
                    "is_primary": 0,
                    "created_at": "2026-04-01T10:00:00+00:00",
                    "updated_at": "2026-04-01T10:05:00+00:00",
                }
            ]
        )
        source_auth = self._write_auth(self.paths.source_profiles_dir / "alpha" / "home", "source-token")
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
        self._write_accounts_db(
            [
                {
                    "id": "alpha",
                    "label": "alpha@example.com",
                    "home_dir": str(self.paths.source_profiles_dir / "alpha" / "home"),
                    "status": "connected",
                    "enabled": 1,
                    "is_primary": 0,
                    "created_at": "2026-04-01T10:00:00+00:00",
                    "updated_at": "2026-04-01T10:05:00+00:00",
                }
            ]
        )
        (self.paths.source_profiles_dir / "alpha" / "home").mkdir(parents=True, exist_ok=True)
        self.store.import_accounts()

        with self.assertRaisesRegex(ValueError, "no Codex auth file"):
            self.store.set_primary(self.store.load_config(), "alpha")

        self.assertIsNone(self.store.load_config().primary_account_id)

    def test_import_falls_back_to_source_profiles_scan(self) -> None:
        (self.paths.source_profiles_dir / "account-1" / "home").mkdir(parents=True, exist_ok=True)
        imported = self.store.import_accounts()
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].id, "account-1")
        self.assertEqual(imported[0].source, "imported_scan")

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

    def test_import_preserves_local_accounts(self) -> None:
        local_account, _config = self.store.add_local_account("Local alpha")
        self._write_accounts_db(
            [
                {
                    "id": "beta",
                    "label": "beta@example.com",
                    "home_dir": str(self.paths.source_profiles_dir / "beta" / "home"),
                    "status": "connected",
                    "enabled": 1,
                    "is_primary": 0,
                    "created_at": "2026-04-02T10:00:00+00:00",
                    "updated_at": "2026-04-02T10:05:00+00:00",
                }
            ]
        )
        (self.paths.source_profiles_dir / "beta" / "home").mkdir(parents=True, exist_ok=True)

        imported = self.store.import_accounts()
        imported_ids = {account.id for account in imported}

        self.assertIn(local_account.id, imported_ids)
        self.assertIn("beta", imported_ids)

    def test_load_accounts_prunes_legacy_pending_local_placeholders(self) -> None:
        legacy_account, _config = self.store.add_local_account()
        imported_account = self.store.persist_local_oauth_account(
            account_id="oauth-local-1",
            label="oauth@example.com",
            home_dir=self.paths.prepared_profiles_root / "oauth-local-1" / "home",
            auth_mode="chatgpt_oauth",
            identity={"email": "oauth@example.com"},
        )

        accounts, config = self.store.load_accounts()

        account_ids = {account.id for account in accounts}
        self.assertNotIn(legacy_account.id, account_ids)
        self.assertIn(imported_account.id, account_ids)
        self.assertNotIn(legacy_account.id, config.launch_profiles)
        self.assertEqual(config.primary_account_id, imported_account.id)

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

    def _write_accounts_db(self, rows: list[dict[str, object]]) -> None:
        connection = sqlite3.connect(str(self.paths.source_relay_db_path))
        with connection:
            connection.execute(
                """
                CREATE TABLE accounts (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    home_dir TEXT NOT NULL,
                    status TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO accounts (
                        id, label, home_dir, status, enabled, is_primary, created_at, updated_at
                    )
                    VALUES (:id, :label, :home_dir, :status, :enabled, :is_primary, :created_at, :updated_at)
                    """,
                    row,
                )
        connection.close()

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
