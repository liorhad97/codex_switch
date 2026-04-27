from __future__ import annotations

import signal
import unittest
from pathlib import Path
from unittest.mock import call, patch

from codex_profile_switcher.launcher import (
    DEFAULT_CODEX_APP_PATH,
    DEFAULT_CODEX_USER_DATA_DIR,
    CODEX_VSCODE_URI,
    build_codex_launch_command,
    build_codex_vscode_extension_command,
    codex_executable_path,
    codex_launch_env,
    is_default_codex_running,
    is_safe_codex_user_data_dir,
    launch_codex,
    launch_codex_vscode_extension,
    running_vscode_pids,
    running_codex_pids,
    terminate_running_vscode,
    terminate_running_codex,
)


class LauncherTests(unittest.TestCase):
    def test_build_command_launches_codex_normally(self) -> None:
        command = build_codex_launch_command(DEFAULT_CODEX_APP_PATH)
        self.assertEqual(command, [str(codex_executable_path(DEFAULT_CODEX_APP_PATH))])

    def test_build_codex_vscode_extension_command_opens_codex_extension_uri(self) -> None:
        command = build_codex_vscode_extension_command()
        self.assertEqual(command, ["open", CODEX_VSCODE_URI])

    def test_default_codex_profile_is_rejected(self) -> None:
        self.assertFalse(is_safe_codex_user_data_dir(DEFAULT_CODEX_USER_DATA_DIR))
        self.assertFalse(is_safe_codex_user_data_dir(DEFAULT_CODEX_USER_DATA_DIR / "Default"))
        self.assertTrue(is_safe_codex_user_data_dir(Path("/tmp/codex-switcher/account-2")))

    def test_launch_env_uses_default_environment(self) -> None:
        with patch.dict("os.environ", {"BASE_ENV": "1"}, clear=True):
            env = codex_launch_env(Path("/tmp/codex-switcher/account-3"), Path("/tmp/codex/home"))
        self.assertEqual(env, {"BASE_ENV": "1"})

    def test_build_command_ignores_profile_arguments(self) -> None:
        target = Path("/tmp/codex-switcher/account-4")
        account_home_dir = Path("/tmp/codex-switcher/account-4/home")
        command = build_codex_launch_command(DEFAULT_CODEX_APP_PATH, target, account_home_dir)
        self.assertEqual(command, [str(codex_executable_path(DEFAULT_CODEX_APP_PATH))])

    def test_detects_running_default_codex_instance(self) -> None:
        executable = codex_executable_path(DEFAULT_CODEX_APP_PATH)
        process_listing = f"123 {executable}\n"
        with patch("subprocess.check_output", return_value=process_listing):
            self.assertTrue(is_default_codex_running())

    def test_running_codex_pids_detects_matching_process(self) -> None:
        executable = codex_executable_path(DEFAULT_CODEX_APP_PATH)
        process_listing = f"123 {executable}\n456 /bin/bash\n"
        with patch("subprocess.check_output", return_value=process_listing):
            self.assertEqual(running_codex_pids(DEFAULT_CODEX_APP_PATH), [123])

    def test_running_vscode_pids_detects_main_code_process(self) -> None:
        process_listing = (
            "123 /Applications/Visual Studio Code.app/Contents/MacOS/Code\n"
            "456 /Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper.app/Contents/MacOS/Code Helper\n"
            "789 /bin/bash\n"
        )
        with patch("subprocess.check_output", return_value=process_listing):
            self.assertEqual(running_vscode_pids(), [123])

    def test_terminate_running_codex_sends_sigterm(self) -> None:
        with (
            patch("codex_profile_switcher.launcher.running_codex_pids", return_value=[123]),
            patch("codex_profile_switcher.launcher._wait_for_codex_exit", return_value=set()) as wait_for_exit,
            patch("os.kill") as kill,
        ):
            self.assertTrue(terminate_running_codex(DEFAULT_CODEX_APP_PATH))

        kill.assert_called_once_with(123, signal.SIGTERM)
        wait_for_exit.assert_called_once_with(DEFAULT_CODEX_APP_PATH, {123}, 5.0)

    def test_terminate_running_codex_force_kills_stuck_processes(self) -> None:
        with (
            patch("codex_profile_switcher.launcher.running_codex_pids", return_value=[123]),
            patch("codex_profile_switcher.launcher._wait_for_codex_exit", side_effect=[{123}, set()]) as wait_for_exit,
            patch("os.kill") as kill,
        ):
            self.assertTrue(terminate_running_codex(DEFAULT_CODEX_APP_PATH))

        self.assertEqual(
            kill.call_args_list,
            [call(123, signal.SIGTERM), call(123, signal.SIGKILL)],
        )
        self.assertEqual(wait_for_exit.call_count, 2)

    def test_launch_codex_restarts_existing_instance_before_reopening(self) -> None:
        executable = codex_executable_path(DEFAULT_CODEX_APP_PATH)
        with (
            patch("codex_profile_switcher.launcher.terminate_running_codex") as terminate_running,
            patch("subprocess.Popen") as popen,
        ):
            launch_codex(DEFAULT_CODEX_APP_PATH)

        terminate_running.assert_called_once_with(DEFAULT_CODEX_APP_PATH)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [str(executable)])

    def test_terminate_running_vscode_quits_then_sigterms_stubborn_process(self) -> None:
        with (
            patch("codex_profile_switcher.launcher.running_vscode_pids", return_value=[123]),
            patch("codex_profile_switcher.launcher._wait_for_vscode_exit", side_effect=[{123}, set()]) as wait_for_exit,
            patch("subprocess.run") as run,
            patch("os.kill") as kill,
        ):
            self.assertTrue(terminate_running_vscode())

        run.assert_called_once()
        kill.assert_called_once_with(123, signal.SIGTERM)
        self.assertEqual(wait_for_exit.call_count, 2)

    def test_launch_codex_vscode_extension_restarts_vscode_and_opens_extension_uri(self) -> None:
        with (
            patch("codex_profile_switcher.launcher.terminate_running_vscode") as terminate_vscode,
            patch("subprocess.Popen") as popen,
        ):
            launch_codex_vscode_extension()

        terminate_vscode.assert_called_once_with()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["open", CODEX_VSCODE_URI])


if __name__ == "__main__":
    unittest.main()
