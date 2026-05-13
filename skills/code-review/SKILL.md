---
name: code-review
title: Code Review
description: Use for code review. Prioritize bugs, behavioral regressions, edge cases, and missing tests.
---

# Code Review Skill

## Purpose

Use this skill when the task is primarily review rather than implementation. The goal is to identify concrete defects, regressions, risky assumptions, and missing validation or tests.

## Default Priorities

Evaluate findings in this order:

1. Correctness bugs
2. Behavioral regressions
3. Edge-case handling
4. Missing validation or error handling
5. Missing or insufficient tests
6. Lower-priority maintainability issues

## Review Rules

- Prefer evidence over speculation.
- Reference concrete files, functions, branches, inputs, or behaviors.
- Do not spend most of the answer on style issues unless they create real risk.
- If there are no meaningful findings, say so explicitly instead of inventing weak concerns.
- Distinguish verified findings from plausible but unverified risks.

## Output Style

When reporting findings:

- Put findings first.
- Order findings by severity.
- Each finding should state:
  - what is wrong
  - why it matters
  - where it happens
  - what case triggers it

After findings, include:

- open questions or assumptions
- testing gaps
- a brief overall assessment

## Severity Guide

- High: likely bug, data loss, security issue, crash, or clear behavior break
- Medium: realistic edge-case failure or important missing validation
- Low: non-blocking but real risk, maintainability problem, or weak test coverage

## Non-Goals

- Do not rewrite the code unless the task explicitly asks for changes.
- Do not give generic best-practice lists without grounding them in the code under review.
