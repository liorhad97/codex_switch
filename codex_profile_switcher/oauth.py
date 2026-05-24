from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .account_identity import AccountIdentityService
from .app_server import CodexAppServerConnection
from .models import AccountRecord, OAuthFlowSnapshot
from .profile_home import (
    ProfileHomeManager,
    clear_pending_oauth_profile,
    mark_pending_oauth_profile,
)
from .store import ProfileStore


@dataclass(slots=True)
class _SessionState:
    account_id: str
    home_dir: str
    persisted: bool
    connection: CodexAppServerConnection
    flow: OAuthFlowSnapshot | None = None
    auth_mode: str | None = None
    email: str | None = None
    plan_type: str | None = None
    rate_limits: dict[str, Any] | None = None
    last_rate_limit_refresh: datetime | None = None
    last_error: str | None = None


class AccountOAuthManager:
    def __init__(self, *, store: ProfileStore, workspace_root: Path, codex_binary: str = "codex") -> None:
        self._store = store
        self._workspace_root = workspace_root
        self._codex_binary = codex_binary
        self._homes = ProfileHomeManager(store.paths.prepared_profiles_root)
        self._identities = AccountIdentityService()
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionState] = {}

    def start_temporary(self) -> OAuthFlowSnapshot:
        pending = self.get_pending_flow()
        if pending is not None and pending.status != "error":
            return pending
        if pending is not None:
            self.close(pending.account_id)
            self._homes.delete_profile(pending.account_id)

        account_id = str(uuid4())
        home_dir = str(self._homes.ensure_profile_home(account_id))
        mark_pending_oauth_profile(self._homes.profile_root(account_id))
        try:
            session = self._ensure_session_state(account_id=account_id, home_dir=home_dir, persisted=False)
            session.flow = self._begin_login(session)
        except Exception:
            self.close(account_id)
            self._homes.delete_profile(account_id)
            raise
        return session.flow

    def start(self, account: AccountRecord) -> OAuthFlowSnapshot:
        session = self._ensure_session(account)
        session.flow = self._begin_login(session)
        self._store.update_local_account(
            account.id,
            status="pending_oauth",
            enabled=True,
            last_error=None,
            auth_mode=None,
            oauth=asdict(session.flow),
        )
        return session.flow

    def get_snapshot(self, account_id: str) -> OAuthFlowSnapshot | None:
        with self._lock:
            session = self._sessions.get(account_id)
            return session.flow if session else None

    def get_pending_flow(self) -> OAuthFlowSnapshot | None:
        with self._lock:
            sessions = list(self._sessions.values())

        for session in sessions:
            if session.persisted or session.flow is None:
                continue
            if session.flow.status == "connected":
                continue
            return session.flow
        return None

    def refresh_account_state(
        self,
        account: AccountRecord,
        *,
        refresh_rate_limits: bool = False,
        persist_account: bool = True,
    ) -> dict[str, Any]:
        session = self._ensure_session(account)
        account_result = session.connection.request("account/read", {"refreshToken": False})
        account_payload = account_result.get("account")

        email = None
        plan_type = None
        auth_mode = None
        if isinstance(account_payload, dict):
            email = account_payload.get("email")
            plan_type = account_payload.get("planType")
            auth_mode = account_payload.get("type")

        if persist_account and isinstance(email, str) and email:
            self._persist_session_account(session, email=email)
        elif persist_account and account.enabled and account.status == "pending_oauth":
            self._store.update_local_account(account.id, status="pending_oauth", enabled=True)

        identity = self._identities.read(account.home_dir)
        session.email = email or (identity or {}).get("email")
        session.plan_type = plan_type if isinstance(plan_type, str) else None
        session.auth_mode = auth_mode if isinstance(auth_mode, str) else None

        if refresh_rate_limits or self._rate_limits_stale(session):
            try:
                rate_limit_result = session.connection.request("account/rateLimits/read")
                rate_limits = rate_limit_result.get("rateLimits")
                session.rate_limits = rate_limits if isinstance(rate_limits, dict) else None
                session.last_rate_limit_refresh = datetime.now(timezone.utc)
                session.last_error = None
            except Exception as error:
                session.last_error = _format_account_state_error(error)

        return {
            "email": session.email,
            "name": (identity or {}).get("name"),
            "plan_type": session.plan_type,
            "auth_mode": session.auth_mode,
            "rate_limits": session.rate_limits,
            "last_error": session.last_error,
            "oauth": asdict(session.flow) if session.flow else None,
        }

    def cached_account_state(self, account: AccountRecord) -> dict[str, Any]:
        identity = self._identities.read(account.home_dir)
        with self._lock:
            session = self._sessions.get(account.id)

        if session is not None:
            return {
                "email": session.email or (identity or {}).get("email"),
                "name": (identity or {}).get("name"),
                "plan_type": session.plan_type,
                "auth_mode": session.auth_mode or account.auth_mode,
                "rate_limits": session.rate_limits or account.rate_limits,
                "last_error": session.last_error or account.last_error,
                "oauth": asdict(session.flow) if session.flow else account.oauth,
            }

        snapshot = self.get_snapshot(account.id)
        return {
            "email": (identity or {}).get("email"),
            "name": (identity or {}).get("name"),
            "plan_type": None,
            "auth_mode": account.auth_mode,
            "rate_limits": account.rate_limits,
            "last_error": account.last_error,
            "oauth": asdict(snapshot) if snapshot else account.oauth,
        }

    def close(self, account_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(account_id, None)
        if session:
            session.connection.close()

    def cancel_pending(self) -> bool:
        pending = self.get_pending_flow()
        if pending is None:
            return False
        self.close(pending.account_id)
        self._homes.delete_profile(pending.account_id)
        return True

    def cancel(self, account_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(account_id)

        if session is not None and not session.persisted:
            self.close(account_id)
            self._homes.delete_profile(account_id)
            return True

        self.close(account_id)
        updated_account = self._store.update_local_account(
            account_id,
            status="disconnected",
            enabled=False,
            auth_mode=None,
            last_error=None,
            oauth=None,
        )
        return updated_account is not None

    def close_all(self) -> None:
        with self._lock:
            account_ids = list(self._sessions.keys())
        for account_id in account_ids:
            self.close(account_id)

    def set_codex_binary(self, codex_binary: str) -> None:
        if codex_binary == self._codex_binary:
            return
        self.close_all()
        with self._lock:
            self._codex_binary = codex_binary

    def _ensure_session(self, account: AccountRecord) -> _SessionState:
        return self._ensure_session_state(
            account_id=account.id,
            home_dir=str(account.home_dir),
            persisted=True,
        )

    def _ensure_session_state(self, *, account_id: str, home_dir: str, persisted: bool) -> _SessionState:
        with self._lock:
            existing = self._sessions.get(account_id)
            if existing is not None:
                existing.home_dir = home_dir
                existing.persisted = existing.persisted or persisted
                return existing

            resolved_home_dir = Path(home_dir).expanduser().resolve()
            resolved_home_dir.mkdir(parents=True, exist_ok=True)
            connection = CodexAppServerConnection(
                codex_binary=self._codex_binary,
                workspace_root=self._workspace_root,
                home_dir=resolved_home_dir,
                notification_handler=lambda method, params, account_id=account_id: self._handle_notification(
                    account_id,
                    method,
                    params,
                ),
            )
            session = _SessionState(
                account_id=account_id,
                home_dir=str(resolved_home_dir),
                persisted=persisted,
                connection=connection,
            )
            self._sessions[account_id] = session
            return session

    def _handle_notification(self, account_id: str, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            session = self._sessions.get(account_id)
        if session is None:
            return

        if method == "account/login/completed":
            success = bool(params.get("success"))
            error = params.get("error")
            if session.flow is None:
                session.flow = OAuthFlowSnapshot(account_id=account_id, status="starting")
            if success:
                self._persist_session_account(session)
            else:
                session.flow.status = "error"
                session.flow.error = str(error) if error else "ChatGPT login failed."
                if session.persisted:
                    self._store.update_local_account(
                        account_id,
                        status="error",
                        enabled=False,
                        last_error=session.flow.error,
                        auth_mode=None,
                        oauth=asdict(session.flow),
                    )
            return

        if method == "account/updated":
            auth_mode = params.get("authMode")
            plan_type = params.get("planType")
            session.auth_mode = auth_mode if isinstance(auth_mode, str) else None
            session.plan_type = plan_type if isinstance(plan_type, str) else None
            if session.auth_mode == "chatgpt":
                self._persist_session_account(session)
            elif session.auth_mode is None and session.persisted:
                self._store.update_local_account(
                    account_id,
                    status="disconnected",
                    enabled=False,
                    auth_mode=None,
                    last_error=None,
                    oauth=None,
                )
            return

        if method == "account/rateLimits/updated":
            rate_limits = params.get("rateLimits")
            session.rate_limits = rate_limits if isinstance(rate_limits, dict) else None
            session.last_rate_limit_refresh = datetime.now(timezone.utc)
            session.last_error = None

    @staticmethod
    def _rate_limits_stale(session: _SessionState) -> bool:
        if session.last_rate_limit_refresh is None:
            return True
        return (datetime.now(timezone.utc) - session.last_rate_limit_refresh) > timedelta(seconds=30)

    @staticmethod
    def _begin_login(session: _SessionState) -> OAuthFlowSnapshot:
        result = session.connection.request("account/login/start", {"type": "chatgpt"})
        verification_uri = _string_value(result.get("authUrl"))
        settings_url = _string_value(result.get("settingsUrl"))
        help_url = _string_value(result.get("helpUrl"))
        if not (verification_uri or settings_url or help_url):
            raise RuntimeError("Codex app-server did not return a ChatGPT sign-in URL.")
        return OAuthFlowSnapshot(
            account_id=session.account_id,
            status="awaiting_browser",
            verification_uri=verification_uri,
            user_code=_string_value(result.get("userCode")),
            error=None,
            settings_url=settings_url,
            help_url=help_url,
            transcript=[],
        )

    def _persist_session_account(self, session: _SessionState, *, email: str | None = None) -> None:
        identity = self._identities.read(session.home_dir)
        label = email or session.email or (identity or {}).get("email") or "Connected account"
        clear_pending_oauth_profile(_profile_root_for_home(Path(session.home_dir)))
        account = self._store.persist_local_oauth_account(
            account_id=session.account_id,
            label=label,
            home_dir=Path(session.home_dir),
            auth_mode="chatgpt_oauth",
            identity=identity,
            rate_limits=session.rate_limits,
        )
        session.persisted = True
        session.email = label
        session.flow = None
        session.auth_mode = account.auth_mode


def _format_account_state_error(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    if len(message) > 240:
        message = f"{message[:237]}..."
    return f"Usage refresh failed: {message}"


def _profile_root_for_home(home_dir: Path) -> Path:
    resolved_home_dir = home_dir.expanduser().resolve()
    return resolved_home_dir.parent if resolved_home_dir.name == "home" else resolved_home_dir


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
