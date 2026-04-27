from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from .profile_home import codex_home_path, sync_profile_home


NotificationHandler = Callable[[str, dict[str, Any]], None]


class CodexAppServerConnection:
    def __init__(
        self,
        *,
        codex_binary: str,
        workspace_root: Path,
        home_dir: Path,
        notification_handler: NotificationHandler | None,
    ) -> None:
        self._codex_binary = codex_binary
        self._workspace_root = workspace_root
        self._home_dir = home_dir
        self._notification_handler = notification_handler
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._started = False
        self._next_id = 1
        self._pending: dict[int | str, queue.Queue[dict[str, Any]]] = {}
        self._stderr_tail: list[str] = []

    def start(self) -> None:
        with self._lock:
            if self._started and self._process and self._process.poll() is None:
                return

            env = os.environ.copy()
            env.setdefault("HOME", str(Path.home()))
            sync_profile_home(self._home_dir)
            env["CODEX_HOME"] = str(codex_home_path(self._home_dir))
            process = subprocess.Popen(
                [self._codex_binary, "app-server"],
                cwd=self._workspace_root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._process = process
            self._started = True

            self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader_thread.start()
            self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
            self._stderr_thread.start()

        self.request("initialize", self._build_initialize_params())
        self.notify("initialized", {})

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        self.start()
        request_id = self._allocate_id()
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self._register_pending_request(request_id, response_queue)
        self._send({"id": request_id, "method": method, "params": params or {}})
        try:
            payload = response_queue.get(timeout=timeout_seconds)
        except queue.Empty as error:
            raise self._timeout_error(method) from error
        finally:
            self._remove_pending_request(request_id)

        if payload.get("error") is not None:
            error_obj = payload["error"]
            if isinstance(error_obj, dict):
                message = error_obj.get("message") or json.dumps(error_obj)
            else:
                message = str(error_obj)
            raise RuntimeError(message)
        result = payload.get("result", {})
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.start()
        self._send({"method": method, "params": params or {}})

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._started = False
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    @property
    def stderr_tail(self) -> list[str]:
        return self._stderr_tail[-20:]

    def _allocate_id(self) -> int:
        with self._lock:
            value = self._next_id
            self._next_id += 1
            return value

    @staticmethod
    def _build_initialize_params() -> dict[str, Any]:
        return {
            "clientInfo": {
                "name": "codex_switch",
                "title": "codex switch",
                "version": "0.1.0",
            },
            "capabilities": {
                "experimentalApi": True,
            },
        }

    def _send(self, payload: dict[str, Any]) -> None:
        with self._lock:
            process = self._process
            if process is None or process.stdin is None:
                raise RuntimeError("Codex app-server is not running.")
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()

    @staticmethod
    def _normalize_request_id(value: Any) -> int | str | None:
        if isinstance(value, (int, str)):
            return value
        return None

    def _register_pending_request(self, request_id: int, response_queue: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._pending[request_id] = response_queue

    def _remove_pending_request(self, request_id: int | str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def _pending_request_queue(self, request_id: int | str) -> queue.Queue[dict[str, Any]] | None:
        with self._lock:
            return self._pending.get(request_id)

    def _timeout_error(self, method: str) -> TimeoutError:
        stderr_tail = "\n".join(self.stderr_tail).strip()
        self.close()
        if stderr_tail:
            return TimeoutError(f"Timed out waiting for {method} response.\nRecent app-server stderr:\n{stderr_tail}")
        return TimeoutError(f"Timed out waiting for {method} response.")

    def _reader_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for raw_line in self._process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle_reader_message(message)

    def _handle_reader_message(self, message: dict[str, Any]) -> None:
        response_id = self._normalize_request_id(message.get("id"))
        if response_id is not None:
            queue_for_id = self._pending_request_queue(response_id)
            if queue_for_id is not None:
                queue_for_id.put(message)
            return

        method = message.get("method")
        if method and self._notification_handler is not None:
            params = message.get("params", {})
            self._notification_handler(str(method), params if isinstance(params, dict) else {})

    def _stderr_loop(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        for raw_line in self._process.stderr:
            line = raw_line.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            if len(self._stderr_tail) > 50:
                self._stderr_tail[:] = self._stderr_tail[-50:]
