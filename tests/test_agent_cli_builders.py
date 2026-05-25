def _patch_build_agent_side_effects(monkeypatch, builders):
    monkeypatch.setattr(
        builders,
        "create_agent",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(
        builders.memory_store,
        "format_for_system_prompt",
        lambda name: "",
    )
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: 0)
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)


def test_build_agent_forwards_checkpointer(monkeypatch):
    import agent_core.builders as builders

    _patch_build_agent_side_effects(monkeypatch, builders)

    result = builders.build_agent(checkpointer="checkpoint")

    assert result["checkpointer"] == "checkpoint"
