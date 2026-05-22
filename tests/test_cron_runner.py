from __future__ import annotations

import concurrent.futures


def test_validate_script_path_rejects_unsafe_paths(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert runner.validate_script_path("/tmp/script.py").startswith(
        "Script path must be relative"
    )
    assert runner.validate_script_path("~/script.py").startswith(
        "Script path must be relative"
    )
    assert runner.validate_script_path("C:\\Temp\\script.py").startswith(
        "Script path must be relative"
    )
    assert runner.validate_script_path("C:Temp\\script.py").startswith(
        "Script path must be relative"
    )
    assert runner.validate_script_path("../escape.py").startswith(
        "Script path escapes"
    )
    assert runner.validate_script_path("nested/../../escape.py").startswith(
        "Script path escapes"
    )


def test_wake_gate_false_skips_agent(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "check.py"
    script.write_text('print("nothing")\nprint(\'{"wakeAgent": false}\')\n')

    def fail_agent(job, prompt):
        raise AssertionError("agent should not run")

    monkeypatch.setattr(runner, "_invoke_cron_agent", fail_agent)

    result = runner.run_job(
        {"id": "job-1", "name": "gate", "prompt": "x", "script": "check.py"}
    )

    assert result.success is True
    assert result.final_response == "[SILENT]"
    assert "agent skipped" in result.output_doc


def test_wake_gate_false_uses_stdout_even_when_stderr_is_present(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "check.py"
    script.write_text(
        "import sys\n"
        "print('warning', file=sys.stderr)\n"
        "print('{\"wakeAgent\": false}')\n"
    )

    def fail_agent(job, prompt):
        raise AssertionError("agent should not run")

    monkeypatch.setattr(runner, "_invoke_cron_agent", fail_agent)

    result = runner.run_job(
        {"id": "job-1", "name": "gate", "prompt": "x", "script": "check.py"}
    )

    assert result.success is True
    assert result.final_response == "[SILENT]"


def test_build_cron_tools_excludes_cronjob_and_includes_requested_toolsets():
    import cron.runner as runner

    names = [
        getattr(tool, "name", "")
        for tool in runner.build_cron_tools(["terminal", "file_write", "delegation"])
    ]

    assert "cronjob" not in names
    assert "list_directory" in names
    assert "terminal" in names
    assert "process" in names
    assert "write_file" in names
    assert "patch" in names
    assert "task" in names


def test_build_cron_tools_defaults_to_read_only_tools():
    import cron.runner as runner

    names = [getattr(tool, "name", "") for tool in runner.build_cron_tools()]

    assert "list_directory" in names
    assert "read_file" in names
    assert "web_search" in names
    assert "write_file" not in names
    assert "terminal" not in names
    assert "task" not in names
    assert "cronjob" not in names


def test_build_job_prompt_includes_context_skills_script_and_job_prompt(monkeypatch):
    import cron.runner as runner

    monkeypatch.setattr(
        runner, "latest_job_output", lambda job_id: f"output for {job_id}"
    )
    monkeypatch.setattr(
        runner, "_load_skill_content", lambda skill: f"skill content {skill}"
    )

    prompt = runner.build_job_prompt(
        {
            "id": "job-1",
            "name": "daily",
            "prompt": "write report",
            "context_from": ["upstream"],
            "skills": ["reporting"],
            "skill": "legacy",
        },
        script_output="script output",
    )

    assert "unattended cron job" in prompt
    assert "script output" in prompt
    assert "output for upstream" in prompt
    assert "skill content reporting" in prompt
    assert "skill content legacy" in prompt
    assert "write report" in prompt


def test_script_failure_returns_failure_result(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "fail.py"
    script.write_text('import sys\nprint("bad script")\nsys.exit(7)\n')

    result = runner.run_job(
        {"id": "job-1", "name": "fail", "prompt": "x", "script": "fail.py"}
    )

    assert result.success is False
    assert result.error == "Pre-run script failed."
    assert "bad script" in result.output_doc


def test_run_job_extracts_final_response_from_agent(monkeypatch):
    import cron.runner as runner

    monkeypatch.setattr(
        runner,
        "_build_cron_agent",
        lambda job: type(
            "FakeAgent",
            (),
            {
                "invoke": lambda self, payload, config: {
                    "messages": [
                        type(
                            "Message",
                            (),
                            {"content": [{"type": "text", "text": "final text"}]},
                        )()
                    ]
                }
            },
        )(),
    )

    result = runner.run_job({"id": "job-1", "name": "agent", "prompt": "do it"})

    assert result.success is True
    assert result.final_response == "final text"
    assert "final text" in result.output_doc


def test_cron_timeout_returns_failure_result(monkeypatch):
    import cron.runner as runner

    def fake_invoke(job, prompt):
        raise concurrent.futures.TimeoutError()

    monkeypatch.setattr(runner, "_invoke_cron_agent", fake_invoke)

    result = runner.run_job({"id": "job-1", "name": "slow", "prompt": "do it"})

    assert result.success is False
    assert result.error == "Cron job timed out."
    assert "Cron job timed out." in result.output_doc
