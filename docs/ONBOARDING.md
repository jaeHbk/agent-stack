# Onboarding Contract

## Goal

`agent-stack setup` is the only user-facing installation interface. `./install`
handles the special case where Python is absent; Make targets and
`scripts/bootstrap.sh` are developer/phase adapters, not separate procedures.

## First Run

```bash
./install --plan
./install
```

The read-only plan does not create the data directory or lock file. Setup then:

1. acquires a user-scoped exclusive lock;
2. reports all preflight results rather than stopping at the first failure;
3. resolves `auto`, `local`, `hybrid`, or `cloud`;
4. displays `RUN`, `ADOPT`, and `SKIP` decisions;
5. asks once before mutation when attached to a terminal;
6. records each completed phase atomically;
7. runs quick doctor and the security audit; and
8. prints the exact commands to start working.

Use `--yes --non-interactive` in automation. Use `--json` for line-delimited
preflight, plan, and completion objects.

## Preflight

Preflight checks, in one pass:

- macOS/Apple Silicon compatibility for local inference;
- base commands, Python 3.10+, and Node.js 20+;
- RAM, disk, and runtime acquisition path;
- GUI launchd availability;
- ownership of port 8080;
- `~/.local/bin` visibility;
- write access to stack, Pi, Hermes, and npm locations;
- Pi JSON validity;
- AWS credential-chain shape for hybrid/cloud; and
- a verified prebuilt URL/SHA or complete source/Metal toolchain.

Every failure contains a concrete remedy. `--install-deps` explicitly runs the
repository Brewfile. It does not install Homebrew and never uses `sudo`.
`--configure-shell` is the only option allowed to edit `.zprofile`; it creates a
backup and is idempotent.

## Profiles

- `auto`: local when hardware and runtime acquisition support it; otherwise
  cloud only if AWS readiness is detected.
- `local`: local Bonsai default; cloud wrappers remain explicit.
- `hybrid`: same local default plus Bedrock readiness reporting.
- `cloud`: Hermes uses native `bedrock`, Pi removes only the stack-owned Bonsai
  provider, and local/legacy inference services are stopped while data remains.

AWS authentication stays in the standard SDK chain (`AWS_PROFILE`, SSO,
credential files, web identity, container/instance roles). Setup never reads a
credential value into its state or logs.

## Phases and Resume

```text
backup → pi → hermes → runtime → model → cutover → links → verify → audit
```

State is schema-versioned at
`~/.local/share/agent-stack/install-state.json`. Each phase stores a fingerprint
of the pins/profile that affect it. A healthy installation without new state is
adopted. Matching checkpoints are skipped. `--repair` ignores checkpoint trust,
re-probes every phase, and still adopts healthy artifacts rather than rebuilding
them.

The backup phase runs once only when a later phase will mutate state. Downloads
are resumable. Writes to state/configuration are atomic. A setup lock rejects
concurrent runs.

## Runtime Acquisition

Order for `--runtime-source auto`:

1. adopt an existing runtime matching `LLAMA_COMMIT`;
2. download a configured prebuilt archive only when both URL and SHA-256 exist;
3. otherwise build the immutable PrismML source pin with Metal.

Override release values without editing the checkout:

```bash
export AGENT_STACK_LLAMA_PREBUILT_URL=https://example.invalid/runtime.tar.gz
export AGENT_STACK_LLAMA_PREBUILT_SHA256=<64-hex-digest>
./install --runtime-source prebuilt
```

Artifacts contain a normalized manifest and per-file checksums. Extraction
rejects absolute paths, traversal, hardlinks, devices, and unsafe symlinks.
`require-codesign` can be enabled after a publishing identity exists.

## Failure and Diagnostics

Every phase has a hard timeout. Output is streamed while a mode-0600 copy is
written under `~/.local/share/agent-stack/logs`; known tokens, authorization
headers, AWS access-key identifiers, and secret assignments are redacted.

On failure, setup automatically creates a mode-0600 diagnostics archive. Create
one manually with:

```bash
agent-stack diagnostics [OUTPUT]
```

The archive includes pins, platform metadata, state, status, and the setup log.
It excludes configuration bodies, credentials, model bytes, memory, sessions,
and environment variables.

## Transactional Cutover

Before touching launchd, cutover snapshots:

- whether Bonsai and each Hermes Metal job were loaded;
- the existing Bonsai plist; and
- every active Hermes Metal plist.

It then stops old services, stages the authenticated loopback Bonsai plist,
starts Bonsai, and waits for authenticated `/v1/models`. Only after health
passes are Hermes Metal plists moved to disabled storage. Any error, timeout, or
handled signal restores the snapshot. A transaction left by process death is
rolled back before the next cutover.
