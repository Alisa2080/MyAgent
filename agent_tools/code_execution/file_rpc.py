from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from agent_tools.code_execution.dispatch import failure_payload
from agent_tools.code_execution.safety import redact_code_execution_text

DEFAULT_FILE_RPC_MAX_REQUEST_BYTES = 1_000_000
DEFAULT_FILE_RPC_MAX_RESPONSE_BYTES = 1_000_000


class FileRpcBridge:
    def __init__(
        self,
        *,
        rpc_dir: str | Path,
        dispatcher,
        max_request_bytes: int = DEFAULT_FILE_RPC_MAX_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_FILE_RPC_MAX_RESPONSE_BYTES,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self.rpc_dir = Path(rpc_dir)
        self.dispatcher = dispatcher
        self.max_request_bytes = int(max_request_bytes)
        self.max_response_bytes = int(max_response_bytes)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dispatch_threads: set[threading.Thread] = set()
        self._dispatch_lock = threading.Lock()

    def start(self) -> None:
        self.rpc_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="code-execution-file-rpc", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._dispatch_lock:
            dispatch_threads = list(self._dispatch_threads)
        interrupted_thread_ids: list[int] = []
        for thread in dispatch_threads:
            thread_id = thread.ident
            if thread_id is not None:
                try:
                    from agent_tools.terminal_toolkit.interrupt import set_interrupt

                    set_interrupt(True, thread_id=thread_id)
                    interrupted_thread_ids.append(thread_id)
                except Exception:
                    pass
        deadline = time.monotonic() + 2.0
        for thread in dispatch_threads:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        if self._thread is not None:
            remaining = max(0.0, deadline - time.monotonic())
            self._thread.join(timeout=remaining)
        if interrupted_thread_ids:
            try:
                from agent_tools.terminal_toolkit.interrupt import set_interrupt

                for thread_id in interrupted_thread_ids:
                    set_interrupt(False, thread_id=thread_id)
            except Exception:
                pass

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def has_active_dispatches(self) -> bool:
        with self._dispatch_lock:
            return any(thread.is_alive() for thread in self._dispatch_threads)

    def _run(self) -> None:
        while not self._stop.is_set():
            for req_path in sorted(self.rpc_dir.glob("req_*.json")):
                try:
                    self._handle_request_path(req_path)
                except Exception:
                    request_id = req_path.stem.removeprefix("req_")
                    try:
                        self._write_response(
                            request_id,
                            failure_payload(
                                "unknown",
                                "RPC request handling failed.",
                                code="rpc_internal_error",
                            ),
                        )
                    except OSError:
                        pass
            self._stop.wait(self.poll_interval_seconds)

    def _handle_request_path(self, req_path: Path) -> None:
        request_id = req_path.stem.removeprefix("req_")
        claim_path = req_path.with_name(f".claimed_{request_id}.json")
        try:
            req_path.replace(claim_path)
        except FileNotFoundError:
            return
        except OSError:
            return

        response = self._response_for_claimed_request(claim_path)
        self._write_response(request_id, response)
        try:
            claim_path.unlink()
        except OSError:
            pass

    def _response_for_claimed_request(self, claim_path: Path) -> dict:
        try:
            if claim_path.stat().st_size > self.max_request_bytes:
                return failure_payload("unknown", "RPC request exceeds maximum file size.", code="rpc_payload_too_large")
            request = json.loads(claim_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return failure_payload("unknown", "Invalid RPC JSON request.", code="rpc_json_error")
        except OSError as exc:
            return failure_payload("unknown", f"Failed to read RPC request: {exc}", code="rpc_io_error")

        if not isinstance(request, dict):
            return failure_payload(
                "unknown",
                "RPC request must be a JSON object.",
                code="rpc_protocol_error",
            )
        tool_name = str(request.get("tool") or "")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        return self._dispatch(tool_name, args)

    def _dispatch(self, tool_name: str, args: dict) -> dict:
        done = threading.Event()
        holder: dict[str, dict] = {}

        def run_dispatch() -> None:
            try:
                holder["response"] = self.dispatcher.dispatch(tool_name, args)
            except Exception as exc:
                holder["response"] = failure_payload(
                    tool_name,
                    redact_code_execution_text(
                        f"RPC tool dispatch failed: {type(exc).__name__}: {exc}"
                    ),
                    code="dispatch_error",
                )
            finally:
                thread_id = threading.current_thread().ident
                if thread_id is not None:
                    try:
                        from agent_tools.terminal_toolkit.interrupt import set_interrupt

                        set_interrupt(False, thread_id=thread_id)
                    except Exception:
                        pass
                with self._dispatch_lock:
                    self._dispatch_threads.discard(thread)
                done.set()

        thread = threading.Thread(
            target=run_dispatch,
            name="code-execution-file-rpc-dispatch",
            daemon=True,
        )
        with self._dispatch_lock:
            self._dispatch_threads.add(thread)
        thread.start()

        while not done.wait(0.05):
            if self._stop.is_set():
                return failure_payload(
                    tool_name,
                    "RPC dispatch was cancelled during cleanup.",
                    code="rpc_cancelled",
                )

        return holder["response"]

    def _write_response(self, request_id: str, response: dict) -> None:
        res_path = self.rpc_dir / f"res_{request_id}.json"
        tmp_path = self.rpc_dir / f".res_{request_id}.tmp"
        data = json.dumps(response, ensure_ascii=False).encode("utf-8")
        if len(data) > self.max_response_bytes:
            data = json.dumps(
                failure_payload(
                    str(response.get("tool") or "unknown"),
                    "RPC response exceeds maximum file size.",
                    code="rpc_response_too_large",
                ),
                ensure_ascii=False,
            ).encode("utf-8")
        tmp_path.write_bytes(data)
        tmp_path.replace(res_path)
