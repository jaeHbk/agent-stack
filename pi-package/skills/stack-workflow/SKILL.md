---
name: stack-workflow
description: Apply the local Hermes-Bonsai-Pi operating rules when implementing, delegating, or reviewing code in the portable agent stack.
---

# Stack Workflow

Pi owns code changes and validation. Hermes coordinates across tasks and keeps
durable memory. Bonsai is the economical local worker; Sonnet and Opus are
escalation tiers.

## Execution Rules

1. Read repository instructions and inspect existing patterns before editing.
2. Keep one writer in the active worktree.
3. Ask Hermes to coordinate read-only parallel research or independent review.
   Keep Pi as the only writer in the active worktree.
4. Give every delegated task a self-contained contract, relevant paths,
   constraints, and expected output. Do not pass the complete parent transcript.
5. Run deterministic tests before accepting a change.
6. Escalate after one failed Bonsai repair cycle.

## Model Routing

- Local Bonsai: mapping, summaries, logs, documentation, tests, and bounded
  deterministic edits.
- Sonnet: ordinary implementation and first repair attempts.
- Opus: architecture, security, concurrency, migrations, hard debugging, and
  independent final review.

Return a concise handoff with result, files changed, tests run, and remaining
risks.
