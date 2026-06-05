from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from agent_core.session_context import origin_identity_from_runtime
from agent_tools.shared.tool_result import tool_failure, tool_success


MAX_CHOICES = 4


class ClarifyInput(BaseModel):
    question: str = Field(description="Question to ask the user for clarification.")
    choices: list[str] | None = Field(
        default=None,
        max_length=MAX_CHOICES,
        description=(
            "Optional list of 1 to 4 suggested answers. The UI may add an "
            "Other option that lets the user type a custom answer."
        ),
    )


def _clean_question(question: str) -> str:
    return str(question or "").strip()


def _clean_choices(choices: list[str] | None) -> list[str] | None:
    if choices is None:
        return None
    cleaned = [str(choice).strip() for choice in choices if str(choice).strip()]
    return cleaned or None


def _interactive_source(runtime: ToolRuntime | None) -> str | None:
    identity = origin_identity_from_runtime(runtime)
    if identity is None:
        return "cli"
    source_type = identity.get("source_type")
    if source_type in {"cli", "gateway", "web"}:
        return source_type
    return None


@tool("clarify", args_schema=ClarifyInput)
def clarify(
    question: str,
    choices: list[str] | None = None,
    *,
    runtime: ToolRuntime,
) -> ToolMessage:
    """Ask the user a clarification question before proceeding.

    Use this when the task is ambiguous, when the agent needs the user's
    preference, or when there are meaningful technical trade-offs. Do not use
    this for dangerous terminal command yes/no approval; terminal policy tools
    handle command approval separately.
    """
    cleaned_question = _clean_question(question)
    if not cleaned_question:
        return tool_failure(
            "clarify",
            "question is required.",
            code="invalid_input",
            runtime=runtime,
        )

    cleaned_choices = _clean_choices(choices)
    if cleaned_choices is not None and len(cleaned_choices) > MAX_CHOICES:
        return tool_failure(
            "clarify",
            f"choices supports at most {MAX_CHOICES} non-empty items.",
            code="invalid_input",
            data={"max_choices": MAX_CHOICES},
            runtime=runtime,
        )

    source_type = _interactive_source(runtime)
    if source_type is None:
        return tool_failure(
            "clarify",
            "clarify is unavailable in this non-interactive execution context.",
            code="interactive_unavailable",
            runtime=runtime,
        )

    return tool_success(
        "clarify",
        message="Clarification requested.",
        data={
            "question": cleaned_question,
            "choices": cleaned_choices,
            "source_type": source_type,
        },
        runtime=runtime,
        content="Clarification requested.",
    )
