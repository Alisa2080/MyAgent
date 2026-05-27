from __future__ import annotations

from agent_cli.commands import render_help
from agent_cli.doctor import render_doctor_output, run_health_checks


def debug_handlers():
    return {"help": handle_help, "doctor": handle_doctor, "reload": handle_reload, "exit": handle_exit}


def handle_reload(ctx, arg, command):
    if arg:
        return "Usage: /reload"
    return ctx.reload_runtime_settings()


def handle_help(ctx, arg, command):
    return render_help()


def handle_doctor(ctx, arg, command):
    results = run_health_checks(workdir=ctx.workdir)
    return render_doctor_output(results)


def handle_exit(ctx, arg, command):
    raise EOFError
