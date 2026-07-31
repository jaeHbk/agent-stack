# Architecture

## Responsibility Model

| Module | Owns | Explicitly does not own |
| --- | --- | --- |
| Nous Hermes | Intake, decomposition, memory, skills, schedules, delegation | Concurrent active-worktree authorship |
| Ternary Bonsai-27B | Private/economical reasoning | Final authority on long-horizon changes |
| Pi | Repository edits, deterministic tests, implementation/review | Persistent cross-project coordination |
| Sonnet/Opus | Explicit Bedrock escalation | Silent default/background export |
| Hermes Metal | Preserved legacy Obsidian retrieval | Active orchestration/inference |

The parent Pi session is the sole writer. Parallel writers require separate
worktrees and non-overlapping ownership.

## Onboarding Modules

The onboarding design uses three deep modules behind narrow interfaces:

- `scripts/onboard.py`: `setup`, `preflight`, and `diagnostics`; owns profiles,
  planning, locking, checkpoints, timeouts, redaction, and completion output.
- `scripts/runtime_artifact.py`: `package`, `verify`, and `extract`; owns
  deterministic archives, manifests, checksum/signature policy, safe extraction,
  and atomic runtime replacement.
- `scripts/service_cutover.py`: `activate`, `deactivate`, and `recover`; owns
  launchd snapshots, authenticated health gating, and rollback.

`scripts/bootstrap.sh` is a phase adapter around existing install functions.
`bin/agent-stack` and root `install` are thin entry points. Tests cross the same
module interfaces using local adapters rather than reaching through them.

## Request and Inference Flows

```text
coordinated: User → Hermes → task contract → Pi → repository/tests
focused:     User → pi-local|pi-sonnet|pi-opus → repository/tests
local:       Hermes/Pi → authenticated 127.0.0.1:8080 → Bonsai
cloud:       Hermes/Pi → standard AWS credential chain → Bedrock
```

The local server advertises `bonsai-27b`, has one 65,536-token slot, uses Metal
offload, Q4_0 KV, Jinja tool calling, and no web UI. One slot avoids silently
splitting memory/context between concurrent clients.

## Pi Customization Seam

The repository-owned `pi-package/` contributes workflow guidance and a
self-contained task-contract prompt. `fd` and `rg` remain ordinary pinned
command-line dependencies. Hermes owns delegation, so the baseline does not
copy or activate an overlapping third-party Pi orchestration package. Existing
providers, defaults, auth, and unrelated packages are merged rather than
replaced.

## State and Data Ownership

```text
checkout/                                  portable control repository
~/.local/share/agent-stack/
  install-state.json                       schema/version/phase checkpoints
  setup.lock                               concurrent-run guard
  runtime/llama.cpp/                       source or verified prebuilt runtime
  models/                                  verified GGUF
  secrets/bonsai-api-key                   mode 0600 local token
  transactions/                            recoverable cutover state/receipt
  logs/                                    redacted setup and server logs
  diagnostics/                             redacted support bundles
  backups/                                 pre-mutation configuration copies
  disabled-launch-agents/hermes-metal/     preserved legacy service definitions
~/.hermes/                                 Nous Hermes code and user state
~/.pi/agent/                               Pi auth, packages, settings, sessions
~/Documents/hermes-metal/                  untouched legacy implementation/data
```

Only the checkout belongs in Git. User state, credentials, logs, generated
services, runtime binaries, and model bytes remain machine-local.

## Reconciliation Invariants

- immutable source/version pins and downloaded-file checksums;
- local listener is authenticated and loopback-only;
- configuration writes are atomic and preserve unrelated values;
- a backup exists before a planned mutation;
- checkpoints are trusted only when their fingerprint matches;
- healthy artifacts are adopted rather than rebuilt;
- Hermes Metal is disabled only after replacement health succeeds;
- cloud profile stops local inference but preserves all bytes; and
- uninstall removes wiring, not user/runtime data.
