from types import SimpleNamespace

from agent_core.session_context import (
    RuntimeContext,
    runtime_task_id_from_runtime,
    runtime_task_id_from_thread_id,
)


def test_terminal_task_id_is_deterministic_and_prefixed():
    first = runtime_task_id_from_thread_id("thread-123")
    second = runtime_task_id_from_thread_id("thread-123")

    assert first == second
    assert first.startswith("lg_")
    assert len(first) == 27


def test_terminal_task_id_does_not_embed_raw_thread_id():
    task_id = runtime_task_id_from_thread_id("user@example.com/session/abc")

    assert "user@example.com" not in task_id
    assert "/" not in task_id
    assert task_id.startswith("lg_")


def test_missing_thread_id_falls_back_to_default():
    assert runtime_task_id_from_thread_id(None) == "default"
    assert runtime_task_id_from_thread_id("") == "default"
    assert runtime_task_id_from_runtime(None) == "default"


def test_runtime_execution_info_thread_id_is_used():
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-from-runtime"),
        config={"configurable": {"thread_id": "thread-from-config"}},
    )

    assert runtime_task_id_from_runtime(runtime) == runtime_task_id_from_thread_id("thread-from-runtime")


def test_runtime_task_id_ignores_raising_tool_call_id_property():
    class RuntimeWithRaisingToolCallId:
        execution_info = SimpleNamespace(thread_id="thread-from-runtime")

        @property
        def tool_call_id(self):
            raise RuntimeError("tool call id unavailable")

    runtime = RuntimeWithRaisingToolCallId()

    assert runtime_task_id_from_runtime(runtime) == runtime_task_id_from_thread_id("thread-from-runtime")
    assert RuntimeContext.from_runtime(runtime).tool_call_id is None


def test_runtime_config_thread_id_is_fallback_when_execution_info_missing():
    runtime = SimpleNamespace(
        execution_info=None,
        config={"configurable": {"thread_id": "thread-from-config"}},
    )

    assert runtime_task_id_from_runtime(runtime) == runtime_task_id_from_thread_id("thread-from-config")


def test_runtime_context_prefers_execution_info_thread_id():
    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="thread-from-runtime"),
        config={"configurable": {"thread_id": "thread-from-config"}},
        tool_call_id="call-123",
    )

    ctx = RuntimeContext.from_runtime(runtime)

    assert ctx.thread_id == "thread-from-runtime"
    assert ctx.task_id == runtime_task_id_from_thread_id("thread-from-runtime")
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
    assert ctx.task_id == runtime_task_id_from_thread_id("thread-from-config")
    assert ctx.tool_call_id == "call-456"
    assert ctx.thread_source == "config"
    assert ctx.has_thread is True
    assert ctx.is_default_task is False


def test_runtime_context_from_config_uses_config_thread_id():
    ctx = RuntimeContext.from_config({"configurable": {"thread_id": "thread-from-config"}})

    assert ctx.thread_id == "thread-from-config"
    assert ctx.task_id == runtime_task_id_from_thread_id("thread-from-config")
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "config"
    assert ctx.has_thread is True
    assert ctx.is_default_task is False


def test_runtime_context_from_config_falls_back_for_missing_or_non_dict_config():
    for config in (None, "not-a-dict", {}, {"configurable": "not-a-dict"}, {"configurable": {}}):
        ctx = RuntimeContext.from_config(config)

        assert ctx.thread_id is None
        assert ctx.task_id == "default"
        assert ctx.tool_call_id is None
        assert ctx.thread_source == "fallback"
        assert ctx.has_thread is False
        assert ctx.is_default_task is True


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
    assert ctx.task_id == runtime_task_id_from_thread_id("manual-thread")
    assert ctx.tool_call_id is None
    assert ctx.thread_source == "fallback"
    assert ctx.has_thread is True
    assert ctx.is_default_task is False


def test_runtime_context_extracts_cli_origin_identity_from_config_thread():
    from agent_core.session_context import origin_identity_from_runtime

    runtime = SimpleNamespace(config={"configurable": {"thread_id": "thread-1", "session_id": "session-1"}})

    assert origin_identity_from_runtime(runtime) == {
        "source_type": "cli",
        "session_id": "session-1",
        "thread_id": "thread-1",
    }


def test_runtime_context_extracts_gateway_origin_identity_from_config():
    from agent_core.session_context import origin_identity_from_runtime

    runtime = SimpleNamespace(
        config={
            "configurable": {
                "source_type": "gateway",
                "platform": "slack",
                "chat_id": "C123",
                "thread_id": "T456",
                "session_id": "gateway-session-1",
                "display_name": "ops",
            }
        }
    )

    assert origin_identity_from_runtime(runtime) == {
        "source_type": "gateway",
        "platform": "slack",
        "chat_id": "C123",
        "thread_id": "T456",
        "session_id": "gateway-session-1",
        "display_name": "ops",
    }


def test_runtime_context_extracts_web_origin_identity_from_config():
    from agent_core.session_context import origin_identity_from_runtime

    runtime = SimpleNamespace(config={"configurable": {"source_type": "web", "session_id": "web-session-1"}})

    assert origin_identity_from_runtime(runtime) == {
        "source_type": "web",
        "session_id": "web-session-1",
    }
