from pathlib import Path


def test_parent_prompt_guides_shell_use_to_terminal_and_process():
    from agent_core.system_prompt import SystemPromptBuilder, SystemPromptContext

    context = SystemPromptContext(
        workdir=Path("/workspace"),
        model_name="test-model",
        current_datetime="2026-05-18T00:00:00+08:00",
        platform="test-platform",
    )

    prompt = SystemPromptBuilder().build_parent(context)

    assert "terminal(background=False)" in prompt
    assert "terminal(background=True)" in prompt
    assert "process(action='poll')" in prompt
    assert "process(action='log')" in prompt
    assert "process(action='wait')" in prompt
    assert "pty=True" in prompt
    assert "execute_command" not in prompt
