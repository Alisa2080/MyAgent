from __future__ import annotations

import shlex

from agent_cli import cron_commands


def cron_handlers():
    return {"cron": handle_cron}


def _usage() -> str:
    return "\n".join(
        [
            "Usage:",
            "  /cron list [--all]",
            '  /cron add <schedule> [prompt] [--name NAME] [--deliver origin|local]',
            "  /cron edit <job_id> [--schedule S] [--prompt P]",
            "  /cron pause|resume|run|remove <job_id>",
            "  /cron status",
            "  /cron tick  # manual diagnostic; automatic scheduling uses agent cron serve",
            "  /cron doctor",
            "  /cron test-delivery --target local|origin [--session-id SESSION]",
        ]
    )


def _parse_flags(tokens: list[str]) -> tuple[dict, list[str], str | None]:
    flags = {
        "name": None,
        "deliver": None,
        "repeat": None,
        "skills": [],
        "add_skills": [],
        "remove_skills": [],
        "clear_skills": False,
        "include_disabled": False,
        "prompt": None,
        "schedule": None,
        "script": None,
        "workdir": None,
        "target": None,
        "session_id": None,
    }
    positionals: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--name", "--deliver", "--prompt", "--schedule", "--script", "--workdir", "--target", "--session-id"}:
            if i + 1 >= len(tokens):
                return flags, positionals, f"{token} requires a value"
            flags[token[2:].replace("-", "_")] = tokens[i + 1]
            i += 2
        elif token == "--repeat":
            if i + 1 >= len(tokens):
                return flags, positionals, "--repeat requires a value"
            try:
                flags["repeat"] = int(tokens[i + 1])
            except ValueError:
                return flags, positionals, "--repeat must be an integer"
            i += 2
        elif token == "--skill":
            if i + 1 >= len(tokens):
                return flags, positionals, "--skill requires a value"
            flags["skills"].append(tokens[i + 1])
            i += 2
        elif token == "--add-skill":
            if i + 1 >= len(tokens):
                return flags, positionals, "--add-skill requires a value"
            flags["add_skills"].append(tokens[i + 1])
            i += 2
        elif token == "--remove-skill":
            if i + 1 >= len(tokens):
                return flags, positionals, "--remove-skill requires a value"
            flags["remove_skills"].append(tokens[i + 1])
            i += 2
        elif token == "--clear-skills":
            flags["clear_skills"] = True
            i += 1
        elif token == "--all":
            flags["include_disabled"] = True
            i += 1
        else:
            positionals.append(token)
            i += 1
    return flags, positionals, None


def _compact_kwargs(flags: dict) -> dict:
    mapping = {
        "name": flags["name"],
        "deliver": flags["deliver"],
        "repeat": flags["repeat"],
        "skills": flags["skills"] or None,
        "script": flags["script"],
        "workdir": flags["workdir"],
    }
    return {key: value for key, value in mapping.items() if value is not None}


def handle_cron(ctx, arg, command):
    try:
        tokens = shlex.split(arg)
    except ValueError as exc:
        return f"Invalid /cron arguments: {exc}"
    if not tokens:
        return _usage() + "\n\n" + cron_commands.list_cron_jobs().text
    subcommand = tokens[0].lower()
    flags, positionals, error = _parse_flags(tokens[1:])
    if error:
        return f"{error}\n{_usage()}"

    if subcommand == "list":
        return cron_commands.list_cron_jobs(
            include_disabled=flags["include_disabled"]
        ).text
    if subcommand in {"add", "create"}:
        if not positionals and not flags["schedule"]:
            return _usage()
        schedule = flags["schedule"] or positionals[0]
        prompt = flags["prompt"] or (" ".join(positionals[1:]) if len(positionals) > 1 else None)
        session_id = ctx.ensure_session()
        return cron_commands.create_cron_job(
            schedule=schedule,
            prompt=prompt,
            session_id=session_id,
            **_compact_kwargs(flags),
        ).text
    if subcommand == "edit":
        if not positionals:
            return _usage()
        job_id = positionals[0]
        updates = _compact_kwargs(flags)
        if flags["schedule"] is not None:
            updates["schedule"] = flags["schedule"]
        if flags["prompt"] is not None:
            updates["prompt"] = flags["prompt"]
        if flags["clear_skills"]:
            updates["skills"] = []
        elif flags["skills"]:
            updates["skills"] = flags["skills"]
        elif flags["add_skills"] or flags["remove_skills"]:
            skills = cron_commands.existing_job_skills(job_id)
            remove = set(flags["remove_skills"])
            final_skills = [skill for skill in skills if skill not in remove]
            for skill in flags["add_skills"]:
                if skill not in final_skills:
                    final_skills.append(skill)
            updates["skills"] = final_skills
        return cron_commands.update_cron_job(
            job_id=job_id,
            session_id=ctx.session_id,
            **updates,
        ).text
    if subcommand in {"pause", "resume", "run", "remove", "rm", "delete"}:
        if not positionals:
            return _usage()
        action = "remove" if subcommand in {"remove", "rm", "delete"} else subcommand
        return cron_commands.simple_job_action(
            action,
            job_id=positionals[0],
            reason="paused from /cron" if action == "pause" else None,
        ).text
    if subcommand == "status":
        return cron_commands.cron_status().text
    if subcommand == "tick":
        return cron_commands.run_tick().text
    if subcommand == "doctor":
        return cron_commands.cron_doctor().text
    if subcommand == "test-delivery":
        target = flags["target"] or "local"
        session_id = flags["session_id"]
        if target == "origin" and not session_id:
            session_id = ctx.session_id
        return cron_commands.test_delivery(
            target=target,
            session_id=session_id,
        ).text
    return f"Unknown /cron command: {subcommand}\n{_usage()}"
