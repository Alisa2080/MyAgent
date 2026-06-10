from __future__ import annotations

import json
import os
import socket
import threading

from agent_tools.code_execution.dispatch import failure_payload

MAX_RPC_FRAME_BYTES = 1_000_000
RPC_SOCKET_TIMEOUT_SECONDS = 2.0
RPC_ACCEPT_TIMEOUT_SECONDS = 0.1


class CodeExecutionRpcServer:
    def __init__(self, *, socket_path: str, dispatcher) -> None:
        self.socket_path = socket_path
        self.dispatcher = dispatcher
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    def start(self) -> None:
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(16)
        self._sock.settimeout(RPC_ACCEPT_TIMEOUT_SECONDS)
        self._thread = threading.Thread(target=self._serve, name="code-execution-rpc", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        interrupted_thread_id = None
        if self._thread is not None and self._thread.ident is not None:
            interrupted_thread_id = self._thread.ident
            try:
                from agent_tools.terminal_toolkit.interrupt import set_interrupt

                set_interrupt(True, thread_id=interrupted_thread_id)
            except Exception:
                pass
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                wake.settimeout(RPC_ACCEPT_TIMEOUT_SECONDS)
                wake.connect(self.socket_path)
        except OSError:
            pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        if interrupted_thread_id is not None:
            try:
                from agent_tools.terminal_toolkit.interrupt import set_interrupt

                set_interrupt(False, thread_id=interrupted_thread_id)
            except Exception:
                pass
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(RPC_SOCKET_TIMEOUT_SECONDS)
                response = self._handle_connection(conn)
                data = json.dumps(response, ensure_ascii=False).encode("utf-8")
                try:
                    conn.sendall(len(data).to_bytes(8, "big") + data)
                except OSError:
                    pass

    def _handle_connection(self, conn: socket.socket) -> dict:
        try:
            header = conn.recv(8)
        except socket.timeout:
            return failure_payload("unknown", "RPC request timed out.", code="rpc_protocol_error")
        if len(header) != 8:
            return failure_payload("unknown", "Invalid RPC request header.", code="rpc_protocol_error")
        size = int.from_bytes(header, "big")
        if size > MAX_RPC_FRAME_BYTES:
            return failure_payload("unknown", "RPC request exceeds maximum frame size.", code="rpc_payload_too_large")
        chunks = []
        remaining = size
        while remaining > 0:
            try:
                chunk = conn.recv(min(65536, remaining))
            except socket.timeout:
                return failure_payload("unknown", "RPC request timed out.", code="rpc_protocol_error")
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        try:
            request = json.loads(b"".join(chunks).decode("utf-8"))
        except json.JSONDecodeError:
            return failure_payload("unknown", "Invalid RPC JSON request.", code="rpc_json_error")
        if not isinstance(request, dict):
            return failure_payload(
                "unknown",
                "RPC request must be a JSON object.",
                code="rpc_protocol_error",
            )
        tool_name = str(request.get("tool") or "")
        args = request.get("args") if isinstance(request.get("args"), dict) else {}
        return self.dispatcher.dispatch(tool_name, args)
