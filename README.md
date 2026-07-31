# Portable Agent Stack

[![onboarding](https://github.com/jaeHbk/agent-stack/actions/workflows/onboarding.yml/badge.svg)](https://github.com/jaeHbk/agent-stack/actions/workflows/onboarding.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **Beta:** source onboarding is validated on a 36 GiB Apple Silicon Mac. The
> 24 GiB minimum is enforced but has not been exercised across every supported
> hardware configuration. Review `./install --plan` before approving changes.

This is an independent project and is not affiliated with or endorsed by the
vendors and projects it integrates.

A reproducible macOS agent system with one owner for each responsibility:

```text
Nous Hermes                    coordination, memory, skills, schedules
  ├─ Ternary Bonsai-27B        private local inference (local/hybrid profiles)
  └─ Pi                        sole worktree-writing harness
       ├─ pi-local             bounded local work
       ├─ pi-sonnet            normal Bedrock implementation
       └─ pi-opus              hard work and independent review
```

The setup preserves Pi authentication and user settings, keeps Hermes Metal's
data and compatibility CLI, verifies downloaded bytes, binds local inference to
authenticated loopback only, and can recover an interrupted service cutover.
Oh My Pi and Firecrawl remain outside the baseline to avoid overlapping agents
and unnecessary external data flows.

## Profiles

| Profile | Default inference | Local runtime/model | Bedrock |
| --- | --- | --- | --- |
| `auto` | Selects the best supported profile | Preferred on a capable Apple Silicon Mac | Used when local is unsupported and AWS is ready |
| `local` | Bonsai | Required | Optional wrappers |
| `hybrid` | Bonsai | Required | Readiness reported; explicit wrappers |
| `cloud` | Bedrock | Not installed or started | Required |

Local and hybrid require macOS on Apple Silicon, 24 GiB RAM, and about 15 GiB
free for a first install. Cloud does not download Bonsai. All profiles require
Node.js 20+, Python 3.10+, and a user-writable npm prefix. AWS uses the normal
credential chain; credentials are never copied into this repository.

## One-Command Setup

Clone and inspect the read-only plan before installation:

```bash
git clone https://github.com/jaeHbk/agent-stack.git
cd agent-stack
./install --plan
./install
```

Setup reports every prerequisite together, shows the exact plan, asks before
mutation, resumes completed phases, runs a quick health check, and performs the
security audit. Useful variants:

```bash
./install --plan                         # read-only plan
./install --install-deps                 # explicitly run the pinned Brewfile
./install --configure-shell              # add ~/.local/bin to ~/.zprofile
./install --profile hybrid               # local default + Bedrock readiness
./install --profile cloud                # no local model/runtime/service
./install --repair                       # re-probe every phase and repair drift
./install --yes --non-interactive        # automation
./install --verify full                  # full live inference verification
```

Homebrew itself is never installed implicitly. If it is absent, setup prints the
exact remedy. Source runtime builds require the full Xcode Metal compiler. A
release can instead provide a SHA-256-pinned prebuilt artifact; see
[`docs/RELEASING.md`](docs/RELEASING.md).

## What Gets Installed

| Module | Purpose | Behavior |
| --- | --- | --- |
| Nous Hermes | Intake, plans, durable context, delegation | Focused tool set and `pi-coder` skill |
| Bonsai-27B | Private, zero-marginal-cost inference | One 65,536-token slot on `127.0.0.1:8080`, bearer auth, no UI |
| Pi | Inspection, edits, tests, review | Stock pinned client plus repository-owned workflow/task-contract package |
| Sonnet/Opus | Explicit quality escalation | Existing Amazon Bedrock account only when invoked |
| Hermes Metal | Optional legacy Obsidian retrieval | Data untouched; detected CLI retained as `hermes-metal`; services reversibly disabled |

The repository-owned Pi package supplies workflow guidance and a task-contract
prompt without copying third-party extension code. `fd` and `rg` are installed
as normal command-line tools. Hermes owns delegation and parallel coordination.
Existing Pi defaults, unrelated packages, sessions, and `auth.json` are
preserved.

## Use

Use Hermes for decomposition, durable memory, schedules, or multiple steps:

```bash
hermes
```

Use Pi directly when the coding task is already scoped:

```bash
cd /path/to/repository
pi-local       # maps, summaries, tests, docs, bounded deterministic changes
pi-sonnet      # normal multi-file implementation and first repair
pi-opus        # architecture, security, migrations, hard debugging, review
```

Pi remains the sole writer in an active worktree. Give delegated work an
objective, observable acceptance criteria, relevant paths, constraints, exact
test commands, and expected report. Escalate after one failed local repair
cycle rather than repeatedly prompting Bonsai.

## Operate and Recover

```bash
agent-stack status
agent-stack doctor --quick
agent-stack doctor                       # hashes model and runs live turns
agent-stack security-audit
agent-stack start | stop | restart
agent-stack logs
agent-stack diagnostics                  # mode-0600 redacted support bundle
agent-stack setup --repair
```

A failed phase leaves a named error, private redacted log, diagnostics path, and
an atomic checkpoint. Rerun the same setup command to resume. Service activation
snapshots Bonsai and Hermes Metal first; if authenticated Bonsai health fails,
the prior loaded services and plists are restored automatically.

## Portability

The Git repository reproduces software and wiring. User state moves separately:

```bash
# Source Mac
agent-stack export ~/agent-stack-user-data.tar.gz

# Destination Mac
./install
agent-stack restore ~/agent-stack-user-data.tar.gz
agent-stack doctor
```

Normal exports omit Pi auth, Hermes `.env`, local tokens, runtimes, models, and
logs. `--include-secrets` creates a plaintext mode-0600 archive and should only
be used through encrypted storage/transport.

A prebuilt runtime artifact removes Xcode from destination requirements while
remaining checksum-verified. Until a separately verified runtime release is
published, setup falls back to the immutable source pin. Source releases do not
contain model weights or generated runtime binaries.

## Development Validation

```bash
make test                    # shell/static and all Python unit tests
make onboarding-test         # isolated clean-machine scenarios
make integration-test        # full doctor against the installed stack
make security-audit          # live fail-closed OSV audit
make secret-scan             # full-history Gitleaks scan
make release-check           # source-release test gate
make hermes-regression-test  # disposable vendored-patch regression suite
make runtime-package         # deterministic local runtime archive
```

See [`docs/ONBOARDING.md`](docs/ONBOARDING.md),
[`docs/OPERATIONS.md`](docs/OPERATIONS.md),
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and
[`docs/SECURITY.md`](docs/SECURITY.md).

## License and Security

Agent Stack is licensed under the [MIT License](LICENSE). Downloaded components
retain their upstream licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
Report vulnerabilities privately according to [SECURITY.md](SECURITY.md).
