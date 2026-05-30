"""Import-health regression tests for cron runner dependency injection.

These tests verify that cron.scheduler remains lightweight and does not
transitively import heavy agent dependencies.
"""
from __future__ import annotations

from datetime import datetime, timezone
import sys


def test_scheduler_imports_no_heavy_modules():
    """Verify that importing cron.scheduler does not import cron.runner or langchain.agents."""
    # Remove any cached modules
    for mod in list(sys.modules.keys()):
        if mod.startswith("cron.") or mod.startswith("langchain."):
            del sys.modules[mod]

    # Import scheduler
    import cron.scheduler  # noqa: F401

    # Verify cron.runner is not imported
    assert "cron.runner" not in sys.modules, (
        "cron.scheduler should not import cron.runner at module level"
    )

    # Verify langchain.agents is not imported
    assert "langchain.agents" not in sys.modules, (
        "cron.scheduler should not import langchain.agents"
    )


def test_scheduler_tick_no_due_jobs_does_not_import_runner(monkeypatch, tmp_path):
    """Verify that tick() with no due jobs does not import the heavy runner."""
    import cron.scheduler as scheduler

    # Clear any cached runner
    for mod in list(sys.modules.keys()):
        if mod.startswith("cron.runner") or mod.startswith("langchain."):
            del sys.modules[mod]

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda now_dt=None: [])

    result = scheduler.tick()

    assert result.due == 0
    assert result.ran == 0
    # The default runner should NOT be imported when there are no due jobs
    assert "cron.runner" not in sys.modules, (
        "tick() with no due jobs should not import cron.runner"
    )


def test_scheduler_tick_with_fake_runner_does_not_import_heavy_modules(monkeypatch, tmp_path):
    """Verify that tick() with an injected fake runner does not import heavy modules."""
    import cron.scheduler as scheduler
    from cron.contracts import JobRunResult
    from cron.jobs import create_job, update_job

    # Clear any cached modules
    for mod in list(sys.modules.keys()):
        if mod.startswith("cron.runner") or mod.startswith("langchain."):
            del sys.modules[mod]

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, doc, run_at=None: "/tmp/out.md",
    )
    job = create_job(prompt="daily", schedule="30m", name="daily", deliver="local")
    update_job(job["id"], {"next_run_at": "2026-05-22T09:00:00+00:00"})

    fake_runner = lambda job: JobRunResult(True, "doc", "final", None)
    result = scheduler.tick(
        now_dt=datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc),
        job_runner=fake_runner,
    )

    assert result.due == 1
    assert result.ran == 1
    assert result.succeeded == 1
    assert "cron.runner" not in sys.modules, (
        "tick() with fake runner should not import cron.runner"
    )
    assert "langchain.agents" not in sys.modules, (
        "tick() with fake runner should not import langchain.agents"
    )


def test_contracts_defines_single_source_of_truth():
    """Verify that JobRunResult is defined only in contracts."""
    from cron.contracts import JobRunResult as ContractResult

    # Verify the contracts definition is frozen dataclass
    assert hasattr(ContractResult, "__dataclass_fields__")

    # Verify delivery imports from contracts
    from cron.delivery import JobRunResult as DeliveryResult
    from cron.runner import JobRunResult as RunnerResult

    # All should be the same class (imported from contracts)
    assert ContractResult is DeliveryResult, (
        "JobRunResult should be imported from contracts in cron.delivery"
    )
    assert ContractResult is RunnerResult, (
        "JobRunResult should be imported from contracts in cron.runner"
    )
