from __future__ import annotations

from agent_cli.approval import collect_approval_decisions


def background_handlers():
    return {
        "background": handle_background,
        "tasks": handle_tasks,
        "tail": handle_tail,
        "queue": handle_queue,
        "steer": handle_steer,
        "stop": handle_stop,
        "approve": handle_approve,
    }


def handle_background(cli, arg, command):
    if not arg.strip():
        return "Usage: /background <prompt>"
    record = cli._require_background_registry().start(arg)
    return f"Started background task {record.task_id} · session {record.session_id}"


def handle_tasks(cli, arg, command):
    arg = arg.strip()
    if arg == "all":
        return render_task_list(cli._require_background_registry().list_tasks(active_only=False, limit=100))
    if arg:
        return render_task_detail(cli, arg)
    return render_task_list(cli._require_background_registry().list_tasks(active_only=False, limit=20))


def render_task_list(records):
    if not records:
        return "No background tasks."
    lines = ["Background Tasks:"]
    for record in records:
        detail = (
            record.last_error
            or record.last_result_preview
            or getattr(record, "prompt_preview", None)
            or ""
        )
        lines.append(
            f"  {record.task_id}  {record.status:<16} {record.title} "
            f"session={record.session_id} steer={record.pending_steer_count} {detail}".rstrip()
        )
    return "\n".join(lines)


def render_task_detail(cli, task_id):
    registry = cli._require_background_registry()
    record = registry.get_task(task_id)
    if record is None:
        return f"Unknown background task: {task_id}"
    lines = [
        f"Task: {record.task_id}",
        f"  Status: {record.status}",
        f"  Title: {record.title}",
        f"  Session: {record.session_id}",
    ]
    if record.last_result_preview:
        lines.append(f"  Result: {record.last_result_preview}")
    if record.last_error:
        lines.append(f"  Error: {record.last_error}")
    lines.append(f"  Resume: /resume {record.session_id}")
    return "\n".join(lines)


def handle_tail(cli, arg, command):
    task_id = arg.strip()
    if not task_id:
        return "Usage: /tail <task_id>"
    registry = cli._require_background_registry()
    record = registry.get_task(task_id)
    if record is None:
        return f"Unknown background task: {task_id}"
    steers = registry.get_steers(task_id, limit=20)
    lines = [f"Tail for {record.task_id}:"]
    lines.append(f"  Result: {record.last_result_preview or '-'}")
    lines.append(f"  Error: {record.last_error or '-'}")
    lines.append("  Steers:")
    if not steers:
        lines.append("    -")
    for steer in steers:
        lines.append(f"    {steer.id} {steer.status} {steer.message}")
    return "\n".join(lines)


def handle_queue(cli, arg, command):
    return render_task_list(cli._require_background_registry().list_tasks(active_only=True))


def handle_steer(cli, arg, command):
    parts = arg.split(maxsplit=1)
    if len(parts) != 2:
        return "Usage: /steer <task_id> <message>"
    steer = cli._require_background_registry().steer(parts[0], parts[1])
    return f"Queued steer {steer.id} for {steer.task_id}"


def handle_stop(cli, arg, command):
    task_id = arg.strip()
    if not task_id:
        return "Usage: /stop <task_id>"
    record = cli._require_background_registry().stop(task_id)
    if record.status in {"completing", "completed", "failed", "stopped"}:
        return f"Background task {record.task_id} is already {record.status}"
    return f"Stop requested for {record.task_id}"


def handle_approve(cli, arg, command):
    task_id = arg.strip()
    if not task_id:
        return "Usage: /approve <task_id>"
    registry = cli._require_background_registry()
    requests = registry.approval_requests(task_id)
    resume_value = collect_approval_decisions(requests)
    registry.approve(task_id, resume_value)
    return f"Approved background task {task_id}"
