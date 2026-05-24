from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import json
import time
from pathlib import Path

from .profile_home import codex_home_path, sync_profile_home

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


def _default_codex_home() -> Path:
    return Path.home().expanduser().resolve() / ".codex"


DEFAULT_CODEX_APP_PATH = _default_codex_app_path()
DEFAULT_CODEX_USER_DATA_DIR = _default_codex_user_data_dir()
DEFAULT_CODEX_HOME = _default_codex_home()
CODEX_VSCODE_URI = "vscode://openai.chatgpt/"
VSCODE_BUNDLE_ID = "com.microsoft.VSCode"
CODEX_TERMINATION_TIMEOUT_SECONDS = 5.0
CODEX_TERMINATION_POLL_INTERVAL_SECONDS = 0.1
VSCODE_TERMINATION_TIMEOUT_SECONDS = 8.0
FORCE_TERMINATION_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
WINDOWS_CODEX_APP_NAME = "Codex"


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
        if resolved_app.suffix.lower() == ".exe" and resolved_app.is_file():
            return resolved_app
        for candidate_name in ("Codex.exe", "codex.exe"):
            candidate = resolved_app / candidate_name
            if candidate.is_file():
                return candidate
        appx_executable = _windows_appx_codex_executable_path()
        if appx_executable is not None:
            return appx_executable
        if resolved_app.suffix.lower() == ".exe":
            return resolved_app
        return resolved_app / "Codex.exe"
    return resolved_app / "Contents" / "MacOS" / resolved_app.stem


def codex_launch_env(
    user_data_dir: Path | None = None,
    account_home_dir: Path | None = None,
) -> dict[str, str]:
    del user_data_dir, account_home_dir
    env = dict(os.environ)
    env["CODEX_HOME"] = str(DEFAULT_CODEX_HOME)
    env.setdefault("HOME", str(Path.home().expanduser().resolve()))
    env.setdefault("USERPROFILE", str(Path.home().expanduser().resolve()))
    return env


def codex_isolated_launch_env(account_home_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home_path(account_home_dir))
    return env


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
    quoted_executable = f'"{executable}"'
    single_quoted_executable = f"'{executable}'"
    return (
        command == executable
        or command.startswith(f"{executable} ")
        or command == quoted_executable
        or command.startswith(f"{quoted_executable} ")
        or command == single_quoted_executable
        or command.startswith(f"{single_quoted_executable} ")
    )


def _path_text_variants(path: Path) -> set[str]:
    resolved = str(path.expanduser().resolve())
    variants = {resolved}
    if os.name == "nt":
        variants.add(resolved.replace("/", "\\"))
    return variants


def _command_uses_default_codex_profile(command: str) -> bool:
    if "--user-data-dir" not in command:
        return True

    for default_dir in _path_text_variants(DEFAULT_CODEX_USER_DATA_DIR):
        escaped = re.escape(default_dir)
        path_pattern = rf"{escaped}[\\/]?"
        pattern = rf"--user-data-dir(?:=|\s+)(?:\"{path_pattern}\"|'{path_pattern}'|{path_pattern})(?=\s|$)"
        if re.search(pattern, command):
            return True
    return False


def running_codex_pids(codex_app_path: Path) -> list[int]:
    executable = str(codex_executable_path(codex_app_path))
    return [pid for pid, command in _read_process_listing() if _matches_codex_executable(command, executable)]


def running_default_codex_pids(codex_app_path: Path) -> list[int]:
    executable = str(codex_executable_path(codex_app_path))
    return [
        pid
        for pid, command in _read_process_listing()
        if _matches_codex_executable(command, executable) and _command_uses_default_codex_profile(command)
    ]


def _matches_vscode_main_process(command: str) -> bool:
    if os.name == "nt":
        normalized = command.replace("/", "\\").lower()
        return "\\code.exe" in normalized and "code helper" not in normalized
    if "/Visual Studio Code" not in command or "/Contents/Frameworks/" in command:
        return False
    return command.endswith(".app/Contents/MacOS/Code") or ".app/Contents/MacOS/Code " in command


def running_vscode_pids() -> list[int]:
    return [pid for pid, command in _read_process_listing() if _matches_vscode_main_process(command)]


def is_default_codex_running() -> bool:
    return bool(running_default_codex_pids(DEFAULT_CODEX_APP_PATH))


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
    pids = set(running_default_codex_pids(codex_app_path))
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

    if os.name == "nt":
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
    else:
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
    default_user_data_arg = f"--user-data-dir={DEFAULT_CODEX_USER_DATA_DIR.expanduser().resolve()}"
    if os.name == "nt":
        app_id = _windows_codex_app_id()
        executable = codex_executable_path(codex_app_path)
        if app_id and (_is_windows_apps_path(executable) or not executable.is_file()):
            return ["explorer.exe", f"shell:AppsFolder\\{app_id}"]
    executable = codex_executable_path(codex_app_path)
    return [str(executable), default_user_data_arg]


def build_codex_isolated_launch_command(
    codex_app_path: Path,
    user_data_dir: Path,
    account_home_dir: Path | None = None,
) -> list[str]:
    del account_home_dir
    executable = codex_executable_path(codex_app_path)
    if os.name == "nt":
        app_id = _windows_codex_app_id()
        if app_id and (_is_windows_apps_path(executable) or not executable.is_file()):
            raise ValueError(
                "Isolated Codex app launches need a direct Codex executable path. "
                "The Windows Store Codex launcher cannot receive an isolated profile argument."
            )
    return [str(executable), f"--user-data-dir={user_data_dir.expanduser().resolve()}"]


def launch_codex(
    codex_app_path: Path,
    user_data_dir: Path | None = None,
    account_home_dir: Path | None = None,
) -> None:
    del user_data_dir, account_home_dir
    terminate_running_codex(codex_app_path)
    command = build_codex_launch_command(codex_app_path)
    subprocess.Popen(
        command,
        env=codex_launch_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch_codex_isolated(
    codex_app_path: Path,
    user_data_dir: Path,
    account_home_dir: Path,
) -> None:
    resolved_user_data_dir = user_data_dir.expanduser().resolve()
    resolved_user_data_dir.mkdir(parents=True, exist_ok=True)
    resolved_account_home_dir = sync_profile_home(account_home_dir)
    command = build_codex_isolated_launch_command(
        codex_app_path,
        resolved_user_data_dir,
        resolved_account_home_dir,
    )
    subprocess.Popen(
        command,
        env=codex_isolated_launch_env(resolved_account_home_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def build_codex_vscode_extension_command() -> list[str]:
    if os.name == "nt":
        return ["explorer.exe", CODEX_VSCODE_URI]
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
    resolved = str(path.expanduser().resolve())
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", resolved])
    else:
        subprocess.Popen(["open", resolved])


def _windows_appx_codex_executable_path() -> Path | None:
    install_location = _windows_codex_appx_install_location()
    if install_location is None:
        return None
    executable = install_location / "app" / "Codex.exe"
    return executable if executable.is_file() else None


def _windows_codex_app_id() -> str | None:
    powershell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    script = (
        f"Get-StartApps | Where-Object {{ $_.Name -eq '{WINDOWS_CODEX_APP_NAME}' }} | "
        "Select-Object -First 1 -ExpandProperty AppID"
    )
    try:
        output = subprocess.check_output(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    for line in output.splitlines():
        app_id = line.strip()
        if app_id:
            return app_id
    return None


def _windows_codex_appx_install_location() -> Path | None:
    powershell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    script = "Get-AppxPackage -Name 'OpenAI.Codex' | Select-Object -First 1 -ExpandProperty InstallLocation"
    try:
        output = subprocess.check_output(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return None
    for line in output.splitlines():
        install_location = line.strip()
        if install_location:
            return Path(install_location)
    return None


def _is_windows_apps_path(path: Path) -> bool:
    return "windowsapps" in str(path).lower()
