"""SSH remote execution environment with ControlMaster connection persistence."""

import hashlib
import logging
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import BaseEnvironment, _popen_bash

logger = logging.getLogger(__name__)


def _ensure_ssh_available() -> None:
    if not shutil.which("ssh"):
        raise RuntimeError(
            "SSH is not installed or not in PATH. Install OpenSSH client: apt install openssh-client"
        )


class SSHEnvironment(BaseEnvironment):
    """Run commands on a remote machine over SSH."""

    def __init__(self, host: str, user: str, cwd: str = "~", timeout: int = 60, port: int = 22, key_path: str = ""):
        super().__init__(cwd=cwd, timeout=timeout)
        self.host = host
        self.user = user
        self.port = port
        self.key_path = key_path
        self.control_dir = Path(tempfile.gettempdir()) / "hermes-terminal-toolkit-ssh"
        self.control_dir.mkdir(parents=True, exist_ok=True)
        socket_id = hashlib.sha256(f"{user}@{host}:{port}".encode()).hexdigest()[:16]
        self.control_socket = self.control_dir / f"{socket_id}.sock"
        _ensure_ssh_available()
        self._establish_connection()
        self._remote_home = self._detect_remote_home()
        self.init_session()

    def _build_ssh_command(self, extra_args: list | None = None) -> list:
        cmd = ["ssh"]
        cmd.extend(["-o", f"ControlPath={self.control_socket}"])
        cmd.extend(["-o", "ControlMaster=auto"])
        cmd.extend(["-o", "ControlPersist=300"])
        cmd.extend(["-o", "BatchMode=yes"])
        cmd.extend(["-o", "StrictHostKeyChecking=accept-new"])
        cmd.extend(["-o", "ConnectTimeout=10"])
        if self.port != 22:
            cmd.extend(["-p", str(self.port)])
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if extra_args:
            cmd.extend(extra_args)
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _establish_connection(self):
        cmd = self._build_ssh_command()
        cmd.append("echo 'SSH connection established'")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                error_msg = result.stderr.strip() or result.stdout.strip()
                raise RuntimeError(f"SSH connection failed: {error_msg}")
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"SSH connection to {self.user}@{self.host} timed out") from exc

    def _detect_remote_home(self) -> str:
        try:
            cmd = self._build_ssh_command()
            cmd.append("echo $HOME")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            home = result.stdout.strip()
            if home and result.returncode == 0:
                logger.debug("SSH: remote home = %s", home)
                return home
        except Exception:
            pass
        if self.user == "root":
            return "/root"
        return f"/home/{self.user}"

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120, stdin_data: str | None = None) -> subprocess.Popen:
        cmd = self._build_ssh_command()
        if login:
            cmd.extend(["bash", "-l", "-c", shlex.quote(cmd_string)])
        else:
            cmd.extend(["bash", "-c", shlex.quote(cmd_string)])
        return _popen_bash(cmd, stdin_data)

    def cleanup(self):
        if self.control_socket.exists():
            try:
                cmd = ["ssh", "-o", f"ControlPath={self.control_socket}", "-O", "exit", f"{self.user}@{self.host}"]
                subprocess.run(cmd, capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                self.control_socket.unlink()
            except OSError:
                pass
