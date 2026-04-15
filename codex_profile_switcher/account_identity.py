from __future__ import annotations

import base64
import json
from pathlib import Path

from .profile_home import codex_home_path


class AccountIdentityService:
    def read(self, home_dir: str | Path) -> dict[str, str] | None:
        auth_path = codex_home_path(home_dir).joinpath("auth.json")
        if not auth_path.exists():
            return None

        try:
            payload = json.loads(auth_path.read_text(encoding="utf-8"))
            token = payload.get("tokens", {}).get("id_token")
            if not token:
                return None
            parts = token.split(".")
            if len(parts) < 2:
                return None
            encoded_payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(encoded_payload))
        except (OSError, ValueError, json.JSONDecodeError):
            return None

        identity: dict[str, str] = {}
        email = claims.get("email")
        name = claims.get("name")
        if isinstance(email, str) and email:
            identity["email"] = email
        if isinstance(name, str) and name:
            identity["name"] = name
        return identity or None
