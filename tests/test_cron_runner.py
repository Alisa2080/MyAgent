from __future__ import annotations

import concurrent.futures
import time
from types import SimpleNamespace


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


def test_run_script_uses_script_parent_as_cwd(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    nested = tmp_path / "scripts" / "nested"
    nested.mkdir(parents=True)
    script = nested / "cwd.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('relative-output.txt').write_text(Path.cwd().name)\n"
        "print(Path.cwd())\n"
    )

    ok, output = runner._run_script("nested/cwd.py")

    assert ok is True
    assert str(nested) in output
    assert (nested / "relative-output.txt").read_text() == "nested"


def test_script_timeout_env_default_and_override(monkeypatch):
    import cron.runner as runner

    monkeypatch.delenv("HERMES_CRON_SCRIPT_TIMEOUT", raising=False)
    assert runner._script_timeout() == 120

    monkeypatch.setenv("HERMES_CRON_SCRIPT_TIMEOUT", "3")
    assert runner._script_timeout() == 3

    monkeypatch.setenv("HERMES_CRON_SCRIPT_TIMEOUT", "not-an-int")
    assert runner._script_timeout() == 120


def test_script_timeout_failure_path_returns_failure(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CRON_SCRIPT_TIMEOUT", "1")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "slow.py"
    script.write_text("import time\nprint('before sleep', flush=True)\ntime.sleep(5)\n")

    start = time.monotonic()
    result = runner.run_job(
        {"id": "job-1", "name": "slow-script", "prompt": "x", "script": "slow.py"}
    )

    assert time.monotonic() - start < 4
    assert result.success is False
    assert result.error == "Pre-run script timed out after 1 seconds."
    assert "before sleep" in result.output_doc


def test_run_script_captures_stdout_stderr_with_bounded_output(monkeypatch, tmp_path):
    import cron.runner as runner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(runner, "SCRIPT_OUTPUT_MAX_CHARS", 80)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "noisy.py"
    script.write_text(
        "import sys\n"
        "print('e' * 70 + 'stderr tail', file=sys.stderr)\n"
        "print('stdout tail')\n"
    )

    ok, output = runner._run_script("noisy.py")

    assert ok is True
    assert len(output) == 80
    assert "stderr tail" in output
    assert "stdout tail" in output
    assert "e" * 70 not in output


def test_build_cron_agent_uses_model_workdir_middleware_and_toolsets(
    monkeypatch, tmp_path
):
    import cron.runner as runner

    calls = {}
    workdir = tmp_path / "work"
    workdir.mkdir()

    def fake_build_prompt_context(**kwargs):
        calls["prompt_context"] = kwargs
        return SimpleNamespace(context="prompt-context")

    def fake_load_project_instruction_blocks(path):
        calls["project_instruction_path"] = path
        return ("project instructions",)

    def fake_middleware(**kwargs):
        calls["middleware_kwargs"] = kwargs
        return ["middleware"]

    def fake_build_cron_tools(enabled_toolsets):
        calls["enabled_toolsets"] = enabled_toolsets
        return ["tools"]

    class FakePromptBuilder:
        def build_parent(self, context):
            calls["system_prompt_context"] = context
            return "system prompt"

    def fake_create_agent(**kwargs):
        calls["create_agent"] = kwargs
        return "agent"

    monkeypatch.setattr(runner, "build_prompt_context", fake_build_prompt_context)
    monkeypatch.setattr(
        runner, "load_project_instruction_blocks", fake_load_project_instruction_blocks
    )
    monkeypatch.setattr(runner, "build_tool_call_limit_middleware", fake_middleware)
    monkeypatch.setattr(runner, "build_cron_tools", fake_build_cron_tools)
    monkeypatch.setattr(runner, "SystemPromptBuilder", FakePromptBuilder)
    monkeypatch.setattr(runner, "create_agent", fake_create_agent)

    agent = runner._build_cron_agent(
        {
            "id": "job-1",
            "prompt": "x",
            "workdir": str(workdir),
            "enabled_toolsets": ["terminal", "delegation"],
        }
    )

    assert agent == "agent"
    assert calls["prompt_context"]["workdir"] == workdir
    assert calls["prompt_context"]["model_name"] == runner.model_display_name(
        runner.MAIN_MODEL
    )
    assert calls["prompt_context"]["project_instruction_blocks"] == (
        "project instructions",
    )
    assert calls["project_instruction_path"] == workdir
    assert calls["middleware_kwargs"] == {"include_task": True}
    assert calls["enabled_toolsets"] == ["terminal", "delegation"]
    assert calls["create_agent"] == {
        "model": runner.MAIN_MODEL,
        "system_prompt": "system prompt",
        "middleware": ["middleware"],
        "tools": ["tools"],
    }


def test_build_cron_agent_uses_default_workdir(monkeypatch, tmp_path):
    import cron.runner as runner

    calls = {}
    default_workdir = tmp_path / "default"
    default_workdir.mkdir()

    monkeypatch.setattr(runner, "WORKDIR", default_workdir)
    monkeypatch.setattr(
        runner,
        "build_prompt_context",
        lambda **kwargs: calls.setdefault("prompt_context", kwargs) or "ctx",
    )
    monkeypatch.setattr(
        runner,
        "load_project_instruction_blocks",
        lambda path: calls.setdefault("project_instruction_path", path) or (),
    )
    monkeypatch.setattr(
        runner, "build_tool_call_limit_middleware", lambda **kwargs: []
    )
    monkeypatch.setattr(runner, "build_cron_tools", lambda enabled_toolsets: [])
    monkeypatch.setattr(
        runner,
        "SystemPromptBuilder",
        lambda: SimpleNamespace(build_parent=lambda context: "system prompt"),
    )
    monkeypatch.setattr(runner, "create_agent", lambda **kwargs: "agent")

    runner._build_cron_agent({"id": "job-1", "prompt": "x"})

    assert calls["prompt_context"]["workdir"] == default_workdir
    assert calls["project_instruction_path"] == default_workdir


def test_invoke_cron_agent_passes_recursion_limit_and_default_timeout(monkeypatch):
    import cron.runner as runner

    calls = {}

    class FakeAgent:
        def invoke(self, payload, config):
            calls["payload"] = payload
            calls["config"] = config
            return {
                "messages": [
                    SimpleNamespace(content=[{"type": "text", "text": "done"}])
                ]
            }

    class FakeFuture:
        def __init__(self, fn, args):
            self.fn = fn
            self.args = args

        def result(self, timeout=None):
            calls["timeout"] = timeout
            return self.fn(*self.args)

    class FakeExecutor:
        def __init__(self, max_workers):
            calls["max_workers"] = max_workers

        def submit(self, fn, *args):
            return FakeFuture(fn, args)

        def shutdown(self, **kwargs):
            calls["shutdown"] = kwargs

    monkeypatch.delenv("HERMES_CRON_TIMEOUT", raising=False)
    monkeypatch.setattr(runner, "_build_cron_agent", lambda job: FakeAgent())
    monkeypatch.setattr(runner.concurrent.futures, "ThreadPoolExecutor", FakeExecutor)

    response = runner._invoke_cron_agent({"id": "job-1"}, "prompt text")

    assert response == "done"
    assert calls["payload"] == {
        "messages": [{"role": "user", "content": "prompt text"}]
    }
    assert calls["config"] == {"recursion_limit": runner.AGENT_RECURSION_LIMIT}
    assert calls["timeout"] == 600
    assert calls["shutdown"] == {"wait": False, "cancel_futures": True}


def test_invoke_cron_agent_timeout_zero_waits_without_timeout(monkeypatch):
    import cron.runner as runner

    calls = {}

    class FakeFuture:
        def result(self, timeout=None):
            calls["timeout"] = timeout
            return {
                "messages": [
                    SimpleNamespace(content=[{"type": "text", "text": "done"}])
                ]
            }

    class FakeExecutor:
        def __init__(self, max_workers):
            pass

        def submit(self, fn, *args):
            return FakeFuture()

        def shutdown(self, **kwargs):
            pass

    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    monkeypatch.setattr(
        runner, "_build_cron_agent", lambda job: SimpleNamespace(invoke=lambda: None)
    )
    monkeypatch.setattr(runner.concurrent.futures, "ThreadPoolExecutor", FakeExecutor)

    assert runner._invoke_cron_agent({"id": "job-1"}, "prompt text") == "done"
    assert calls["timeout"] is None


def test_invoke_cron_agent_timeout_error_propagates(monkeypatch):
    import cron.runner as runner

    class FakeFuture:
        def result(self, timeout=None):
            raise concurrent.futures.TimeoutError()

    class FakeExecutor:
        def __init__(self, max_workers):
            pass

        def submit(self, fn, *args):
            return FakeFuture()

        def shutdown(self, **kwargs):
            pass

    monkeypatch.setattr(
        runner, "_build_cron_agent", lambda job: SimpleNamespace(invoke=lambda: None)
    )
    monkeypatch.setattr(runner.concurrent.futures, "ThreadPoolExecutor", FakeExecutor)

    try:
        runner._invoke_cron_agent({"id": "job-1"}, "prompt text")
    except concurrent.futures.TimeoutError:
        pass
    else:
        raise AssertionError("expected TimeoutError")


def test_run_job_generic_agent_error_returns_failure(monkeypatch):
    import cron.runner as runner

    def fail_agent(job, prompt):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(runner, "_invoke_cron_agent", fail_agent)

    result = runner.run_job({"id": "job-1", "name": "agent", "prompt": "do it"})

    assert result.success is False
    assert result.error == "agent exploded"
    assert "agent exploded" in result.output_doc


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
