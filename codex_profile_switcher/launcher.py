from __future__ import annotations

import os
import signal
import shutil
import subprocess
import json
import time
from pathlib import Path

def _default_windows_local_appdata() -> Path:
    value = os.getenv("LOCALAPPDATA")
    return Path(value) if value else Path.home() / "AppData" / "Local"


def _default_windows_roaming_appdata() -> Path:
    value = os.getenv("APPDATA")
    return Path(value) if value else Path.home() / "AppData" / "Roaming"


def _default_codex_app_path() -> Path:
    if os.name == "nt":
        return _default_windows_local_appdata() / "Programs" / "Codex" / "Codex.exe"
    return Path("/Applications/Codex.app")


def _default_codex_user_data_dir() -> Path:
    if os.name == "nt":
        return _default_windows_roaming_appdata() / "Codex"
    return Path.home() / "Library" / "Application Support" / "Codex"


DEFAULT_CODEX_APP_PATH = _default_codex_app_path()
DEFAULT_CODEX_USER_DATA_DIR = _default_codex_user_data_dir()
CODEX_VSCODE_URI = "vscode://openai.chatgpt/"
VSCODE_BUNDLE_ID = "com.microsoft.VSCode"
CODEX_TERMINATION_TIMEOUT_SECONDS = 5.0
CODEX_TERMINATION_POLL_INTERVAL_SECONDS = 0.1
VSCODE_TERMINATION_TIMEOUT_SECONDS = 8.0
FORCE_TERMINATION_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _is_same_or_nested(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def is_safe_codex_user_data_dir(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    default_dir = DEFAULT_CODEX_USER_DATA_DIR.expanduser().resolve()
    return not (_is_same_or_nested(resolved, default_dir) or _is_same_or_nested(default_dir, resolved))


def codex_executable_path(codex_app_path: Path) -> Path:
    resolved_app = codex_app_path.expanduser().resolve()
    if os.name == "nt":
        if resolved_app.suffix.lower() == ".exe":
            return resolved_app
        for candidate_name in ("Codex.exe", "codex.exe"):
            candidate = resolved_app / candidate_name
            if candidate.is_file():
                return candidate
        return resolved_app / "Codex.exe"
    return resolved_app / "Contents" / "MacOS" / resolved_app.stem


def codex_launch_env(
    user_data_dir: Path | None = None,
    account_home_dir: Path | None = None,
) -> dict[str, str]:
    del user_data_dir, account_home_dir
    return dict(os.environ)


def _read_process_listing() -> list[tuple[int, str]]:
    if os.name == "nt":
        return _read_windows_process_listing()

    try:
        output = subprocess.check_output(["ps", "-Ao", "pid=,command="], text=True)
    except (OSError, subprocess.SubprocessError):
        return []

    processes: list[tuple[int, str]] = []
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        pid_text, command = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        processes.append((pid, command))
    return processes


def _read_windows_process_listing() -> list[tuple[int, str]]:
    powershell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        output = subprocess.check_output(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        payload = json.loads(output or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    entries = payload if isinstance(payload, list) else [payload]
    processes: list[tuple[int, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("ProcessId")
        command = entry.get("CommandLine")
        if isinstance(pid, int) and isinstance(command, str) and command:
            processes.append((pid, command))
    return processes


def _matches_codex_executable(command: str, executable: str) -> bool:
    return command == executable or command.startswith(f"{executable} ")


def running_codex_pids(codex_app_path: Path) -> list[int]:
    executable = str(codex_executable_path(codex_app_path))
    return [pid for pid, command in _read_process_listing() if _matches_codex_executable(command, executable)]


def _matches_vscode_main_process(command: str) -> bool:
    if "/Visual Studio Code" not in command or "/Contents/Frameworks/" in command:
        return False
    return command.endswith(".app/Contents/MacOS/Code") or ".app/Contents/MacOS/Code " in command


def running_vscode_pids() -> list[int]:
    return [pid for pid, command in _read_process_listing() if _matches_vscode_main_process(command)]


def is_default_codex_running() -> bool:
    default_dir = str(DEFAULT_CODEX_USER_DATA_DIR.expanduser().resolve())
    executable = str(codex_executable_path(DEFAULT_CODEX_APP_PATH))
    for _, command in _read_process_listing():
        if _matches_codex_executable(command, executable) and (
            f"--user-data-dir={default_dir}" in command or command == executable
        ):
            return True
    return False


def _wait_for_codex_exit(codex_app_path: Path, pids: set[int], timeout_seconds: float) -> set[int]:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    remaining = set(pids)
    while remaining:
        remaining &= set(running_codex_pids(codex_app_path))
        if not remaining:
            return set()
        if time.monotonic() >= deadline:
            return remaining
        time.sleep(CODEX_TERMINATION_POLL_INTERVAL_SECONDS)
    return set()


def _wait_for_vscode_exit(pids: set[int], timeout_seconds: float) -> set[int]:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    remaining = set(pids)
    while remaining:
        remaining &= set(running_vscode_pids())
        if not remaining:
            return set()
        if time.monotonic() >= deadline:
            return remaining
        time.sleep(CODEX_TERMINATION_POLL_INTERVAL_SECONDS)
    return set()


def terminate_running_codex(
    codex_app_path: Path,
    *,
    timeout_seconds: float = CODEX_TERMINATION_TIMEOUT_SECONDS,
) -> bool:
    pids = set(running_codex_pids(codex_app_path))
    if not pids:
        return False

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue

    stubborn_pids = _wait_for_codex_exit(codex_app_path, pids, timeout_seconds)
    if not stubborn_pids:
        return True

    for pid in stubborn_pids:
        try:
            os.kill(pid, FORCE_TERMINATION_SIGNAL)
        except OSError:
            continue

    _wait_for_codex_exit(codex_app_path, stubborn_pids, CODEX_TERMINATION_TIMEOUT_SECONDS)
    return True


def terminate_running_vscode(
    *,
    timeout_seconds: float = VSCODE_TERMINATION_TIMEOUT_SECONDS,
) -> bool:
    pids = set(running_vscode_pids())
    if not pids:
        return False

    try:
        subprocess.run(
            ["osascript", "-e", f'tell application id "{VSCODE_BUNDLE_ID}" to quit'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=min(max(timeout_seconds, 0.5), 5.0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    stubborn_pids = _wait_for_vscode_exit(pids, timeout_seconds)
    if not stubborn_pids:
        return True

    for pid in stubborn_pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue

    stubborn_pids = _wait_for_vscode_exit(stubborn_pids, CODEX_TERMINATION_TIMEOUT_SECONDS)
    if not stubborn_pids:
        return True

    for pid in stubborn_pids:
        try:
            os.kill(pid, FORCE_TERMINATION_SIGNAL)
        except OSError:
            continue

    _wait_for_vscode_exit(stubborn_pids, CODEX_TERMINATION_TIMEOUT_SECONDS)
    return True


def build_codex_launch_command(
    codex_app_path: Path,
    user_data_dir: Path | None = None,
    account_home_dir: Path | None = None,
) -> list[str]:
    del user_data_dir, account_home_dir
    executable = codex_executable_path(codex_app_path)
    return [str(executable)]


def launch_codex(
    codex_app_path: Path,
    user_data_dir: Path | None = None,
    account_home_dir: Path | None = None,
) -> None:
    del user_data_dir, account_home_dir
    terminate_running_codex(codex_app_path)
    executable = codex_executable_path(codex_app_path)
    subprocess.Popen(
        [str(executable)],
        env=codex_launch_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def build_codex_vscode_extension_command() -> list[str]:
    return ["open", CODEX_VSCODE_URI]


def launch_codex_vscode_extension() -> None:
    terminate_running_vscode()
    subprocess.Popen(
        build_codex_vscode_extension_command(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def reveal_in_finder(path: Path) -> None:
    subprocess.Popen(["open", str(path.expanduser().resolve())])
