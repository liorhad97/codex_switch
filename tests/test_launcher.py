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
    FORCE_TERMINATION_SIGNAL,
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
        with patch("codex_profile_switcher.launcher.os.name", "posix"):
            command = build_codex_vscode_extension_command()
        self.assertEqual(command, ["open", CODEX_VSCODE_URI])

    def test_build_codex_vscode_extension_command_uses_explorer_on_windows(self) -> None:
        with patch("codex_profile_switcher.launcher.os.name", "nt"):
            command = build_codex_vscode_extension_command()

        self.assertEqual(command, ["explorer.exe", CODEX_VSCODE_URI])

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
        with patch("codex_profile_switcher.launcher._read_process_listing", return_value=[(123, str(executable))]):
            self.assertTrue(is_default_codex_running())

    def test_running_codex_pids_detects_matching_process(self) -> None:
        executable = codex_executable_path(DEFAULT_CODEX_APP_PATH)
        process_listing = [(123, str(executable)), (456, "/bin/bash")]
        with patch("codex_profile_switcher.launcher._read_process_listing", return_value=process_listing):
            self.assertEqual(running_codex_pids(DEFAULT_CODEX_APP_PATH), [123])

    def test_running_vscode_pids_detects_main_code_process(self) -> None:
        process_listing = [
            (123, "/Applications/Visual Studio Code.app/Contents/MacOS/Code"),
            (456, "/Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper.app/Contents/MacOS/Code Helper"),
            (789, "/bin/bash"),
        ]
        with (
            patch("codex_profile_switcher.launcher.os.name", "posix"),
            patch("codex_profile_switcher.launcher._read_process_listing", return_value=process_listing),
        ):
            self.assertEqual(running_vscode_pids(), [123])

    def test_running_vscode_pids_detects_windows_code_process(self) -> None:
        process_listing = [
            (123, r"C:\Users\Owner\AppData\Local\Programs\Microsoft VS Code\Code.exe"),
            (456, r"C:\Users\Owner\AppData\Local\Programs\Microsoft VS Code\Code Helper.exe"),
            (789, r"C:\Windows\System32\cmd.exe"),
        ]
        with (
            patch("codex_profile_switcher.launcher.os.name", "nt"),
            patch("codex_profile_switcher.launcher._read_process_listing", return_value=process_listing),
        ):
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
            [call(123, signal.SIGTERM), call(123, FORCE_TERMINATION_SIGNAL)],
        )
        self.assertEqual(wait_for_exit.call_count, 2)

    def test_launch_codex_restarts_existing_instance_before_reopening(self) -> None:
        executable = codex_executable_path(DEFAULT_CODEX_APP_PATH)
        with (
            patch("codex_profile_switcher.launcher.terminate_running_codex") as terminate_running,
            patch("codex_profile_switcher.launcher._windows_codex_app_id", return_value=None),
            patch("subprocess.Popen") as popen,
        ):
            launch_codex(DEFAULT_CODEX_APP_PATH)

        terminate_running.assert_called_once_with(DEFAULT_CODEX_APP_PATH)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [str(executable)])

    def test_build_codex_launch_command_uses_windows_store_app_id_for_appx(self) -> None:
        appx_executable = Path(
            r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0.0.0_x64__2p2nqsd0c76g0\app\Codex.exe"
        )
        with (
            patch("codex_profile_switcher.launcher.os.name", "nt"),
            patch("codex_profile_switcher.launcher.codex_executable_path", return_value=appx_executable),
            patch("codex_profile_switcher.launcher._windows_codex_app_id", return_value="OpenAI.Codex_2p2nqsd0c76g0!App"),
        ):
            command = build_codex_launch_command(Path(r"C:\Unused\Codex.exe"))

        self.assertEqual(command, ["explorer.exe", r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"])

    def test_codex_executable_path_falls_back_to_windows_store_app(self) -> None:
        appx_executable = Path(
            r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0.0.0_x64__2p2nqsd0c76g0\app\Codex.exe"
        )
        configured_path = Path("/missing/Codex.exe")
        with (
            patch("codex_profile_switcher.launcher.os.name", "nt"),
            patch("codex_profile_switcher.launcher.Path.is_file", return_value=False),
            patch("codex_profile_switcher.launcher._windows_appx_codex_executable_path", return_value=appx_executable),
        ):
            self.assertEqual(codex_executable_path(configured_path), appx_executable)

    def test_terminate_running_vscode_quits_then_sigterms_stubborn_process(self) -> None:
        with (
            patch("codex_profile_switcher.launcher.os.name", "posix"),
            patch("codex_profile_switcher.launcher.running_vscode_pids", return_value=[123]),
            patch("codex_profile_switcher.launcher._wait_for_vscode_exit", side_effect=[{123}, set()]) as wait_for_exit,
            patch("subprocess.run") as run,
            patch("os.kill") as kill,
        ):
            self.assertTrue(terminate_running_vscode())

        run.assert_called_once()
        kill.assert_called_once_with(123, signal.SIGTERM)
        self.assertEqual(wait_for_exit.call_count, 2)

    def test_terminate_running_vscode_sigterms_windows_process(self) -> None:
        with (
            patch("codex_profile_switcher.launcher.os.name", "nt"),
            patch("codex_profile_switcher.launcher.running_vscode_pids", return_value=[123]),
            patch("codex_profile_switcher.launcher._wait_for_vscode_exit", return_value=set()) as wait_for_exit,
            patch("subprocess.run") as run,
            patch("os.kill") as kill,
        ):
            self.assertTrue(terminate_running_vscode())

        run.assert_not_called()
        kill.assert_called_once_with(123, signal.SIGTERM)
        wait_for_exit.assert_called_once_with({123}, 8.0)

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
