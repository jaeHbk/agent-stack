# Agent Stack Development Guide

## Scope

This repository manages a user-scoped local agent stack. Keep installers
idempotent, preserve pre-existing configuration, and never store credentials,
model weights, generated plists, or runtime logs in Git.

## Constraints

- Support macOS on Apple Silicon with the system Bash 3.2.
- Bind local inference to `127.0.0.1` only.
- Pin every downloaded source and verify model weights by SHA-256.
- Back up a file before mutating it and make service changes reversible.
- Keep Hermes Metal data intact. Only disable or restore its LaunchAgents.
- Pi is the sole code writer. Parallel agents remain read-only unless a user
  explicitly assigns isolated worktrees and non-overlapping ownership.

## Testing

- Unit and static tests: `make test`
- Live integration tests after installation: `make integration-test`
- Full local health check: `agent-stack doctor`
