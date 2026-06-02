from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


BASE_TIME = "2026-06-02T10:00:00+00:00"
WEBHOOK_URL = "https://example.invalid/hook"


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
    import cron.scheduler as scheduler

    tick_result = None

    def tick_fn():
        nonlocal tick_result
        tick_result = scheduler.tick(now_text=now_text, job_runner=job_runner)
        return tick_result

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
