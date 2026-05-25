import uuid
from typing import Any

from langchain.agents.middleware.human_in_the_loop import HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage, ToolCall, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from agent_core.permissions import tool_policy
from agent_core.permissions.approvals import ApprovalRecord, make_args_digest, record_approval
from agent_core.permissions.audit import audit_policy_event
from agent_core.permissions.profiles import resolve_runtime_profile
from agent_core.policy_tool_middleware import POLICY_ARG_BUILDERS
from agent_core.session_context import hermes_task_id_from_runtime


class FlexibleHumanInTheLoopMiddleware(HumanInTheLoopMiddleware):
    """Human-in-the-loop middleware with lenient LangSmith resume handling.

    This middleware is a thin adapter around LangChain's official HumanInTheLoopMiddleware.
    It adds:
    - lenient LangSmith resume value parsing
    - dynamic selection of policy-controlled tool calls that need human review
    - approval and audit recording after a human approves a policy review

    Policy enforcement (allow/deny/review) at execution time is handled by PolicyToolMiddleware.
    """

    def __init__(
        self,
        *args: Any,
        policy_tools: set[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.policy_tools = policy_tools or set()

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

    @staticmethod
    def _preview_for_tool_call(tool_call: ToolCall) -> str:
        tool_args = tool_call.get("args") or {}
        for key in ("command", "path"):
            if value := tool_args.get(key):
                return str(value)
        return ""

    @staticmethod
    def _policy_args_for_tool_call(tool_call: ToolCall) -> dict[str, Any]:
        args = tool_call.get("args") or {}
        builder = POLICY_ARG_BUILDERS.get(tool_call["name"])
        if builder is None:
            return tool_policy.canonical_tool_args(tool_call["name"], args)
        return builder(args)

    @classmethod
    def _policy_decision_for_tool_call(cls, tool_call: ToolCall, runtime: Runtime[Any]) -> Any:
        task_id = hermes_task_id_from_runtime(runtime)
        return tool_policy.evaluate_tool_call(
            tool_name=tool_call["name"],
            args=cls._policy_args_for_tool_call(tool_call),
            task_id=task_id,
            tool_call_id=tool_call.get("id"),
        )

    @staticmethod
    def _review_config_for_policy_decision(decision: Any) -> dict[str, Any]:
        return {
            "allowed_decisions": ["approve", "edit", "reject", "respond"],
            "description": decision.human_message,
        }

    def _record_policy_approval(
        self,
        *,
        task_id: str,
        tool_call: ToolCall,
        policy_decision: Any,
    ) -> None:
        approval_id = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())
        record_approval(
            ApprovalRecord(
                approval_id=approval_id,
                decision_id=decision_id,
                task_id=task_id,
                tool_call_id=tool_call["id"],
                tool_name=tool_call["name"],
                args_digest=make_args_digest(self._policy_args_for_tool_call(tool_call)),
                risk_tags=policy_decision.risk_tags,
                allow_network_once=policy_decision.requires_network,
            )
        )
        audit_policy_event(
            profile=resolve_runtime_profile(),
            tool_name=tool_call["name"],
            task_id=task_id,
            decision=policy_decision,
            preview=self._preview_for_tool_call(tool_call),
            approved_by_human=True,
            network_once=policy_decision.requires_network,
            extra={"approval_id": approval_id, "decision_id": decision_id},
        )

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
        policy_decisions: dict[int, Any] = {}
        task_id = hermes_task_id_from_runtime(runtime)

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if tool_call["name"] in self.policy_tools:
                policy_decision = self._policy_decision_for_tool_call(tool_call, runtime)
                audit_policy_event(
                    profile=resolve_runtime_profile(),
                    tool_name=tool_call["name"],
                    task_id=task_id,
                    decision=policy_decision,
                    preview=self._preview_for_tool_call(tool_call),
                )

                if policy_decision.outcome == "review":
                    policy_decisions[idx] = policy_decision
                    config = self._review_config_for_policy_decision(policy_decision)
                else:
                    continue
            else:
                config = self._resolve_interrupt_config(tool_call, self.interrupt_on)
                if config is None:
                    continue

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
                    if idx in policy_decisions and decision["type"] == "approve":
                        self._record_policy_approval(
                            task_id=task_id,
                            tool_call=revised_tool_call,
                            policy_decision=policy_decisions[idx],
                        )
                if tool_message:
                    artificial_tool_messages.append(tool_message)
            else:
                revised_tool_calls.append(tool_call)

        last_ai_msg.tool_calls = revised_tool_calls
        if artificial_tool_messages:
            return {"messages": [last_ai_msg, *artificial_tool_messages]}
        if interrupt_indices:
            return {"messages": [last_ai_msg]}
        return None

    async def aafter_model(
        self, state: dict[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        return self.after_model(state, runtime)
