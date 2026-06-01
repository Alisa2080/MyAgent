"""Child-process entrypoint for cron job execution via subprocess boundary.

This module is invoked by the parent-side runner_subprocess client.
It loads the job payload from an input file, executes it via cron.runner,
and writes a structured result to an output file.

Exit codes:
    0 - Result file was written (success or failure, both valid).
    1 - Worker-level failure (could not read input or write output).

The worker does NOT use stdout as a structured protocol; stdout and stderr
are diagnostic text only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_PROTOCOL_VERSION = 1


def _write_result(output_path: Path, result: dict[str, Any]) -> None:
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    from cron.paths import secure_file

    secure_file(output_path)


def _failure_result(error: str) -> dict[str, Any]:
    return {
        "version": _PROTOCOL_VERSION,
        "success": False,
        "output_doc": None,
        "final_response": None,
        "error": error,
        "exit_reason": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cron.runner_worker",
        description="Child-process cron job runner.",
    )
    parser.add_argument("--input", required=True, help="Path to input JSON file")
    parser.add_argument("--output", required=True, help="Path to output JSON file")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    # ------------------------------------------------------------------
    # Load input
    # ------------------------------------------------------------------
    if not input_path.exists():
        err = f"Input file not found: {input_path}"
        _write_result(output_path, _failure_result(err))
        return 0  # parent still gets a result to parse

    try:
        raw = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        err = f"Failed to read input file: {exc}"
        _write_result(output_path, _failure_result(err))
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        err = f"Invalid input JSON: {exc}"
        _write_result(output_path, _failure_result(err))
        return 0

    job = payload.get("job")
    if not isinstance(job, dict):
        err = f"Input payload missing 'job' dict (got {type(job).__name__})"
        _write_result(output_path, _failure_result(err))
        return 0

    # ------------------------------------------------------------------
    # Execute job via the real runner
    # ------------------------------------------------------------------
    try:
        from cron.runner import run_job as _run_job

        result = _run_job(job)
    except Exception as exc:
        err = f"Worker exception during job execution: {exc}"
        _write_result(output_path, _failure_result(err))
        return 0

    # ------------------------------------------------------------------
    # Serialize result
    # ------------------------------------------------------------------
    try:
        result_payload = {
            "version": _PROTOCOL_VERSION,
            "success": result.success,
            "output_doc": result.output_doc,
            "final_response": result.final_response,
            "error": result.error,
            "exit_reason": getattr(result, "exit_reason", None),
        }
        _write_result(output_path, result_payload)
        return 0
    except Exception as exc:
        # Last resort — we failed to write the output, parent will see missing file
        sys.stderr.write(f"runner_worker: failed to write result file: {exc}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
