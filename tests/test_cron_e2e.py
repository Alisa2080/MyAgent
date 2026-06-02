from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest


BASE_TIME = "2026-06-02T10:00:00+00:00"
WEBHOOK_URL = "https://example.invalid/hook"
WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=e2e"


@pytest.fixture(autouse=True)
def isolated_cron_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))
    import cron.delivery_registry as delivery_registry

    delivery_registry.clear_delivery_adapter_factories()
    yield tmp_path
    delivery_registry.clear_delivery_adapter_factories()


def install_webhook_sender(
    sender: Callable[[str, dict[str, Any], int], tuple[int, str]],
) -> None:
    import cron.delivery_registry as delivery_registry

    def factory(**kwargs):
        from cron.delivery_adapters import WebhookDeliveryAdapter

        return WebhookDeliveryAdapter(sender=sender)

    delivery_registry.register_delivery_adapter_factory(factory)


def install_wecom_sender(
    sender: Callable[[str, dict[str, Any], int], tuple[int, str]],
) -> None:
    import cron.delivery_registry as delivery_registry

    def factory(**kwargs):
        from cron.delivery_adapters import WeComDeliveryAdapter

        return WeComDeliveryAdapter(sender=sender)

    delivery_registry.register_delivery_adapter_factory(factory)


class RecordingRunner:
    def __init__(
        self,
        *,
        success: bool = True,
        output_doc: str = "# E2E Output",
        final_response: str = "delivered response",
        error: str | None = None,
    ):
        self.success = success
        self.output_doc = output_doc
        self.final_response = final_response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, job: dict[str, Any]):
        from cron.contracts import JobRunResult

        self.calls.append(dict(job))
        return JobRunResult(
            success=self.success,
            output_doc=self.output_doc,
            final_response=self.final_response,
            error=self.error,
        )


class ScriptedWebhookSender:
    def __init__(self, responses: list[tuple[int, str]]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def __call__(
        self,
        url: str,
        payload: dict[str, Any],
        timeout: int = 10,
    ) -> tuple[int, str]:
        self.calls.append((url, payload, timeout))
        if self.responses:
            return self.responses.pop(0)
        return 200, "ok"


def run_service_once(now_text: str, job_runner):
    from cron.service import CronService
    import cron.delivery_store as delivery_store
    import cron.scheduler as scheduler
    import cron.state_store as state_store

    tick_result = None
    now = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
    original_state_utc_now = state_store.utc_now
    original_delivery_utc_now = delivery_store.utc_now

    def tick_fn():
        nonlocal tick_result
        tick_result = scheduler.tick(now_text=now_text, job_runner=job_runner)
        return tick_result

    try:
        state_store.utc_now = lambda: now
        delivery_store.utc_now = lambda: now
        service = CronService(
            interval_seconds=1,
            lease_seconds=60,
            owner_id="e2e-service",
            pid=4242,
            hostname="e2e-host",
            tick_fn=tick_fn,
            clock=lambda: now_text,
            sleeper=lambda _seconds: None,
        )
        exit_code = service.run(once=True)
    finally:
        state_store.utc_now = original_state_utc_now
        delivery_store.utc_now = original_delivery_utc_now
    return service, tick_result, exit_code


def create_due_job(
    *,
    prompt: str = "write report",
    deliver: str = f"webhook:{WEBHOOK_URL}",
    concurrency_key: str | None = None,
    concurrency_policy: str | None = None,
    next_run_at: str = BASE_TIME,
):
    from cron.jobs import create_job, update_job

    kwargs: dict[str, Any] = {
        "prompt": prompt,
        "schedule": "every 30m",
        "name": prompt,
        "deliver": deliver,
    }
    if concurrency_key is not None:
        kwargs["concurrency_key"] = concurrency_key
    if concurrency_policy is not None:
        kwargs["concurrency_policy"] = concurrency_policy
    job = create_job(**kwargs)
    return update_job(
        job["id"],
        {"next_run_at": next_run_at, "state": "scheduled", "enabled": True},
    )


def force_run_running(store, *, run_id: str, job_id: str, at_text: str) -> None:
    at = datetime.fromisoformat(at_text.replace("Z", "+00:00"))
    lease_expires_at = (at + timedelta(seconds=60)).isoformat()
    with store._connect() as conn:
        conn.execute(
            """
            UPDATE runs
            SET status = 'running',
                started_at = ?,
                heartbeat_at = ?,
                last_activity_at = ?,
                last_activity_desc = 'running',
                updated_at = ?
            WHERE id = ?
            """,
            (at_text, at_text, at_text, at_text, run_id),
        )
        conn.execute(
            """
            UPDATE jobs
            SET state = 'running',
                lease_run_id = ?,
                lease_expires_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (run_id, lease_expires_at, at_text, job_id),
        )


def test_service_executes_due_job_saves_output_and_delivers_webhook(isolated_cron_home):
    from cron.service_state import read_service_status
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    sender = ScriptedWebhookSender([(200, "ok")])
    install_webhook_sender(sender)
    job = create_due_job()
    runner = RecordingRunner(
        output_doc="# E2E Output\nbody",
        final_response="final e2e response",
    )

    service, tick_result, exit_code = run_service_once(BASE_TIME, runner)

    store = StateStore()
    delivery_store = DeliveryStore()
    runs = store.runs_for_job(job["id"])
    events = delivery_store.list_events(job_id=job["id"], limit=10)
    service_status = read_service_status()

    assert exit_code == 0
    assert tick_result.ran == 1
    assert len(runner.calls) == 1
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["delivery_status"] == "delivered"
    assert runs[0]["output_path"]
    output_path = Path(runs[0]["output_path"])
    assert output_path.exists()
    assert "# E2E Output" in output_path.read_text(encoding="utf-8")
    assert len(events) == 1
    assert events[0]["status"] == "delivered"
    assert events[0]["attempt_count"] == 1
    assert events[0]["run_id"] == runs[0]["id"]
    assert sender.calls[0][0] == WEBHOOK_URL
    assert store.get_job(job["id"])["last_delivery_error"] is None
    assert service.status["last_tick"]["ran"] == 1
    assert service.status["last_tick"]["delivery"]["error"] is None
    assert service_status["last_tick"]["ran"] == 1


def test_service_retries_failed_delivery_on_no_due_tick(isolated_cron_home):
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    sender = ScriptedWebhookSender([(500, "down"), (200, "ok")])
    install_webhook_sender(sender)
    job = create_due_job()
    runner = RecordingRunner(output_doc="# retry output", final_response="retry me")
    delivery_store = DeliveryStore()

    _service1, first_tick, first_exit = run_service_once(BASE_TIME, runner)

    store = StateStore()
    first_run = store.runs_for_job(job["id"])[0]
    first_events = delivery_store.list_events(job_id=job["id"], limit=10)
    assert first_exit == 0
    assert first_tick.ran == 1
    assert len(runner.calls) == 1
    assert first_run["status"] == "succeeded"
    assert first_run["delivery_status"] in {"retrying", "failed"}
    assert first_events[0]["status"] == "failed"
    assert "HTTP 500" in (store.get_job(job["id"])["last_delivery_error"] or "")

    delivery_store.update_event(
        first_events[0]["id"],
        next_attempt_at="2000-01-01T00:00:00+00:00",
    )
    _service2, second_tick, second_exit = run_service_once(
        "2026-06-02T10:01:00+00:00",
        runner,
    )

    second_run = store.get_run(first_run["id"])
    second_events = delivery_store.list_events(job_id=job["id"], limit=10)
    assert second_exit == 0
    assert second_tick.due == 0
    assert second_tick.ran == 0
    assert len(runner.calls) == 1
    assert second_tick.delivery.claimed >= 1
    assert second_tick.delivery.delivered == 1
    assert second_events[0]["status"] == "delivered"
    assert second_run["delivery_status"] == "delivered"
    assert store.get_job(job["id"])["last_delivery_error"] is None
    assert len(sender.calls) == 2


def test_service_executes_due_job_and_delivers_wecom_markdown(
    isolated_cron_home,
    monkeypatch,
):
    from cron.service_state import read_service_status
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    monkeypatch.setenv("AGENT_CRON_WECOM_WEBHOOK_URL", WECOM_URL)
    sender = ScriptedWebhookSender([(200, '{"errcode":0,"errmsg":"ok"}')])
    install_wecom_sender(sender)
    job = create_due_job(deliver="wecom")
    runner = RecordingRunner(
        output_doc="# WeCom E2E Output\nbody",
        final_response="final wecom response",
    )

    service, tick_result, exit_code = run_service_once(BASE_TIME, runner)

    store = StateStore()
    delivery_store = DeliveryStore()
    runs = store.runs_for_job(job["id"])
    events = delivery_store.list_events(job_id=job["id"], limit=10)
    service_status = read_service_status()
    payload = sender.calls[0][1]
    content = payload["markdown"]["content"]

    assert exit_code == 0
    assert tick_result.ran == 1
    assert len(runner.calls) == 1
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["delivery_status"] == "delivered"
    assert runs[0]["output_path"]
    output_path = Path(runs[0]["output_path"])
    assert output_path.exists()
    assert len(events) == 1
    assert events[0]["adapter_key"] == "wecom"
    assert events[0]["status"] == "delivered"
    assert events[0]["attempt_count"] == 1
    assert events[0]["run_id"] == runs[0]["id"]
    assert sender.calls[0][0] == WECOM_URL
    assert payload["msgtype"] == "markdown"
    assert "final wecom response" in content
    assert job["id"] in content
    assert runs[0]["id"] in content
    assert str(output_path) in content
    assert store.get_job(job["id"])["last_delivery_error"] is None
    assert service.status["last_tick"]["delivery"]["delivered"] == 1
    assert service_status["last_tick"]["delivery"]["delivered"] == 1


def test_service_retries_failed_wecom_delivery_on_no_due_tick(
    isolated_cron_home,
    monkeypatch,
):
    from cron.delivery_store import DeliveryStore
    from cron.state_store import StateStore

    monkeypatch.setenv("AGENT_CRON_WECOM_WEBHOOK_URL", WECOM_URL)
    sender = ScriptedWebhookSender(
        [(500, "down"), (200, '{"errcode":0,"errmsg":"ok"}')]
    )
    install_wecom_sender(sender)
    job = create_due_job(deliver="wecom")
    runner = RecordingRunner(
        output_doc="# retry wecom output",
        final_response="retry wecom",
    )
    delivery_store = DeliveryStore()

    _service1, first_tick, first_exit = run_service_once(BASE_TIME, runner)

    store = StateStore()
    first_run = store.runs_for_job(job["id"])[0]
    first_events = delivery_store.list_events(job_id=job["id"], limit=10)
    assert first_exit == 0
    assert first_tick.ran == 1
    assert len(runner.calls) == 1
    assert first_run["status"] == "succeeded"
    assert first_run["delivery_status"] in {"retrying", "failed"}
    assert first_events[0]["adapter_key"] == "wecom"
    assert first_events[0]["status"] == "failed"
    assert "HTTP 500" in (store.get_job(job["id"])["last_delivery_error"] or "")

    delivery_store.update_event(
        first_events[0]["id"],
        next_attempt_at="2000-01-01T00:00:00+00:00",
    )
    _service2, second_tick, second_exit = run_service_once(
        "2026-06-02T10:01:00+00:00",
        runner,
    )

    second_run = store.get_run(first_run["id"])
    second_events = delivery_store.list_events(job_id=job["id"], limit=10)
    assert second_exit == 0
    assert second_tick.due == 0
    assert second_tick.ran == 0
    assert len(runner.calls) == 1
    assert second_tick.delivery.claimed >= 1
    assert second_tick.delivery.delivered == 1
    assert second_events[0]["status"] == "delivered"
    assert second_run["delivery_status"] == "delivered"
    assert store.get_job(job["id"])["last_delivery_error"] is None
    assert len(sender.calls) == 2


def test_fresh_service_recovers_stale_run_and_promotes_queued_run(isolated_cron_home):
    from cron.state_store import StateStore

    stale_job = create_due_job(
        prompt="stale",
        deliver="local",
        concurrency_key="repo:restart",
        concurrency_policy="queue_all",
        next_run_at="2026-06-02T09:00:00+00:00",
    )
    queued_job = create_due_job(
        prompt="queued",
        deliver="local",
        concurrency_key="repo:restart",
        concurrency_policy="queue_all",
        next_run_at="2026-06-02T09:00:00+00:00",
    )
    store = StateStore(lease_seconds=60)
    first_claim = store.claim_due_jobs(
        now_text="2026-06-02T09:00:00+00:00",
        limit=1,
    )[0]
    stale_run_id = first_claim["run"]["id"]
    store.mark_run_started(stale_run_id)
    store.claim_due_jobs(now_text="2026-06-02T09:00:00+00:00", limit=10)

    with store._connect() as conn:
        conn.execute(
            """
            UPDATE runs
            SET heartbeat_at = ?, last_activity_at = ?, status = 'running'
            WHERE id = ?
            """,
            (
                "2026-06-02T09:00:00+00:00",
                "2026-06-02T09:00:00+00:00",
                stale_run_id,
            ),
        )
        conn.execute(
            """
            UPDATE jobs
            SET state = 'running', lease_run_id = ?, lease_expires_at = ?
            WHERE id = ?
            """,
            (
                stale_run_id,
                "2026-06-02T09:01:00+00:00",
                stale_job["id"],
            ),
        )

    runner = RecordingRunner(
        output_doc="# recovered queue",
        final_response="queued done",
    )
    service, tick_result, exit_code = run_service_once(
        "2026-06-02T10:00:00+00:00",
        runner,
    )

    stale_run = store.get_run(stale_run_id)
    queued_runs = store.runs_for_job(queued_job["id"])
    succeeded = [run for run in queued_runs if run["status"] == "succeeded"]

    assert exit_code == 0
    assert stale_run["status"] in {"abandoned", "failed"}
    assert stale_run["exit_reason"] in {
        "lease_expired",
        "idle_timeout",
        "heartbeat_stale",
    }
    assert len(succeeded) == 1
    assert Path(succeeded[0]["output_path"]).exists()
    assert len(runner.calls) == 1
    assert runner.calls[0]["id"] == queued_job["id"]
    assert tick_result.ran == 1
    assert service.status["owner_id"] == "e2e-service"
    assert service.status["last_tick"]["ran"] == 1


def test_skip_if_running_policy_is_enforced_through_service_tick(isolated_cron_home):
    from cron.state_store import StateStore

    first = create_due_job(
        prompt="first skip",
        deliver="local",
        concurrency_key="repo:skip",
        concurrency_policy="skip_if_running",
    )
    second = create_due_job(
        prompt="second skip",
        deliver="local",
        concurrency_key="repo:skip",
        concurrency_policy="skip_if_running",
    )
    store = StateStore()
    first_run = store.claim_due_jobs(now_text=BASE_TIME, limit=1)[0]["run"]
    force_run_running(
        store,
        run_id=first_run["id"],
        job_id=first_run["job_id"],
        at_text=BASE_TIME,
    )

    runner = RecordingRunner()
    service, tick_result, exit_code = run_service_once(BASE_TIME, runner)

    skipped = [
        run for run in store.runs_for_job(second["id"]) if run["status"] == "skipped"
    ]
    assert exit_code == 0
    assert tick_result.ran == 0
    assert runner.calls == []
    assert len(skipped) == 1
    assert skipped[0]["exit_reason"] == "concurrency_skip"
    assert store.get_run(first_run["id"])["status"] == "running"
    assert service.status["last_tick"]["ran"] == 0
    assert first["id"] != second["id"]


def test_replace_running_policy_replaces_old_run_and_executes_newer_through_service_tick(
    isolated_cron_home,
):
    from cron.state_store import StateStore

    first = create_due_job(
        prompt="first replace",
        deliver="local",
        concurrency_key="repo:replace",
        concurrency_policy="replace_running",
    )
    second = create_due_job(
        prompt="second replace",
        deliver="local",
        concurrency_key="repo:replace",
        concurrency_policy="replace_running",
    )
    store = StateStore()
    first_run = store.claim_due_jobs(now_text=BASE_TIME, limit=1)[0]["run"]
    force_run_running(
        store,
        run_id=first_run["id"],
        job_id=first_run["job_id"],
        at_text=BASE_TIME,
    )

    runner = RecordingRunner(
        output_doc="# replacement",
        final_response="replacement done",
    )
    service, tick_result, exit_code = run_service_once(BASE_TIME, runner)

    first_after = store.get_run(first_run["id"])
    second_runs = store.runs_for_job(second["id"])
    succeeded = [run for run in second_runs if run["status"] == "succeeded"]
    assert exit_code == 0
    assert first_after["status"] == "abandoned"
    assert first_after["exit_reason"] == "replaced_by_newer_run"
    assert len(succeeded) == 1
    assert first_after["replaced_by_run_id"] == succeeded[0]["id"]
    assert [call["id"] for call in runner.calls] == [second["id"]]
    assert tick_result.ran == 1
    assert service.status["last_tick"]["ran"] == 1
    assert first["id"] != second["id"]


def test_queue_all_policy_promotes_queued_run_after_key_is_free_through_service_tick(
    isolated_cron_home,
):
    from cron.state_store import StateStore

    first = create_due_job(
        prompt="first queue",
        deliver="local",
        concurrency_key="repo:queue",
        concurrency_policy="queue_all",
    )
    second = create_due_job(
        prompt="second queue",
        deliver="local",
        concurrency_key="repo:queue",
        concurrency_policy="queue_all",
    )
    store = StateStore()
    first_run = store.claim_due_jobs(now_text=BASE_TIME, limit=1)[0]["run"]
    store.claim_due_jobs(now_text=BASE_TIME, limit=10)
    queued_before = [
        run for run in store.runs_for_job(second["id"]) if run["status"] == "queued"
    ]
    assert len(queued_before) == 1

    store.complete_run(
        first_run["id"],
        success=True,
        output_path=None,
        final_response="first complete",
        error=None,
        next_run_at=None,
        completed=False,
    )
    runner = RecordingRunner(
        output_doc="# queued promoted",
        final_response="queued complete",
    )
    service, tick_result, exit_code = run_service_once(
        "2026-06-02T10:01:00+00:00",
        runner,
    )

    second_runs = store.runs_for_job(second["id"])
    succeeded = [run for run in second_runs if run["status"] == "succeeded"]
    assert exit_code == 0
    assert len(succeeded) == 1
    assert Path(succeeded[0]["output_path"]).exists()
    assert [call["id"] for call in runner.calls] == [second["id"]]
    assert tick_result.due == 1
    assert tick_result.ran == 1
    assert service.status["last_tick"]["ran"] == 1
    assert first["id"] != second["id"]
