from __future__ import annotations

import base64
import hashlib
import json
import os
import platform as platform_module
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from .license_config import (
    DEFAULT_LICENSE_API_BASE,
    LEASE_REFRESH_WINDOW_SECONDS,
    LICENSE_PUBLIC_JWK,
)


ISSUER = "codex-switch-license"
AUDIENCE = "codex-switch-desktop"

_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = -3
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_G = (_P256_GX, _P256_GY)
Point = tuple[int, int] | None


class LicenseError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "license_error",
        status: int = 400,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.payload = payload or {}


class LicenseExpiredError(LicenseError):
    pass


@dataclass(frozen=True, slots=True)
class DecodedLease:
    header: dict[str, Any]
    claims: dict[str, Any]


def _utc_iso(unix_seconds: int | float | None = None) -> str:
    value = time.time() if unix_seconds is None else unix_seconds
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _json_decode_base64url(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(_base64url_decode(value).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise LicenseError("License token is not valid JSON.", code="invalid_lease") from error
    if not isinstance(payload, dict):
        raise LicenseError("License token payload is invalid.", code="invalid_lease")
    return payload


def _load_public_jwk() -> dict[str, Any] | None:
    raw = os.getenv("CODEX_SWITCH_LICENSE_PUBLIC_JWK")
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LicenseError("CODEX_SWITCH_LICENSE_PUBLIC_JWK is not valid JSON.", code="bad_public_key") from error
        if not isinstance(payload, dict):
            raise LicenseError("CODEX_SWITCH_LICENSE_PUBLIC_JWK must be a JSON object.", code="bad_public_key")
        return payload
    return dict(LICENSE_PUBLIC_JWK) if LICENSE_PUBLIC_JWK else None


def _install_hash(install_id: str) -> str:
    return hashlib.sha256(install_id.strip().encode("utf-8")).hexdigest()


def _public_key_point(public_jwk: dict[str, Any]) -> tuple[int, int]:
    if public_jwk.get("kty") != "EC" or public_jwk.get("crv") != "P-256":
        raise LicenseError("License public key must be a P-256 EC JWK.", code="bad_public_key")
    x_value = public_jwk.get("x")
    y_value = public_jwk.get("y")
    if not isinstance(x_value, str) or not isinstance(y_value, str):
        raise LicenseError("License public key is missing x or y.", code="bad_public_key")
    x = int.from_bytes(_base64url_decode(x_value), "big")
    y = int.from_bytes(_base64url_decode(y_value), "big")
    point = (x, y)
    if not _is_on_curve(point):
        raise LicenseError("License public key is not on the P-256 curve.", code="bad_public_key")
    return point


def _is_on_curve(point: Point) -> bool:
    if point is None:
        return True
    x, y = point
    return (y * y - (x * x * x + _P256_A * x + _P256_B)) % _P256_P == 0


def _inverse(value: int, modulus: int) -> int:
    return pow(value % modulus, -1, modulus)


def _point_add(left: Point, right: Point) -> Point:
    if left is None:
        return right
    if right is None:
        return left

    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % _P256_P == 0:
        return None
    if left == right:
        slope = (3 * x1 * x1 + _P256_A) * _inverse(2 * y1, _P256_P)
    else:
        slope = (y2 - y1) * _inverse(x2 - x1, _P256_P)
    slope %= _P256_P
    x3 = (slope * slope - x1 - x2) % _P256_P
    y3 = (slope * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _scalar_mult(scalar: int, point: Point) -> Point:
    result: Point = None
    addend = point
    value = scalar
    while value:
        if value & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        value >>= 1
    return result


def _decode_signature(signature: bytes) -> tuple[int, int]:
    if len(signature) == 64:
        return int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    if len(signature) < 8 or signature[0] != 0x30:
        raise LicenseError("License signature has an unsupported format.", code="invalid_lease")

    offset = 2
    if signature[1] & 0x80:
        offset = 2 + (signature[1] & 0x7F)
    if offset >= len(signature) or signature[offset] != 0x02:
        raise LicenseError("License signature is invalid.", code="invalid_lease")
    r_length = signature[offset + 1]
    r_bytes = signature[offset + 2 : offset + 2 + r_length]
    offset += 2 + r_length
    if offset >= len(signature) or signature[offset] != 0x02:
        raise LicenseError("License signature is invalid.", code="invalid_lease")
    s_length = signature[offset + 1]
    s_bytes = signature[offset + 2 : offset + 2 + s_length]
    return int.from_bytes(r_bytes, "big"), int.from_bytes(s_bytes, "big")


def _verify_es256(signing_input: bytes, signature: bytes, public_jwk: dict[str, Any]) -> None:
    r, s = _decode_signature(signature)
    if not (1 <= r < _P256_N and 1 <= s < _P256_N):
        raise LicenseError("License signature is invalid.", code="invalid_lease")
    public_point = _public_key_point(public_jwk)
    digest = hashlib.sha256(signing_input).digest()
    z = int.from_bytes(digest, "big")
    w = _inverse(s, _P256_N)
    u1 = (z * w) % _P256_N
    u2 = (r * w) % _P256_N
    point = _point_add(_scalar_mult(u1, _P256_G), _scalar_mult(u2, public_point))
    if point is None or point[0] % _P256_N != r:
        raise LicenseError("License signature is invalid.", code="invalid_lease")


def decode_lease(
    lease: str,
    *,
    public_jwk: dict[str, Any],
    install_id: str,
    now: float | None = None,
    allow_expired: bool = False,
) -> DecodedLease:
    parts = lease.split(".")
    if len(parts) != 3:
        raise LicenseError("License token has an invalid format.", code="invalid_lease")
    header = _json_decode_base64url(parts[0])
    claims = _json_decode_base64url(parts[1])
    if header.get("alg") != "ES256":
        raise LicenseError("License token uses an unsupported signature algorithm.", code="invalid_lease")
    _verify_es256(f"{parts[0]}.{parts[1]}".encode("ascii"), _base64url_decode(parts[2]), public_jwk)
    if claims.get("iss") != ISSUER or claims.get("aud") != AUDIENCE:
        raise LicenseError("License token is for the wrong product.", code="invalid_lease")
    if claims.get("status") != "active":
        raise LicenseError("License is not active.", code="inactive_license")
    if claims.get("install_hash") != _install_hash(install_id):
        raise LicenseError("License belongs to a different installation.", code="install_mismatch")
    expires_at = claims.get("exp")
    if not isinstance(expires_at, (int, float)):
        raise LicenseError("License token is missing an expiration.", code="invalid_lease")
    current_time = time.time() if now is None else now
    if expires_at <= current_time and not allow_expired:
        raise LicenseExpiredError("License needs to be refreshed.", code="expired_license")
    if not isinstance(claims.get("sub"), str) or not isinstance(claims.get("license_id"), str):
        raise LicenseError("License token is missing activation identifiers.", code="invalid_lease")
    return DecodedLease(header=header, claims=claims)


class LicenseManager:
    def __init__(
        self,
        data_root: Path,
        *,
        api_base: str | None = None,
        public_jwk: dict[str, Any] | None = None,
        now: Any = time.time,
    ) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.install_path = self.data_root / "install.json"
        self.license_path = self.data_root / "license.json"
        self.api_base = (api_base if api_base is not None else os.getenv("CODEX_SWITCH_LICENSE_API_BASE", DEFAULT_LICENSE_API_BASE)).rstrip("/")
        self.public_jwk = public_jwk if public_jwk is not None else _load_public_jwk()
        self.now = now

    def install_id(self) -> str:
        payload = self._read_json(self.install_path)
        install_id = payload.get("install_id") if isinstance(payload, dict) else None
        if isinstance(install_id, str) and len(install_id.strip()) >= 24:
            return install_id.strip()
        install_id = secrets.token_urlsafe(32)
        self._write_json(self.install_path, {"install_id": install_id, "created_at": _utc_iso(self.now())})
        return install_id

    def current_state(self, *, refresh: bool = False) -> dict[str, Any]:
        install_id = self.install_id()
        record = self._read_license_record()
        if record.get("revoked"):
            return self._inactive_state("revoked", "This license is no longer active.")
        lease = record.get("lease")
        if not isinstance(lease, str) or not lease:
            return self._inactive_state("missing", "Enter a license key to activate Codex Switch.")
        if self.public_jwk is None:
            return self._inactive_state("unconfigured", "The license public key is not configured.")

        try:
            decoded = decode_lease(lease, public_jwk=self.public_jwk, install_id=install_id, now=self.now())
        except LicenseExpiredError:
            if refresh:
                try:
                    return self.refresh_license()
                except LicenseError as error:
                    return self._inactive_state(error.code, error.message)
            return self._inactive_state("expired", "License refresh is required.")
        except LicenseError as error:
            return self._inactive_state(error.code, error.message)

        claims = decoded.claims
        expires_at = int(claims["exp"])
        if refresh and expires_at - self.now() <= LEASE_REFRESH_WINDOW_SECONDS:
            try:
                return self.refresh_license()
            except LicenseError as error:
                if error.code == "revoked":
                    return self._inactive_state("revoked", error.message)

        return self._active_state(claims)

    def activate(self, license_key: str) -> dict[str, Any]:
        if not isinstance(license_key, str) or not license_key.strip():
            raise LicenseError("Enter a license key.", code="missing_key")
        response = self._post_json(
            "/v1/activate",
            {
                "license_key": license_key,
                "install_id": self.install_id(),
                "app_version": os.getenv("CODEX_SWITCH_APP_VERSION", ""),
                "platform": platform_module.platform(),
            },
        )
        return self._accept_remote_lease(response)

    def refresh_license(self) -> dict[str, Any]:
        record = self._read_license_record()
        lease = record.get("lease")
        if not isinstance(lease, str) or not lease:
            raise LicenseError("No active license is stored on this installation.", code="missing_license")
        if self.public_jwk is None:
            raise LicenseError("The license public key is not configured.", code="unconfigured")
        decoded = decode_lease(
            lease,
            public_jwk=self.public_jwk,
            install_id=self.install_id(),
            now=self.now(),
            allow_expired=True,
        )
        response = self._post_json(
            "/v1/refresh",
            {
                "activation_id": decoded.claims["sub"],
                "install_id": self.install_id(),
                "app_version": os.getenv("CODEX_SWITCH_APP_VERSION", ""),
                "platform": platform_module.platform(),
            },
        )
        return self._accept_remote_lease(response)

    def _accept_remote_lease(self, response: dict[str, Any]) -> dict[str, Any]:
        lease = response.get("lease")
        if not isinstance(lease, str) or not lease:
            raise LicenseError("Activation server did not return a license lease.", code="bad_license_response")
        if self.public_jwk is None:
            raise LicenseError("The license public key is not configured.", code="unconfigured")
        decoded = decode_lease(lease, public_jwk=self.public_jwk, install_id=self.install_id(), now=self.now())
        claims = decoded.claims
        self._write_json(
            self.license_path,
            {
                "lease": lease,
                "activation_id": claims["sub"],
                "license_id": claims["license_id"],
                "expires_at": _utc_iso(claims["exp"]),
                "last_checked_at": _utc_iso(self.now()),
                "revoked": False,
            },
        )
        return self._active_state(claims)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_base:
            raise LicenseError("The license activation server is not configured.", code="unconfigured", status=503)
        request = urllib_request.Request(
            f"{self.api_base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "codex-switch-license/1",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=8) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
            except (OSError, ValueError):
                body = {}
            message = str(body.get("error") or "License server rejected the request.")
            code = str(body.get("code") or "license_server_error")
            if code == "revoked":
                self._mark_revoked(message)
            raise LicenseError(message, code=code, status=error.code, payload=body) from error
        except (OSError, TimeoutError, ValueError) as error:
            raise LicenseError(
                "Could not reach the license activation server.",
                code="network_error",
                status=503,
            ) from error
        if not isinstance(decoded, dict):
            raise LicenseError("License server returned an invalid response.", code="bad_license_response")
        return decoded

    def _active_state(self, claims: dict[str, Any]) -> dict[str, Any]:
        expires_at = int(claims["exp"])
        return {
            "licensed": True,
            "status": "active",
            "message": "Codex Switch is activated.",
            "activation_id": claims["sub"],
            "license_id": claims["license_id"],
            "expires_at": _utc_iso(expires_at),
            "api_configured": bool(self.api_base),
            "public_key_configured": self.public_jwk is not None,
            "install_id_suffix": self.install_id()[-8:],
        }

    def _inactive_state(self, status: str, message: str) -> dict[str, Any]:
        return {
            "licensed": False,
            "status": status,
            "message": message,
            "api_configured": bool(self.api_base),
            "public_key_configured": self.public_jwk is not None,
            "install_id_suffix": self.install_id()[-8:],
        }

    def _mark_revoked(self, message: str) -> None:
        record = self._read_license_record()
        record["revoked"] = True
        record["last_error"] = message
        record["last_checked_at"] = _utc_iso(self.now())
        self._write_json(self.license_path, record)

    def _read_license_record(self) -> dict[str, Any]:
        payload = self._read_json(self.license_path)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _read_json(path: Path) -> Any:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temp_path.replace(path)
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
