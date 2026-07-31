# Operations

## Install and Reconcile

```bash
./install --plan
./install
```

Use `./install --install-deps` only when you explicitly want `brew bundle`.
Homebrew is never installed for you. Use `--configure-shell` when preflight says
`~/.local/bin` is absent from PATH. `--repair` re-probes every phase; it does not
force a healthy model download, runtime build, or backup.

State and logs:

| Item | Location |
| --- | --- |
| Install/checkpoint state | `~/.local/share/agent-stack/install-state.json` |
| Setup lock | `~/.local/share/agent-stack/setup.lock` |
| Setup/server logs | `~/.local/share/agent-stack/logs/` |
| Diagnostics | `~/.local/share/agent-stack/diagnostics/` |
| Backups | `~/.local/share/agent-stack/backups/` |
| Cutover recovery | `~/.local/share/agent-stack/transactions/` |
| Runtime/model | `~/.local/share/agent-stack/runtime/`, `models/` |

A failed phase can be resumed by rerunning the identical command. Do not delete
an active transaction directory; setup consumes it to restore the prior service
state.

## Profiles

```bash
agent-stack setup --profile local
agent-stack setup --profile hybrid
agent-stack setup --profile cloud
```

Moving to cloud stops Bonsai and Hermes Metal LaunchAgents but preserves their
runtime/model/data. Moving back to local reuses verified bytes. Hybrid keeps
Bonsai as default and reports whether explicit Bedrock wrappers are ready.

## Daily Workflow

Use `hermes` for decomposition, durable memory, schedules, or delegation. Use
Pi for an already-scoped repository task:

```bash
pi-local
pi-sonnet
pi-opus
```

Escalate after one failed local repair cycle. Keep one writer per worktree.
Record stable outcomes in Hermes memory, not full coding transcripts.

## Health and Service Control

```bash
agent-stack status
agent-stack doctor --quick
agent-stack doctor
agent-stack security-audit
agent-stack start
agent-stack stop
agent-stack restart
agent-stack logs
```

Quick doctor validates pins, patches, dependency compatibility, configuration,
permissions, launchd, auth, and network binding. Full local doctor also hashes
the model and runs direct, tool-call, Pi, and Hermes turns. Full cloud doctor
runs minimal Pi/Hermes Bedrock turns. External operations have hard timeouts.

`start` is meaningful only for local/hybrid. Use `setup --profile ...` when
changing profiles so configuration and state remain coherent.

## Diagnostics

```bash
agent-stack diagnostics
agent-stack diagnostics ~/Desktop/agent-stack-diagnostics.tar.gz
```

Bundles are redacted and mode 0600. Review before sharing; paths and software
versions are intentionally present, while credentials/config bodies are not.

## Export and Restore

```bash
agent-stack export ~/agent-stack-user-data.tar.gz
agent-stack restore ~/agent-stack-user-data.tar.gz
agent-stack setup --repair
```

Normal exports omit secrets. `--include-secrets` is plaintext and requires an
encrypted transfer channel. Restore validates paths, backs up destination config,
and rebinds the destination's local token.

## Rollback

Remove stack wiring but preserve all bytes:

```bash
./scripts/uninstall.sh
```

Restore Hermes Metal's service definitions and primary CLI:

```bash
./scripts/uninstall.sh --restore-metal
```

Cutover failures do not require this procedure: they automatically restore the
pre-cutover service snapshot.

## Troubleshooting Order

1. `agent-stack status`
2. `agent-stack doctor --quick`
3. `agent-stack diagnostics`
4. inspect the named failed phase in the latest setup log
5. resolve the exact preflight/phase remedy
6. rerun `agent-stack setup --repair`
7. run full doctor only after quick doctor passes

Never expose port 8080 through a wildcard bind, tunnel, or reverse proxy.
