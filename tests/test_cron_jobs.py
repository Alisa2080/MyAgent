import os
from pathlib import Path
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest


class FakeSlackAdapter:
    key = "slack"

    def validate(self, target, job):
        from cron.delivery_adapters import AdapterValidation

        if not target.address:
            return AdapterValidation(False, "slack delivery requires a channel id")
        return AdapterValidation(True)

    def deliver(self, event, job, run):
        from cron.delivery_adapters import DeliveryResult

        return DeliveryResult(True)


def test_import_cron_keeps_scheduler_lazy_until_tick_access():
    for module_name in (
        "agent_core.model_config",
        "cron",
        "cron.runner",
        "cron.scheduler",
    ):
        sys.modules.pop(module_name, None)

    import cron

    assert "tick" in cron.__all__
    assert "cron.scheduler" not in sys.modules
    assert "cron.runner" not in sys.modules
    assert "agent_core.model_config" not in sys.modules

    tick = cron.tick

    assert callable(tick)
    assert "cron.scheduler" in sys.modules


def test_cron_package_reexports_job_compatibility_api():
    import cron

    for name in (
        "JOBS_FILE",
        "create_job",
        "get_job",
        "list_jobs",
        "pause_job",
        "remove_job",
        "resume_job",
        "trigger_job",
        "update_job",
    ):
        assert hasattr(cron, name)
        assert name in cron.__all__


def test_import_cron_jobs_works_normally():
    import cron.jobs as jobs

    assert jobs.__name__ == "cron.jobs"


@pytest.fixture
def jobs_module(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    monkeypatch.setattr(
        jobs,
        "now",
        lambda: datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc),
    )
    return jobs


def test_load_save_list_get_update_remove_pause_resume_trigger(jobs_module):
    jobs = jobs_module

    jobs.save_jobs(
        [
            {
                "id": "job-1",
                "name": "one",
                "enabled": True,
                "state": "scheduled",
                "schedule": {"kind": "once", "run_at": "2026-05-22T09:30:00+00:00"},
            },
            {"id": "job-2", "name": "two", "enabled": False, "state": "paused"},
        ]
    )

    assert [job["id"] for job in jobs.load_jobs()] == ["job-1", "job-2"]
    assert [job["id"] for job in jobs.list_jobs()] == ["job-1"]
    assert [job["id"] for job in jobs.list_jobs(include_disabled=True)] == [
        "job-1",
        "job-2",
    ]
    assert jobs.get_job("job-2")["name"] == "two"

    updated = jobs.update_job("job-1", {"name": "renamed"})
    assert updated["name"] == "renamed"

    paused = jobs.pause_job("job-1", reason="manual")
    assert paused["enabled"] is False
    assert paused["state"] == "paused"
    assert paused["paused_reason"] == "manual"

    resumed = jobs.resume_job("job-1")
    assert resumed["enabled"] is True
    assert resumed["state"] == "scheduled"
    assert resumed["next_run_at"] == "2026-05-22T09:30:00+00:00"

    triggered = jobs.trigger_job("job-1")
    assert triggered["enabled"] is True
    assert triggered["state"] == "scheduled"
    assert triggered["next_run_at"] == jobs.now().isoformat()

    assert jobs.remove_job("job-2") is True
    assert jobs.remove_job("missing") is False
    assert jobs.get_job("job-2") is None


def test_save_jobs_replaces_existing_job_set(jobs_module):
    jobs = jobs_module

    jobs.save_jobs(
        [
            {
                "id": "job-1",
                "name": "one",
                "prompt": "one",
                "enabled": True,
                "state": "scheduled",
                "schedule": {"kind": "once", "run_at": "2026-05-22T09:30:00+00:00"},
            },
            {
                "id": "job-2",
                "name": "two",
                "prompt": "two",
                "enabled": True,
                "state": "scheduled",
                "schedule": {"kind": "once", "run_at": "2026-05-22T10:30:00+00:00"},
            },
        ]
    )

    jobs.save_jobs(
        [
            {
                "id": "job-1",
                "name": "one updated",
                "prompt": "one",
                "enabled": True,
                "state": "scheduled",
                "schedule": {"kind": "once", "run_at": "2026-05-22T09:30:00+00:00"},
            }
        ]
    )

    assert [job["id"] for job in jobs.load_jobs()] == ["job-1"]
    assert jobs.get_job("job-1")["name"] == "one updated"
    assert jobs.get_job("job-2") is None


def test_save_jobs_replacement_is_atomic_on_validation_error(jobs_module):
    jobs = jobs_module

    jobs.save_jobs(
        [
            {
                "id": "job-1",
                "name": "one",
                "prompt": "one",
                "enabled": True,
                "state": "scheduled",
                "schedule": {"kind": "once", "run_at": "2026-05-22T09:30:00+00:00"},
                "deliver": "local",
            }
        ]
    )

    with pytest.raises(ValueError, match="unsupported delivery target: telegram:123"):
        jobs.save_jobs(
            [
                {
                    "id": "job-1",
                    "name": "one updated",
                    "prompt": "one",
                    "enabled": True,
                    "state": "scheduled",
                    "schedule": {"kind": "once", "run_at": "2026-05-22T09:30:00+00:00"},
                    "deliver": "local",
                },
                {
                    "id": "job-2",
                    "name": "bad",
                    "prompt": "bad",
                    "enabled": True,
                    "state": "scheduled",
                    "schedule": {"kind": "once", "run_at": "2026-05-22T10:30:00+00:00"},
                    "deliver": "telegram:123",
                },
            ]
        )

    assert [job["id"] for job in jobs.load_jobs()] == ["job-1"]
    assert jobs.get_job("job-1")["name"] == "one"
    assert jobs.get_job("job-2") is None


def test_save_jobs_round_trips_legacy_unsupported_delivery(jobs_module, tmp_path):
    jobs = jobs_module
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(
        """
        {
          "jobs": [
            {
              "id": "legacy-job",
              "name": "legacy",
              "prompt": "legacy",
              "enabled": true,
              "state": "scheduled",
              "schedule": {"kind": "interval", "minutes": 60},
              "next_run_at": "2026-05-22T09:30:00+00:00",
              "repeat": {"times": null, "completed": 0},
              "deliver": "telegram:123"
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    loaded = jobs.load_jobs()
    jobs.save_jobs(loaded)

    assert jobs.get_job("legacy-job")["deliver"] == "telegram:123"
    assert jobs.get_job("legacy-job")["delivery_targets"][0]["adapter_key"] == "telegram"


def test_single_job_mutators_do_not_replace_unrelated_jobs(jobs_module):
    jobs = jobs_module
    base = jobs.now()
    once_job = jobs.create_job(prompt="once", schedule="30m", deliver="local")
    interval_job = jobs.create_job(prompt="interval", schedule="every 30m", deliver="local")
    complete_job = jobs.create_job(prompt="complete", schedule="30m", deliver="local")

    assert jobs.resume_job(once_job["id"])["state"] == "scheduled"
    assert jobs.advance_next_run(interval_job["id"], base)["next_run_at"] == (
        base + timedelta(minutes=30)
    ).isoformat()
    assert jobs.mark_job_run(complete_job["id"], success=True, run_at=base)[
        "state"
    ] == "completed"
    assert {job["id"] for job in jobs.load_jobs()} == {
        once_job["id"],
        interval_job["id"],
        complete_job["id"],
    }


def test_parse_duration_schedule_creates_oneshot(jobs_module):
    jobs = jobs_module

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)

    parsed = jobs.parse_schedule("30m")

    assert parsed["kind"] == "once"
    assert parsed["run_at"] == (base + timedelta(minutes=30)).isoformat()


def test_parse_interval_cron_and_iso_schedules(jobs_module):
    jobs = jobs_module

    interval = jobs.parse_schedule("every 2h")
    cron = jobs.parse_schedule("0 9 * * *")
    iso = jobs.parse_schedule("2026-05-23T14:30:00+00:00")

    assert interval == {"kind": "interval", "minutes": 120, "display": "every 120m"}
    assert cron == {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"}
    assert iso["kind"] == "once"
    assert iso["run_at"] == "2026-05-23T14:30:00+00:00"


def test_create_job_defaults_origin_delivery(jobs_module):
    jobs = jobs_module

    job = jobs.create_job(
        prompt="write a report",
        schedule="30m",
        origin={"thread_id": "thread-1"},
    )

    assert job["deliver"] == "origin"
    assert job["repeat"] == {"times": 1, "completed": 0}
    assert jobs.get_job(job["id"])["name"] == "write a report"


def test_update_job_clears_origin_when_delivery_no_longer_targets_origin(jobs_module):
    jobs = jobs_module

    job = jobs.create_job(
        prompt="write a report",
        schedule="30m",
        deliver="origin",
        origin={"source_type": "cli", "session_id": "session-1", "thread_id": "thread-1"},
    )

    updated = jobs.update_job(job["id"], {"deliver": "local"})

    assert updated["deliver"] == "local"
    assert updated["origin"] is None
    assert updated["delivery_targets"][0]["target_type"] == "local"


def test_create_job_accepts_registered_platform_adapter_and_persists_target(jobs_module, monkeypatch):
    from cron.delivery_registry import default_delivery_registry

    registry = default_delivery_registry()
    registry.register(FakeSlackAdapter())
    monkeypatch.setattr("cron.delivery_registry.default_delivery_registry", lambda **kwargs: registry)

    job = jobs_module.create_job(
        prompt="write a report",
        schedule="30m",
        deliver="slack:C123",
    )

    assert job["deliver"] == "slack:C123"
    assert job["origin"] is None
    assert job["delivery_targets"] == [
        {
            "raw": "slack:C123",
            "target_type": "platform",
            "adapter_key": "slack",
            "address": "C123",
            "thread_id": None,
            "metadata": {},
        }
    ]


def test_create_job_recurring_repeat_defaults_to_forever(jobs_module):
    jobs = jobs_module

    job = jobs.create_job(prompt="x", schedule="every 30m", deliver="local")

    assert job["repeat"] == {"times": None, "completed": 0}


def test_workdir_must_be_absolute(jobs_module):
    jobs = jobs_module

    with pytest.raises(ValueError, match="absolute path"):
        jobs.create_job(prompt="x", schedule="30m", workdir="relative/path")


def test_workdir_must_exist_and_be_directory(jobs_module, tmp_path):
    jobs = jobs_module

    missing = tmp_path / "missing"
    regular_file = tmp_path / "file.txt"
    regular_file.write_text("content", encoding="utf-8")
    existing_dir = tmp_path / "work"
    existing_dir.mkdir()

    with pytest.raises(ValueError, match="does not exist"):
        jobs.create_job(prompt="x", schedule="30m", workdir=str(missing))
    with pytest.raises(ValueError, match="not a directory"):
        jobs.create_job(prompt="x", schedule="30m", workdir=str(regular_file))

    job = jobs.create_job(prompt="x", schedule="30m", workdir=str(existing_dir))
    assert job["workdir"] == str(existing_dir.resolve())


def test_get_due_jobs_filters_disabled_and_unscheduled_states(jobs_module):
    jobs = jobs_module
    base = jobs.now()
    due = jobs.create_job(prompt="due", schedule="30m", deliver="local")
    disabled = jobs.create_job(prompt="disabled", schedule="30m", deliver="local")
    paused = jobs.create_job(prompt="paused", schedule="30m", deliver="local")
    jobs.update_job(due["id"], {"next_run_at": base.isoformat()})
    jobs.update_job(
        disabled["id"], {"enabled": False, "next_run_at": base.isoformat()}
    )
    jobs.update_job(paused["id"], {"state": "paused", "next_run_at": base.isoformat()})

    assert [job["id"] for job in jobs.get_due_jobs(now_dt=base)] == [due["id"]]


def test_get_due_jobs_uses_oneshot_recovery_window(jobs_module):
    jobs = jobs_module
    base = jobs.now()
    recovered = jobs.create_job(prompt="recover", schedule="30m", deliver="local")
    expired = jobs.create_job(prompt="expired", schedule="30m", deliver="local")
    jobs.update_job(
        recovered["id"],
        {"next_run_at": (base - timedelta(seconds=120)).isoformat()},
    )
    jobs.update_job(
        expired["id"],
        {"next_run_at": (base - timedelta(seconds=121)).isoformat()},
    )

    assert [job["id"] for job in jobs.get_due_jobs(now_dt=base)] == [recovered["id"]]


def test_get_due_jobs_returns_non_stale_recurring_jobs(jobs_module):
    jobs = jobs_module
    base = jobs.now()
    job = jobs.create_job(prompt="x", schedule="every 10m", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": (base - timedelta(minutes=4)).isoformat()})

    due = jobs.get_due_jobs(now_dt=base)

    assert [item["id"] for item in due] == [job["id"]]


def test_get_due_jobs_fast_forwards_stale_interval(jobs_module):
    jobs = jobs_module

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    job = jobs.create_job(prompt="x", schedule="every 10m", deliver="local")
    jobs.update_job(
        job["id"],
        {"next_run_at": (base - timedelta(hours=3)).isoformat()},
    )

    due = jobs.get_due_jobs(now_dt=base)

    assert due == []
    refreshed = jobs.get_job(job["id"])
    assert datetime.fromisoformat(refreshed["next_run_at"]) > base


def test_get_due_jobs_clamps_recurring_grace(jobs_module):
    jobs = jobs_module
    base = jobs.now()
    lower_clamped = jobs.create_job(prompt="lower", schedule="every 1m", deliver="local")
    upper_clamped = jobs.create_job(prompt="upper", schedule="every 1d", deliver="local")
    jobs.update_job(
        lower_clamped["id"],
        {"next_run_at": (base - timedelta(seconds=121)).isoformat()},
    )
    jobs.update_job(
        upper_clamped["id"],
        {"next_run_at": (base - timedelta(seconds=7201)).isoformat()},
    )

    due = jobs.get_due_jobs(now_dt=base)

    assert due == []
    assert datetime.fromisoformat(jobs.get_job(lower_clamped["id"])["next_run_at"]) > base
    assert datetime.fromisoformat(jobs.get_job(upper_clamped["id"])["next_run_at"]) > base


def test_advance_next_run_once_and_cron(jobs_module):
    jobs = jobs_module
    base = jobs.now()
    once = jobs.create_job(prompt="once", schedule="30m", deliver="local")
    cron = jobs.create_job(prompt="cron", schedule="0 10 * * *", deliver="local")

    advanced_once = jobs.advance_next_run(once["id"], base)
    advanced_cron = jobs.advance_next_run(cron["id"], base)

    assert advanced_once["next_run_at"] is None
    assert advanced_cron["next_run_at"] == "2026-05-22T10:00:00+00:00"


def test_advance_next_run_before_mark_run(jobs_module):
    jobs = jobs_module

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    job = jobs.create_job(prompt="x", schedule="every 30m", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": base.isoformat()})

    advanced = jobs.advance_next_run(job["id"], base)
    jobs.mark_job_run(job["id"], success=True, error=None, run_at=base)

    assert advanced["next_run_at"] == (base + timedelta(minutes=30)).isoformat()
    final = jobs.get_job(job["id"])
    assert final["last_status"] == "ok"
    assert final["repeat"]["completed"] == 1


def test_failed_recurring_job_remains_due_at_advanced_next_run(jobs_module):
    jobs = jobs_module

    base = datetime(2026, 5, 22, 9, 0, tzinfo=timezone.utc)
    next_run = base + timedelta(minutes=30)
    job = jobs.create_job(prompt="x", schedule="every 30m", deliver="local")
    jobs.update_job(job["id"], {"next_run_at": base.isoformat()})

    advanced = jobs.advance_next_run(job["id"], base)
    marked = jobs.mark_job_run(
        job["id"], success=False, error="boom", run_at=base
    )

    assert advanced["next_run_at"] == next_run.isoformat()
    assert marked["last_status"] == "error"
    assert marked["last_error"] == "boom"
    assert marked["state"] == "scheduled"
    assert marked["enabled"] is True
    assert marked["repeat"] == {"times": None, "completed": 1}
    assert jobs.get_due_jobs(now_dt=next_run - timedelta(seconds=1)) == []
    assert [due["id"] for due in jobs.get_due_jobs(now_dt=next_run)] == [job["id"]]


def test_mark_job_run_error_and_completion_disable(jobs_module):
    jobs = jobs_module
    base = jobs.now()
    error_job = jobs.create_job(
        prompt="x", schedule="every 30m", repeat=2, deliver="local"
    )
    completion_job = jobs.create_job(prompt="x", schedule="30m", deliver="local")

    errored = jobs.mark_job_run(
        error_job["id"], success=False, error="boom", run_at=base
    )
    completed = jobs.mark_job_run(completion_job["id"], success=True, run_at=base)

    assert errored["last_run_at"] == base.isoformat()
    assert errored["last_status"] == "error"
    assert errored["last_error"] == "boom"
    assert errored["repeat"] == {"times": 2, "completed": 1}
    assert errored["state"] == "scheduled"
    assert errored["enabled"] is True
    assert completed["state"] == "completed"
    assert completed["enabled"] is False


def test_mark_job_run_separates_execution_and_delivery_errors(jobs_module):
    jobs = jobs_module
    base = jobs.now()
    job = jobs.create_job(prompt="x", schedule="every 30m", deliver="local")

    marked = jobs.mark_job_run(
        job["id"],
        success=True,
        error=None,
        delivery_error="Unsupported delivery target: telegram:123",
        run_at=base,
    )

    assert marked["last_status"] == "ok"
    assert marked["last_error"] is None
    assert marked["last_delivery_error"] == "Unsupported delivery target: telegram:123"


def test_save_and_latest_job_output(jobs_module, tmp_path):
    jobs = jobs_module
    first_run = jobs.now()
    second_run = first_run + timedelta(minutes=1)

    first_path = Path(jobs.save_job_output("job-1", "first", run_at=first_run))
    second_path = Path(jobs.save_job_output("job-1", "second", run_at=second_run))

    assert first_path.name == "20260522_090000.md"
    assert second_path.name == "20260522_090100.md"
    assert first_path.parent == tmp_path / "cron" / "output" / "job-1"
    assert second_path.read_text(encoding="utf-8") == "second"
    if os.name != "nt":
        assert stat.S_IMODE(second_path.stat().st_mode) == 0o600
    assert jobs.latest_job_output("job-1") == "second"
    assert jobs.latest_job_output("missing") is None
