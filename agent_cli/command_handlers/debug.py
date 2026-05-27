from __future__ import annotations

from agent_cli.commands import render_help
from agent_cli.doctor import render_doctor_output, run_health_checks


def debug_handlers():
    return {"help": handle_help, "doctor": handle_doctor, "exit": handle_exit}


def handle_help(cli, arg, command):
    return render_help()


def handle_doctor(cli, arg, command):
    results = run_health_checks(workdir=cli.workdir)
    return render_doctor_output(results)


def handle_exit(cli, arg, command):
    raise EOFError
