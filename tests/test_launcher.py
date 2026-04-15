from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from codex_profile_switcher.launcher import (
    DEFAULT_CODEX_APP_PATH,
    DEFAULT_CODEX_USER_DATA_DIR,
    build_codex_launch_command,
    codex_executable_path,
    codex_launch_env,
    is_default_codex_running,
    is_safe_codex_user_data_dir,
)


class LauncherTests(unittest.TestCase):
    def test_build_command_launches_codex_normally(self) -> None:
        command = build_codex_launch_command(DEFAULT_CODEX_APP_PATH)
        self.assertEqual(command, [str(codex_executable_path(DEFAULT_CODEX_APP_PATH))])

    def test_default_codex_profile_is_rejected(self) -> None:
        self.assertFalse(is_safe_codex_user_data_dir(DEFAULT_CODEX_USER_DATA_DIR))
        self.assertFalse(is_safe_codex_user_data_dir(DEFAULT_CODEX_USER_DATA_DIR / "Default"))
        self.assertTrue(is_safe_codex_user_data_dir(Path("/tmp/codex-switcher/account-2")))

    def test_launch_env_uses_default_environment(self) -> None:
        with patch.dict("os.environ", {"BASE_ENV": "1"}, clear=True):
            env = codex_launch_env(Path("/tmp/codex-switcher/account-3"), Path("/tmp/codex/home"))
        self.assertEqual(env, {"BASE_ENV": "1"})

    def test_build_command_ignores_legacy_profile_arguments(self) -> None:
        target = Path("/tmp/codex-switcher/account-4")
        account_home_dir = Path("/tmp/codex-switcher/account-4/home")
        command = build_codex_launch_command(DEFAULT_CODEX_APP_PATH, target, account_home_dir)
        self.assertEqual(command, [str(codex_executable_path(DEFAULT_CODEX_APP_PATH))])

    def test_detects_running_default_codex_instance(self) -> None:
        executable = codex_executable_path(DEFAULT_CODEX_APP_PATH)
        process_listing = f"123 {executable}\n"
        with patch("subprocess.check_output", return_value=process_listing):
            self.assertTrue(is_default_codex_running())


if __name__ == "__main__":
    unittest.main()
