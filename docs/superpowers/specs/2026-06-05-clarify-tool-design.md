# Clarify Tool Design

Date: 2026-06-05

## Goal

Port the Herme-style `clarify` capability into this LangChain/LangGraph project as a native interactive clarification tool.

The tool lets the agent ask the user for input when a task is ambiguous, when there are meaningful technical trade-offs, or when user preference is needed before a high-impact decision. It must not replace terminal approval for dangerous commands, and it must not block unattended cron jobs.

## Decisions

- Use LangGraph interrupt as the native interaction mechanism.
- Cover foreground CLI and gateway/IM conversations in the first implementation.
- In gateway/IM, if a session has a pending `clarify`, the next inbound message in that same session is treated as the clarification answer.
- Keep cron jobs unattended: do not expose `clarify` to cron agents, and fail closed if it is somehow invoked in a non-interactive context.

## Public Tool Shape

Add `agent_tools/public/clarify.py` with a LangChain tool facade matching the existing public tool pattern.

Input schema:

- `question: str`: required, non-empty after trimming.
- `choices: list[str] | None`: optional. When provided, cleaned of empty strings and limited to at most 4 choices.

The tool description should tell the model to use `clarify` only for meaningful ambiguity, trade-offs, or user preferences. It should explicitly say not to use the tool for dangerous terminal command yes/no approval because the terminal policy and approval tools already handle that.

The public import surface should export `clarify` from `agent_tools.public`. The existing pasted `agent_tools/clarify_tool.py` should be replaced or converted into a compatibility shim during implementation; it should not keep the Herme `tools.registry` dependency.

## Agent Registration

Add `clarify` to the parent agent's interactive tool set:

- Include it in `agent_core.delegation.BASE_TOOLS`.
- Do not include it in `READ_ONLY_TOOLS`, so read-only subagents cannot ask the user.
- Keep it out of `cron.runner.build_cron_tools`.

The parent builder should include `clarify` by default alongside the existing base tools. Cron-specific tool building remains explicit and excludes interactive tools.

## Interrupt Semantics

Use the existing `FlexibleHumanInTheLoopMiddleware` rather than adding a second interaction channel.

Add `clarify` to `HUMAN_INTERRUPT_ON` with clarify-specific review metadata. The preferred resume decision is:

```json
{"type": "respond", "message": "<user answer>"}
```

For multiple interrupted tool calls, the existing `{"decisions": [...]}` envelope remains the outer resume shape.

The interrupt payload should carry enough structured data for renderers to distinguish clarification from approval:

- tool name: `clarify`
- args: `question`, optional `choices`
- review metadata: `kind: "clarify"` or equivalent, plus a human-readable description

This keeps policy review and clarification on the same checkpoint/resume mechanism while allowing CLI and gateway to render them differently.

## CLI Behavior

Extend the existing interrupt decision collection flow rather than creating a second prompt loop.

When a pending request is for `clarify`:

- Show the question directly.
- If choices exist, render numbered choices.
- Add an implicit final option: `Other (type your answer)`.
- If the user enters a valid choice number, map it to that choice.
- If the user enters any other non-empty text, use that text as the answer.
- If choices are absent, read a free-form answer.

Normal approval requests keep the current `y/n/e/r/a/q` flow. Terminal approval remains separate and is not routed through clarify.

## Gateway/IM Behavior

Gateway dispatch owns session-level routing. Platform adapters should stay focused on parsing inbound events and sending outbound text.

When dispatching an inbound event:

1. Resolve the `GatewaySession` as today.
2. If the session has a pending `clarify`, treat the inbound text as the answer.
3. Resume the agent with `Command(resume={"decisions": [{"type": "respond", "message": answer}]})`.
4. Send the resumed final response through the platform adapter.
5. Clear the pending clarify state after successful resume.

When a normal agent turn returns a `clarify` interrupt:

1. Format and send the question to the same platform target.
2. Store pending clarify state keyed by `session_id`.
3. Complete the inbound event without sending the empty-response fallback.
4. Do not start a new agent turn until the next message answers the pending clarify.

Suggested IM multiple-choice rendering:

```text
<question>

1. <choice A>
2. <choice B>
3. <choice C>
4. <choice D>
5. Other (type your answer)
```

If the next inbound text is `1` through the number of displayed choices, map it to that choice. Otherwise use the raw text as the answer.

## Gateway State

Extend `GatewaySessionStore` with persistent pending-interrupt state so a gateway restart does not lose a waiting clarification.

Minimum stored fields:

- `session_id`
- `kind`, initially `clarify`
- serialized interrupt payload or request args
- `created_at`
- `updated_at`

Only one pending clarify per session is supported in the first implementation. A new inbound message answers the pending clarify before any new normal request is considered.

## Cron Safety

Cron jobs are unattended and must not block waiting for a human.

Maintain the existing cron prompt instruction: "Do not ask clarifying questions. Make a best effort with available context."

Implementation requirements:

- `build_cron_tools()` does not include `clarify`.
- Tests assert `clarify` is absent from cron tool names.
- If `clarify` is invoked without an interactive source, it returns a structured failure rather than waiting forever.

## Error Handling

Validation errors return structured tool failures:

- missing question
- empty question
- non-list `choices`
- more than 4 non-empty choices, if implementation chooses strict validation

For gateway resume failures:

- Keep pending clarify state if resume fails before a response is sent.
- Mark the inbound event retryable through existing dispatch failure behavior.
- Clear pending state only after the resumed response is sent and recorded.

For unsupported gateway platforms:

- Do not clear pending clarify if the outbound send fails.
- Return a failed dispatch result so the inbox retry path can handle it.

## Tests

Add focused tests for:

- `agent_tools.public.clarify` schema, validation, and ToolMessage result.
- `agent_tools.public.__init__` exports `clarify`.
- parent agent tool list includes `clarify`.
- read-only subagent tools exclude `clarify`.
- cron tools exclude `clarify`.
- HITL middleware emits an interrupt for `clarify` and accepts `respond` resume.
- CLI maps numbered choices, other text, and open-ended input to `respond` decisions.
- gateway sends a clarify question and records pending state without fallback text.
- gateway treats the next inbound message as the clarify answer, resumes the agent, sends the final response, and clears pending state.
- gateway preserves pending state on send or resume failure.

## Out of Scope

- Rich TUI or Ink visual rendering.
- Multi-question wizard flows.
- More than one pending clarify per gateway session.
- Using `clarify` for terminal command approval.
- Cron or background task interactive clarification.
