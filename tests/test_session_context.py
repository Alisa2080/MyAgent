from types import SimpleNamespace

from agent_core.session_context import (
    RuntimeContext,
    hermes_task_id_from_runtime,
    hermes_task_id_from_thread_id,
)


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


def test_runtime_context_prefers_execution_info_thread_id():
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-from-runtime"),
        config={"configurable": {"thread_id": "thread-from-config"}},
        tool_call_id="call-123",
    )

    ctx = RuntimeContext.from_runtime(runtime)

    assert ctx.thread_id == "thread-from-runtime"
    assert ctx.task_id == hermes_task_id_from_thread_id("thread-from-runtime")
    assert ctx.tool_call_id == "call-123"
    assert ctx.thread_source == "execution_info"
    assert ctx.has_thread is True
    assert ctx.is_default_task is False


def test_runtime_context_falls_back_to_config_thread_id():
    runtime = SimpleNamespace(
        execution_info=None,
        config={"configurable": {"thread_id": "thread-from-config"}},
        tool_call_id="call-456",
    )

    ctx = RuntimeContext.from_runtime(runtime)

    assert ctx.thread_id == "thread-from-config"
    assert ctx.task_id == hermes_task_id_from_thread_id("thread-from-config")
    assert ctx.tool_call_id == "call-456"
    assert ctx.thread_source == "config"
    assert ctx.has_thread is True
    assert ctx.is_default_task is False


def test_runtime_context_falls_back_to_default_without_runtime():
    ctx = RuntimeContext.from_runtime(None)

    assert ctx.thread_id is None
    assert ctx.task_id == "default"
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "fallback"
    assert ctx.has_thread is False
    assert ctx.is_default_task is True


def test_runtime_context_treats_empty_thread_ids_as_missing():
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id=""),
        config={"configurable": {"thread_id": ""}},
        tool_call_id="",
    )

    ctx = RuntimeContext.from_runtime(runtime)

    assert ctx.thread_id is None
    assert ctx.task_id == "default"
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "fallback"
    assert ctx.has_thread is False
    assert ctx.is_default_task is True


def test_runtime_context_ignores_non_dict_config():
    runtime = SimpleNamespace(
        execution_info=None,
        config="not-a-dict",
        tool_call_id=None,
    )

    ctx = RuntimeContext.from_runtime(runtime)

    assert ctx.thread_id is None
    assert ctx.task_id == "default"
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "fallback"


def test_runtime_context_from_thread_id():
    ctx = RuntimeContext.from_thread_id("manual-thread")

    assert ctx.thread_id == "manual-thread"
    assert ctx.task_id == hermes_task_id_from_thread_id("manual-thread")
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "fallback"
    assert ctx.has_thread is True
    assert ctx.is_default_task is False
