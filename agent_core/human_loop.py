from typing import Any

from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt


class FlexibleHumanInTheLoopMiddleware(HumanInTheLoopMiddleware):
    """Human-in-the-loop middleware with lenient LangSmith resume handling."""

    @classmethod
    def _unwrap_response(cls, raw_response: Any) -> Any:
        if isinstance(raw_response, dict):
            for key in ("decisions", "type"):
                if key in raw_response:
                    return raw_response
            for key in ("value", "input", "response", "resume"):
                if key in raw_response:
                    return cls._unwrap_response(raw_response[key])
        return raw_response

    @classmethod
    def _normalize_response(cls, raw_response: Any, decision_count: int) -> list[dict[str, Any]]:
        raw_response = cls._unwrap_response(raw_response)
        if isinstance(raw_response, dict):
            if "decisions" in raw_response:
                decisions = raw_response["decisions"]
            elif "type" in raw_response:
                decisions = [raw_response]
            else:
                decisions = None
        elif isinstance(raw_response, list):
            decisions = raw_response
        else:
            value = str(raw_response).strip().strip('"').lower()
            if value in {"0", "approve", "approved", "yes", "y"}:
                decisions = [{"type": "approve"} for _ in range(decision_count)]
            elif value in {"1", "reject", "rejected", "no", "n"}:
                decisions = [
                    {"type": "reject", "message": "Rejected by human reviewer."}
                    for _ in range(decision_count)
                ]
            else:
                decisions = None

        if not isinstance(decisions, list):
            raise TypeError(
                "Human-in-the-loop resume value must be "
                '{"decisions": [{"type": "approve"}]}, [{"type": "approve"}], '
                '{"type": "approve"}, or a scalar shortcut such as "0"/"approve".'
            )
        return decisions

    @staticmethod
    def _resolve_interrupt_config(tool_call: ToolCall, interrupt_on: dict[str, Any]) -> Any:
        config = interrupt_on.get(tool_call["name"])
        if not isinstance(config, dict):
            return config

        commands = config.get("commands")
        if not commands:
            return config

        tool_args = tool_call.get("args") or {}
        if tool_args.get("command") not in commands:
            return None

        return {key: value for key, value in config.items() if key != "commands"}

    def after_model(self, state: dict[str, Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        messages = state["messages"]
        if not messages:
            return None

        last_ai_msg = next((msg for msg in reversed(messages) if isinstance(msg, AIMessage)), None)
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        action_requests = []
        review_configs = []
        interrupt_indices: list[int] = []
        interrupt_configs: dict[int, Any] = {}

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if (config := self._resolve_interrupt_config(tool_call, self.interrupt_on)) is not None:
                action_request, review_config = self._create_action_and_config(
                    tool_call, config, state, runtime
                )
                action_requests.append(action_request)
                review_configs.append(review_config)
                interrupt_indices.append(idx)
                interrupt_configs[idx] = config

        if not action_requests:
            return None

        raw_response = interrupt(
            {
                "action_requests": action_requests,
                "review_configs": review_configs,
            }
        )
        decisions = self._normalize_response(raw_response, len(interrupt_indices))

        if (decisions_len := len(decisions)) != (interrupt_count := len(interrupt_indices)):
            raise ValueError(
                f"Number of human decisions ({decisions_len}) does not match "
                f"number of hanging tool calls ({interrupt_count})."
            )

        revised_tool_calls: list[ToolCall] = []
        artificial_tool_messages: list[ToolMessage] = []
        decision_idx = 0

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if idx in interrupt_indices:
                config = interrupt_configs[idx]
                decision = decisions[decision_idx]
                decision_idx += 1
                revised_tool_call, tool_message = self._process_decision(
                    decision, tool_call, config
                )
                if revised_tool_call is not None:
                    revised_tool_calls.append(revised_tool_call)
                if tool_message:
                    artificial_tool_messages.append(tool_message)
            else:
                revised_tool_calls.append(tool_call)

        last_ai_msg.tool_calls = revised_tool_calls
        return {"messages": [last_ai_msg, *artificial_tool_messages]}

    async def aafter_model(
        self, state: dict[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
