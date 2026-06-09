from __future__ import annotations

from types import SimpleNamespace


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

    assert result == {"final_response": "hello", "__agent_cli_streamed_output__": True}
    assert has_streamed_output(result) is True
