from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .launcher import DEFAULT_CODEX_APP_PATH
from .models import AccountRecord, SwitcherConfig
from .profile_home import codex_home_path


@dataclass(frozen=True, slots=True)
class AppPaths:
    source_flutty_data_root: Path
    source_relay_db_path: Path
    source_profiles_dir: Path
    source_accounts_dir: Path
    data_root: Path
    config_path: Path
    accounts_snapshot_path: Path
    prepared_profiles_root: Path
    main_codex_home: Path

    @classmethod
    def default(cls) -> "AppPaths":
        home = Path.home().expanduser().resolve()
        source_root = home / "flutty_orc_data"
        data_root = home / "codex_switch_data"
        return cls(
            source_flutty_data_root=source_root,
            source_relay_db_path=source_root / "codex_relay.db",
            source_profiles_dir=source_root / "profiles",
            source_accounts_dir=source_root / "accounts",
            data_root=data_root,
            config_path=data_root / "config.json",
            accounts_snapshot_path=data_root / "accounts.json",
            prepared_profiles_root=data_root / "prepared_profiles",
            main_codex_home=home / ".codex",
        )


class ProfileStore:
    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.default()
        self.paths.data_root.mkdir(parents=True, exist_ok=True)
        self.paths.prepared_profiles_root.mkdir(parents=True, exist_ok=True)

    def load_config(self) -> SwitcherConfig:
        payload = self._read_json(self.paths.config_path)
        if not isinstance(payload, dict):
            return SwitcherConfig(
                primary_account_id=None,
                last_selected_account_id=None,
                codex_app_path=DEFAULT_CODEX_APP_PATH,
                launch_profiles={},
            )

        launch_profiles: dict[str, Path] = {}
        for account_id, raw_path in (payload.get("launch_profiles") or {}).items():
            if isinstance(account_id, str) and isinstance(raw_path, str) and raw_path.strip():
                launch_profiles[account_id] = self._normalize_launch_profile_path(Path(raw_path))

        raw_app_path = payload.get("codex_app_path")
        codex_app_path = (
            Path(raw_app_path).expanduser().resolve()
            if isinstance(raw_app_path, str) and raw_app_path.strip()
            else DEFAULT_CODEX_APP_PATH
        )
        primary_account_id = payload.get("primary_account_id")
        last_selected_account_id = payload.get("last_selected_account_id")
        return SwitcherConfig(
            primary_account_id=primary_account_id if isinstance(primary_account_id, str) else None,
            last_selected_account_id=last_selected_account_id if isinstance(last_selected_account_id, str) else None,
            codex_app_path=codex_app_path,
            launch_profiles=launch_profiles,
        )

    def save_config(self, config: SwitcherConfig) -> None:
        payload = {
            "primary_account_id": config.primary_account_id,
            "last_selected_account_id": config.last_selected_account_id,
            "codex_app_path": str(config.codex_app_path.expanduser().resolve()),
            "launch_profiles": {
                account_id: str(path.expanduser().resolve())
                for account_id, path in sorted(config.launch_profiles.items())
            },
        }
        self._write_json(self.paths.config_path, payload)

    def import_accounts(self) -> list[AccountRecord]:
        imported = self._load_accounts_from_source_db()
        if not imported:
            imported = self._scan_source_directories()
        local_accounts = [
            account
            for account in self._read_accounts_snapshot()
            if account.source.startswith("local_") and not self._is_legacy_placeholder(account)
        ]
        merged_accounts = self._merge_accounts(imported, local_accounts)
        self._write_accounts_snapshot(merged_accounts)
        return merged_accounts

    def add_local_account(self, label: str | None = None) -> tuple[AccountRecord, SwitcherConfig]:
        accounts = self._read_accounts_snapshot()
        created_at = datetime.now().astimezone()
        account_id = self._next_local_account_id(accounts, created_at=created_at)
        prepared_profile_dir = (self.paths.prepared_profiles_root / account_id).resolve()
        prepared_profile_dir.mkdir(parents=True, exist_ok=True)
        prepared_home_dir = prepared_profile_dir / "home"
        prepared_home_dir.mkdir(parents=True, exist_ok=True)

        account = AccountRecord(
            id=account_id,
            label=(label or "").strip() or self._next_local_account_label(accounts),
            home_dir=prepared_home_dir,
            status="pending_oauth",
            enabled=True,
            flutty_primary=False,
            created_at=created_at,
            updated_at=created_at,
            avatar_path=None,
            source="local_created",
        )

        self._write_accounts_snapshot(self._merge_accounts(accounts, [account]))

        config = self.load_config()
        launch_profiles = dict(config.launch_profiles)
        launch_profiles[account.id] = account.profile_root.resolve()
        updated_config = SwitcherConfig(
            primary_account_id=account.id,
            last_selected_account_id=account.id,
            codex_app_path=config.codex_app_path,
            launch_profiles=launch_profiles,
        )
        self.save_config(updated_config)
        return account, updated_config

    def persist_local_oauth_account(
        self,
        *,
        account_id: str,
        label: str,
        home_dir: Path,
        auth_mode: str | None,
        identity: dict[str, Any] | None = None,
        rate_limits: dict[str, Any] | None = None,
    ) -> AccountRecord:
        accounts = self._read_accounts_snapshot()
        now = datetime.now().astimezone()
        existing = next((account for account in accounts if account.id == account_id), None)
        resolved_home_dir = home_dir.expanduser().resolve()
        resolved_home_dir.mkdir(parents=True, exist_ok=True)
        account = AccountRecord(
            id=account_id,
            label=label.strip() or account_id,
            home_dir=resolved_home_dir,
            status="connected",
            enabled=True,
            flutty_primary=False,
            created_at=(existing.created_at if existing else now),
            updated_at=now,
            avatar_path=existing.avatar_path if existing else _find_avatar(resolved_home_dir),
            source="local_oauth",
            identity=identity,
            rate_limits=rate_limits,
            auth_mode=auth_mode,
            last_error=None,
        )
        self._write_accounts_snapshot(self._merge_accounts(accounts, [account]))

        config = self.load_config()
        launch_profiles = dict(config.launch_profiles)
        launch_profiles[account.id] = account.profile_root.resolve()
        updated_config = SwitcherConfig(
            primary_account_id=account.id,
            last_selected_account_id=account.id,
            codex_app_path=config.codex_app_path,
            launch_profiles=launch_profiles,
        )
        self.save_config(updated_config)
        return account

    def update_local_account(self, account_id: str, **updates: Any) -> AccountRecord | None:
        accounts = self._read_accounts_snapshot()
        updated_account: AccountRecord | None = None
        rendered_accounts: list[AccountRecord] = []

        for account in accounts:
            if account.id != account_id:
                rendered_accounts.append(account)
                continue
            if not account.source.startswith("local_"):
                rendered_accounts.append(account)
                continue

            auth_mode = account.auth_mode
            if "auth_mode" in updates:
                auth_mode = str(updates["auth_mode"]) if updates.get("auth_mode") else None

            last_error = account.last_error
            if "last_error" in updates:
                last_error = str(updates["last_error"]) if updates.get("last_error") else None

            updated_account = AccountRecord(
                id=account.id,
                label=str(updates.get("label") or account.label),
                home_dir=Path(updates.get("home_dir")).expanduser().resolve()
                if updates.get("home_dir")
                else account.home_dir,
                status=str(updates.get("status") or account.status),
                enabled=bool(updates["enabled"]) if "enabled" in updates else account.enabled,
                flutty_primary=account.flutty_primary,
                created_at=account.created_at,
                updated_at=datetime.now().astimezone(),
                avatar_path=account.avatar_path,
                mapped_codex_profile=account.mapped_codex_profile,
                app_primary=account.app_primary,
                source=account.source,
                identity=updates.get("identity") if "identity" in updates else account.identity,
                rate_limits=updates.get("rate_limits") if "rate_limits" in updates else account.rate_limits,
                auth_mode=auth_mode,
                oauth=updates.get("oauth") if "oauth" in updates else account.oauth,
                last_error=last_error,
                issues=list(account.issues),
            )
            rendered_accounts.append(updated_account)

        if updated_account is None:
            return None

        self._write_accounts_snapshot(rendered_accounts)
        return updated_account

    def load_accounts(self) -> tuple[list[AccountRecord], SwitcherConfig]:
        config = self.load_config()
        accounts = self._read_accounts_snapshot()
        accounts, config = self._prune_legacy_placeholders(accounts, config)
        if not accounts:
            accounts = self.import_accounts()
            config = self.load_config()
        resolved_primary = config.primary_account_id
        if resolved_primary and all(account.id != resolved_primary for account in accounts):
            resolved_primary = None

        for account in accounts:
            account.app_primary = account.id == resolved_primary
            account.mapped_codex_profile = config.launch_profiles.get(account.id)
            if not account.home_dir.exists():
                account.issues.append("Imported flutty profile home is missing.")

        accounts.sort(
            key=lambda account: (
                0 if account.app_primary else 1,
                0 if account.enabled else 1,
                account.created_at or datetime.max,
                account.title.lower(),
            )
        )
        return accounts, config

    def set_primary(self, config: SwitcherConfig, account_id: str) -> SwitcherConfig:
        self.copy_account_auth_to_main_codex(account_id)
        updated = SwitcherConfig(
            primary_account_id=account_id,
            last_selected_account_id=account_id,
            codex_app_path=config.codex_app_path,
            launch_profiles=dict(config.launch_profiles),
        )
        self.save_config(updated)
        return updated

    def copy_account_auth_to_main_codex(self, account_id: str) -> Path:
        account = next((account for account in self._read_accounts_snapshot() if account.id == account_id), None)
        if account is None:
            raise ValueError(f"Unknown account: {account_id}")

        source_auth_path = codex_home_path(account.home_dir) / "auth.json"
        if not source_auth_path.is_file():
            raise ValueError(f"Selected account has no Codex auth file at: {source_auth_path}")
        self._validate_auth_json(source_auth_path)

        destination_auth_path = self.paths.main_codex_home.expanduser().resolve() / "auth.json"
        if _same_file(source_auth_path, destination_auth_path):
            return destination_auth_path

        destination_auth_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = destination_auth_path.with_name(f"{destination_auth_path.name}.codex-switch-backup")
        if destination_auth_path.exists() and not backup_path.exists():
            shutil.copy2(destination_auth_path, backup_path)

        temp_path = destination_auth_path.with_name(f"{destination_auth_path.name}.tmp")
        shutil.copy2(source_auth_path, temp_path)
        temp_path.replace(destination_auth_path)
        return destination_auth_path

    def set_selected_account(self, config: SwitcherConfig, account_id: str | None) -> SwitcherConfig:
        updated = SwitcherConfig(
            primary_account_id=config.primary_account_id,
            last_selected_account_id=account_id,
            codex_app_path=config.codex_app_path,
            launch_profiles=dict(config.launch_profiles),
        )
        self.save_config(updated)
        return updated

    def set_launch_profile(self, config: SwitcherConfig, account_id: str, profile_dir: Path) -> SwitcherConfig:
        launch_profiles = dict(config.launch_profiles)
        launch_profiles[account_id] = self._normalize_launch_profile_path(profile_dir)
        updated = SwitcherConfig(
            primary_account_id=config.primary_account_id,
            last_selected_account_id=account_id,
            codex_app_path=config.codex_app_path,
            launch_profiles=launch_profiles,
        )
        self.save_config(updated)
        return updated

    def clear_launch_profile(self, config: SwitcherConfig, account_id: str) -> SwitcherConfig:
        launch_profiles = dict(config.launch_profiles)
        launch_profiles.pop(account_id, None)
        updated = SwitcherConfig(
            primary_account_id=config.primary_account_id,
            last_selected_account_id=account_id,
            codex_app_path=config.codex_app_path,
            launch_profiles=launch_profiles,
        )
        self.save_config(updated)
        return updated

    def set_codex_app_path(self, config: SwitcherConfig, codex_app_path: Path) -> SwitcherConfig:
        updated = SwitcherConfig(
            primary_account_id=config.primary_account_id,
            last_selected_account_id=config.last_selected_account_id,
            codex_app_path=codex_app_path.expanduser().resolve(),
            launch_profiles=dict(config.launch_profiles),
        )
        self.save_config(updated)
        return updated

    def _write_accounts_snapshot(self, accounts: list[AccountRecord]) -> None:
        payload = []
        for account in accounts:
            payload.append(
                {
                    "id": account.id,
                    "label": account.label,
                    "home_dir": str(account.home_dir),
                    "status": account.status,
                    "enabled": account.enabled,
                    "flutty_primary": account.flutty_primary,
                    "identity": account.identity,
                    "rate_limits": account.rate_limits,
                    "auth_mode": account.auth_mode,
                    "last_error": account.last_error,
                    "created_at": account.created_at.isoformat() if account.created_at else None,
                    "updated_at": account.updated_at.isoformat() if account.updated_at else None,
                    "avatar_path": str(account.avatar_path) if account.avatar_path else None,
                    "source": account.source,
                }
            )
        self._write_json(self.paths.accounts_snapshot_path, payload)

    def _read_accounts_snapshot(self) -> list[AccountRecord]:
        payload = self._read_json(self.paths.accounts_snapshot_path)
        if not isinstance(payload, list):
            return []

        accounts: list[AccountRecord] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            account_id = entry.get("id")
            label = entry.get("label")
            home_dir = entry.get("home_dir")
            if not isinstance(account_id, str) or not account_id.strip():
                continue
            if not isinstance(label, str) or not label.strip():
                label = account_id
            if not isinstance(home_dir, str) or not home_dir.strip():
                continue
            avatar_path = entry.get("avatar_path")
            identity = entry.get("identity")
            rate_limits = entry.get("rate_limits")
            auth_mode = entry.get("auth_mode")
            last_error = entry.get("last_error")
            accounts.append(
                AccountRecord(
                    id=account_id,
                    label=label,
                    home_dir=Path(home_dir).expanduser(),
                    status=str(entry.get("status") or "unknown"),
                    enabled=bool(entry.get("enabled", True)),
                    flutty_primary=bool(entry.get("flutty_primary", False)),
                    identity=identity if isinstance(identity, dict) else None,
                    rate_limits=rate_limits if isinstance(rate_limits, dict) else None,
                    auth_mode=auth_mode if isinstance(auth_mode, str) and auth_mode else None,
                    last_error=last_error if isinstance(last_error, str) and last_error else None,
                    created_at=_parse_timestamp(entry.get("created_at")),
                    updated_at=_parse_timestamp(entry.get("updated_at")),
                    avatar_path=Path(avatar_path).expanduser() if isinstance(avatar_path, str) and avatar_path else None,
                    source=str(entry.get("source") or "snapshot"),
                )
            )
        return accounts

    def _load_accounts_from_source_db(self) -> list[AccountRecord]:
        db_path = self.paths.source_relay_db_path
        if not db_path.exists():
            return []

        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(str(db_path))
            connection.row_factory = sqlite3.Row
            with connection:
                tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "accounts" not in tables:
                    return []
                rows = connection.execute(
                    """
                    SELECT id, label, home_dir, status, enabled, is_primary, last_error, created_at, updated_at
                    FROM accounts
                    ORDER BY created_at ASC
                    """
                ).fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass

        accounts: list[AccountRecord] = []
        for row in rows:
            home_dir = Path(str(row["home_dir"])).expanduser()
            label = str(row["label"] or row["id"] or "").strip() or str(row["id"])
            if label.startswith("Pending account "):
                continue
            accounts.append(
                AccountRecord(
                    id=str(row["id"]),
                    label=label,
                    home_dir=home_dir,
                    status=str(row["status"] or "unknown"),
                    enabled=bool(row["enabled"]),
                    flutty_primary=bool(row["is_primary"]),
                    last_error=str(row["last_error"]) if row["last_error"] else None,
                    created_at=_parse_timestamp(row["created_at"]),
                    updated_at=_parse_timestamp(row["updated_at"]),
                    avatar_path=_find_avatar(home_dir),
                    source="imported_db",
                )
            )
        return accounts

    def _scan_source_directories(self) -> list[AccountRecord]:
        results: list[AccountRecord] = []
        seen_ids: set[str] = set()
        for root in (self.paths.source_accounts_dir, self.paths.source_profiles_dir):
            if not root.exists():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                home_dir = child / "home" if (child / "home").is_dir() else child
                account_id = child.name
                if account_id in seen_ids:
                    continue
                seen_ids.add(account_id)
                results.append(
                    AccountRecord(
                        id=account_id,
                        label=account_id,
                        home_dir=home_dir,
                        status="available",
                        enabled=True,
                        flutty_primary=False,
                        last_error=None,
                        created_at=_stat_datetime(child),
                        updated_at=_stat_datetime(home_dir),
                        avatar_path=_find_avatar(home_dir),
                        source="imported_scan",
                    )
                )
        return results

    def _read_json(self, path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp_path.replace(path)

    def _validate_auth_json(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Selected account auth file is not valid JSON: {path}") from error
        if not isinstance(payload, dict):
            raise ValueError(f"Selected account auth file is not a JSON object: {path}")

    def _merge_accounts(
        self,
        primary_accounts: list[AccountRecord],
        secondary_accounts: list[AccountRecord],
    ) -> list[AccountRecord]:
        merged: dict[str, AccountRecord] = {}
        for account in secondary_accounts:
            merged[account.id] = account
        for account in primary_accounts:
            merged[account.id] = account
        return list(merged.values())

    def _next_local_account_id(
        self,
        accounts: list[AccountRecord],
        *,
        created_at: datetime,
    ) -> str:
        base = created_at.strftime("local-%Y%m%d-%H%M%S")
        existing_ids = {account.id for account in accounts}
        if base not in existing_ids:
            return base
        suffix = 2
        while f"{base}-{suffix}" in existing_ids:
            suffix += 1
        return f"{base}-{suffix}"

    def _next_local_account_label(self, accounts: list[AccountRecord]) -> str:
        local_count = sum(1 for account in accounts if account.source.startswith("local_"))
        return f"New isolated account {local_count + 1}"

    def _normalize_launch_profile_path(self, path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if resolved.name != "home":
            return resolved
        prepared_profiles_root = self.paths.prepared_profiles_root.expanduser().resolve()
        try:
            resolved.relative_to(prepared_profiles_root)
        except ValueError:
            return resolved
        return resolved.parent

    @staticmethod
    def _is_legacy_placeholder(account: AccountRecord) -> bool:
        return (
            account.source == "local_created"
            and account.status == "pending_oauth"
            and account.label.startswith("New isolated account ")
        )

    def _prune_legacy_placeholders(
        self,
        accounts: list[AccountRecord],
        config: SwitcherConfig,
    ) -> tuple[list[AccountRecord], SwitcherConfig]:
        legacy_account_ids = {
            account.id for account in accounts if self._is_legacy_placeholder(account)
        }
        if not legacy_account_ids:
            return accounts, config

        filtered_accounts = [
            account for account in accounts if account.id not in legacy_account_ids
        ]
        filtered_launch_profiles = {
            account_id: path
            for account_id, path in config.launch_profiles.items()
            if account_id not in legacy_account_ids
        }
        updated_config = SwitcherConfig(
            primary_account_id=(
                None if config.primary_account_id in legacy_account_ids else config.primary_account_id
            ),
            last_selected_account_id=(
                None
                if config.last_selected_account_id in legacy_account_ids
                else config.last_selected_account_id
            ),
            codex_app_path=config.codex_app_path,
            launch_profiles=filtered_launch_profiles,
        )
        self._write_accounts_snapshot(filtered_accounts)
        self.save_config(updated_config)
        return filtered_accounts, updated_config


def _parse_timestamp(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _stat_datetime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def _find_avatar(home_dir: Path) -> Path | None:
    candidates = [
        home_dir / "avatar.png",
        home_dir / "avatar.jpg",
        home_dir / "avatar.jpeg",
        home_dir.parent / "avatar.png",
        home_dir.parent / "avatar.jpg",
        home_dir.parent / "avatar.jpeg",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.expanduser().resolve().samefile(second.expanduser().resolve())
    except OSError:
        return first.expanduser().resolve() == second.expanduser().resolve()
