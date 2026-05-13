---
name: implementation-plan
title: Implementation Plan
description: Use for concrete implementation plans that cover changes, data flow, constraints, risks, and verification.
---

# Implementation Plan Skill

## Purpose

Use this skill when the task is to design a concrete implementation rather than just explain an idea. The output should help someone make code changes with minimal ambiguity.

## Planning Rules

- Start from the current code structure, not an idealized redesign.
- Prefer the smallest change set that satisfies the requirement.
- Call out affected files, main functions, and interfaces.
- Separate required changes from optional improvements.
- Surface tradeoffs if there is more than one viable path.

## Recommended Structure

Organize the plan in this order when relevant:

1. Current-state summary
2. Target behavior
3. Key design decisions
4. Exact change points
5. Risks and compatibility concerns
6. Validation and testing plan

## Exact Change Points

For each important change point, explain:

- which file or module changes
- what responsibility changes there
- what data or control flow is affected
- whether the change is additive, behavioral, or refactoring

## Constraints

- Avoid proposing broad abstractions unless the problem actually needs them.
- Avoid hidden dependencies; mention required config, environment variables, or directory conventions.
- If the design depends on a new file layout or schema, describe it explicitly.

## Output Style

- Be concrete.
- Prefer actionable statements over generic architecture language.
- If something is uncertain, mark it as an assumption.
- If implementation can fail in specific ways, name those risks directly.
