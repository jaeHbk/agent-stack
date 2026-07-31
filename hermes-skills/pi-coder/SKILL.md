---
name: pi-coder
description: Delegate scoped code implementation or review work to the configured Pi executor while keeping Hermes as coordinator. Use for repository changes, tests, debugging, and independent code review.
---

# Pi Coder

Hermes owns task intake, decomposition, durable memory, and final reporting.
Pi is the only agent allowed to modify an active worktree.

## Select A Tier

Use the lowest tier that can reliably complete the task:

| Command | Use |
| --- | --- |
| `pi-local` | Read-only analysis, repository maps, logs, docs, tests, and small deterministic edits |
| `pi-sonnet` | Normal multi-file implementation and the first repair attempt |
| `pi-opus` | Architecture, security, concurrency, migrations, hard debugging, and final review |

Escalate after one failed local repair cycle. Do not repeatedly reprompt Bonsai
for a long-horizon repair.

## Write A Task Contract

Every delegated prompt must include:

- Objective and observable acceptance criteria
- Relevant repository and working directory
- Files or ownership boundaries in scope
- Constraints and behavior that must not change
- Exact validation commands
- Required result format

Pass only the contract and relevant paths. Do not forward the full Hermes
conversation or unrelated memory.

## Enforce One Writer

- Run one writing Pi session at a time in an active worktree.
- Hermes may parallelize read-only research; Pi remains the sole worktree writer.
- A second writer requires a separate worktree and non-overlapping ownership.
- Never let Hermes and Pi edit the same files concurrently.

## Invoke Pi

Run the selected command from the repository root. For unattended work, use
`--no-session -p` and provide the complete task contract as the prompt. For
interactive work, start the command without `-p` and let the user drive it.

Pi must report:

```text
Result
Files changed
Tests run
Remaining risks
```

Verify the reported diff and test results before recording a durable outcome.
Persist stable commands and conventions, not full coding transcripts.
