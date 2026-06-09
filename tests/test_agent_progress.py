from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_observer_round_trips_through_config_and_runtime():
    from agent_core.progress import (
        ModelStartEvent,
        emit_progress,
        get_progress_observer_from_config,
        get_progress_observer_from_runtime,
        set_progress_observer,
    )

    events = []
    config = {"configurable": {"thread_id": "thread-1"}}
    observer = SimpleNamespace(emit=lambda event: events.append(event))

    updated = set_progress_observer(config, observer)

    assert updated is not config
    assert updated["configurable"]["thread_id"] == "thread-1"
    assert get_progress_observer_from_config(updated) is observer

    runtime = SimpleNamespace(config=updated)
    assert get_progress_observer_from_runtime(runtime) is observer

    emit_progress(observer, ModelStartEvent(thread_id="thread-1"))
    assert events == [ModelStartEvent(thread_id="thread-1")]


def test_emit_progress_swallows_observer_errors(caplog):
    import logging

    from agent_core.progress import ModelStartEvent, emit_progress

    class BrokenObserver:
        def emit(self, event):
            raise RuntimeError("render broke")

    with caplog.at_level(logging.DEBUG):
        emit_progress(BrokenObserver(), ModelStartEvent(thread_id="thread-1"))

    assert "Progress observer failed" in caplog.text


def test_mark_streamed_result_sets_private_marker_without_mutating_original():
    from agent_core.progress import has_streamed_output, mark_streamed_output

    result = {"messages": [{"role": "assistant", "content": "hello"}]}

    marked = mark_streamed_output(result)

    assert marked is not result
    assert marked["messages"] == result["messages"]
    assert has_streamed_output(marked) is True
    assert has_streamed_output(result) is False


def test_mark_streamed_result_handles_non_dict_values():
    from agent_core.progress import has_streamed_output, mark_streamed_output

    result = mark_streamed_output("hello")

    assert result == {"final_response": "hello", "__agent_streamed_output__": True}
    assert has_streamed_output(result) is True


def test_tool_event_args_are_copied_and_immutable():
    from agent_core.progress import ToolCompleteEvent, ToolErrorEvent, ToolStartEvent

    args = {
        "command": "rg progress",
        "options": {"limit": 10},
        "paths": ["agent_core", "tests"],
        "flags": ("-n", {"context": 2}),
        "tags": {"read", "search"},
    }

    events = [
        ToolStartEvent("terminal", args, "call-1"),
        ToolCompleteEvent("terminal", args, "done", 12, "call-1"),
        ToolErrorEvent("terminal", args, "failed", 13, "render broke", "call-1"),
    ]

    args["command"] = "mutated"
    args["options"]["limit"] = 99
    args["paths"].append("mutated")
    args["flags"][1]["context"] = 99
    args["tags"].add("mutated")

    for event in events:
        assert event.args["command"] == "rg progress"
        assert event.args["options"]["limit"] == 10
        assert event.args["paths"] == ("agent_core", "tests")
        assert event.args["flags"] == ("-n", {"context": 2})
        assert event.args["tags"] == frozenset({"read", "search"})

        with pytest.raises(TypeError):
            event.args["command"] = "mutated again"
        with pytest.raises(TypeError):
            event.args["options"]["limit"] = 100
        with pytest.raises(AttributeError):
            event.args["paths"].append("observer mutation")
        with pytest.raises(TypeError):
            event.args["flags"][1]["context"] = 100
        with pytest.raises(AttributeError):
            event.args["tags"].add("observer mutation")
