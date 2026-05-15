from types import SimpleNamespace

from agent_core.session_context import hermes_task_id_from_runtime, hermes_task_id_from_thread_id


def test_hermes_task_id_is_deterministic_and_prefixed():
    first = hermes_task_id_from_thread_id("thread-123")
    second = hermes_task_id_from_thread_id("thread-123")

    assert first == second
    assert first.startswith("lg_")
    assert len(first) == 27


def test_hermes_task_id_does_not_embed_raw_thread_id():
    task_id = hermes_task_id_from_thread_id("user@example.com/session/abc")

    assert "user@example.com" not in task_id
    assert "/" not in task_id
    assert task_id.startswith("lg_")


def test_missing_thread_id_falls_back_to_default():
    assert hermes_task_id_from_thread_id(None) == "default"
    assert hermes_task_id_from_thread_id("") == "default"
    assert hermes_task_id_from_runtime(None) == "default"


def test_runtime_execution_info_thread_id_is_used():
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-from-runtime"),
        config={"configurable": {"thread_id": "thread-from-config"}},
    )

    assert hermes_task_id_from_runtime(runtime) == hermes_task_id_from_thread_id("thread-from-runtime")


def test_runtime_config_thread_id_is_fallback_when_execution_info_missing():
    runtime = SimpleNamespace(
        execution_info=None,
        config={"configurable": {"thread_id": "thread-from-config"}},
    )

    assert hermes_task_id_from_runtime(runtime) == hermes_task_id_from_thread_id("thread-from-config")
