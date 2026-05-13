import os
import subprocess
from pathlib import Path


class LocalTerminalEnvironment:
    """Minimal local shell backend expected by ShellFileOperations."""

    def __init__(self, cwd: str | None = None, timeout: int = 120):
        self.cwd = str(Path(cwd or os.environ.get("TERMINAL_CWD") or os.getcwd()).resolve())
        self.timeout = timeout

    def execute(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        stdin_data: str | None = None,
    ) -> dict:
        run_cwd = cwd or self.cwd
        proc = subprocess.run(
            command,
            input=stdin_data,
            text=True,
            shell=True,
            cwd=run_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout or self.timeout,
        )
        return {"output": proc.stdout, "returncode": proc.returncode}
