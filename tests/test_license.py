from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_profile_switcher.license import LicenseError, LicenseManager, decode_lease


TEST_INSTALL_ID = "test-install-id-12345678901234567890"
TEST_PUBLIC_JWK = {
    "key_ops": ["verify"],
    "ext": True,
    "kty": "EC",
    "x": "fNEnVw70PCZiaoF-qD-5OOz_fBihN28QJnhjBqG9oGI",
    "y": "iP0m2wQO4tIvWLiFLh7Gcupa3bXzXczrGU0KQ53G7A0",
    "crv": "P-256",
}
TEST_LEASE = (
    "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6InRlc3QifQ."
    "eyJpc3MiOiJjb2RleC1zd2l0Y2gtbGljZW5zZSIsImF1ZCI6ImNvZGV4LXN3aXRjaC1kZXNrdG9wIiwic3ViIjoiYWN0X3Rlc3QiLCJsaWNlbnNlX2lkIjoibGljX3Rlc3QiLCJpbnN0YWxsX2hhc2giOiI4NjZlZGM5NWM5NzJhMzM2ZjM4MzIxY2Y2ZmNhMzBmMWZhNTljYTliOTFjMDNkOWI1ZTQ2ZTI2ZTBlMjk5NzZiIiwic3RhdHVzIjoiYWN0aXZlIiwiaWF0IjoxNzAwMDAwMDAwLCJleHAiOjQxMDI0NDQ4MDB9."
    "cbigFCdNHJfWGjKv0puTvOTV14dPxWlvFo_nQ6zZgYMz6RO_W7xRr8S9lnBSMludlzMl4cMP-9u09VWvvUwqxg"
)


class FakeRemoteLicenseManager(LicenseManager):
    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        self.last_remote_path = path
        self.last_remote_payload = payload
        return {"ok": True, "lease": TEST_LEASE}


class LicenseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_install_id(self) -> None:
        (self.root / "install.json").write_text(
            json.dumps({"install_id": TEST_INSTALL_ID}),
            encoding="utf-8",
        )

    def test_decode_lease_verifies_es256_signature_and_claims(self) -> None:
        decoded = decode_lease(
            TEST_LEASE,
            public_jwk=TEST_PUBLIC_JWK,
            install_id=TEST_INSTALL_ID,
            now=1_700_000_100,
        )

        self.assertEqual(decoded.claims["sub"], "act_test")
        self.assertEqual(decoded.claims["license_id"], "lic_test")

    def test_decode_lease_rejects_other_installations(self) -> None:
        with self.assertRaises(LicenseError) as raised:
            decode_lease(
                TEST_LEASE,
                public_jwk=TEST_PUBLIC_JWK,
                install_id="another-install-id-12345678901234567890",
                now=1_700_000_100,
            )

        self.assertEqual(raised.exception.code, "install_mismatch")

    def test_current_state_accepts_stored_valid_lease(self) -> None:
        self._write_install_id()
        (self.root / "license.json").write_text(
            json.dumps({"lease": TEST_LEASE}),
            encoding="utf-8",
        )
        manager = LicenseManager(self.root, public_jwk=TEST_PUBLIC_JWK, now=lambda: 1_700_000_100)

        state = manager.current_state()

        self.assertTrue(state["licensed"])
        self.assertEqual(state["activation_id"], "act_test")

    def test_current_state_marks_missing_license_inactive(self) -> None:
        self._write_install_id()
        manager = LicenseManager(self.root, public_jwk=TEST_PUBLIC_JWK, now=lambda: 1_700_000_100)

        state = manager.current_state()

        self.assertFalse(state["licensed"])
        self.assertEqual(state["status"], "missing")

    def test_activate_stores_verified_remote_lease_without_saving_raw_key(self) -> None:
        self._write_install_id()
        manager = FakeRemoteLicenseManager(
            self.root,
            api_base="https://license.example.test",
            public_jwk=TEST_PUBLIC_JWK,
            now=lambda: 1_700_000_100,
        )

        state = manager.activate("CSW-TEST1-TEST2-TEST3-TEST4")
        stored = json.loads((self.root / "license.json").read_text(encoding="utf-8"))

        self.assertTrue(state["licensed"])
        self.assertEqual(manager.last_remote_path, "/v1/activate")
        self.assertNotIn("license_key", stored)
        self.assertEqual(stored["activation_id"], "act_test")


if __name__ == "__main__":
    unittest.main()
