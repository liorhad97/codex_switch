from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_CODEX_APP_PATH = Path("/Applications/Codex.app")
DEFAULT_CODEX_USER_DATA_DIR = Path.home() / "Library" / "Application Support" / "Codex"


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
    return resolved_app / "Contents" / "MacOS" / resolved_app.stem


def codex_launch_env(
    user_data_dir: Path | None = None,
    account_home_dir: Path | None = None,
) -> dict[str, str]:
    del user_data_dir, account_home_dir
    return dict(os.environ)


def is_default_codex_running() -> bool:
    try:
        output = subprocess.check_output(["ps", "-Ao", "pid,command"], text=True)
    except (OSError, subprocess.SubprocessError):
        return False

    default_dir = str(DEFAULT_CODEX_USER_DATA_DIR.expanduser().resolve())
    executable = str(codex_executable_path(DEFAULT_CODEX_APP_PATH))
    for line in output.splitlines():
        if executable in line and (f"--user-data-dir={default_dir}" in line or line.strip().endswith(executable)):
            return True
    return False


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
    executable = codex_executable_path(codex_app_path)
    subprocess.Popen(
        [str(executable)],
        env=codex_launch_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def reveal_in_finder(path: Path) -> None:
    subprocess.Popen(["open", str(path.expanduser().resolve())])
