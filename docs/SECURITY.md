# Security

## Trust Model

This is a user-scoped development system, not a multi-tenant service. Agents,
models, extensions, repository instructions, and tool output remain untrusted.
Pi is the only worktree writer; deterministic tests and review remain required.

## Network and Credential Boundaries

- Local inference binds only to `127.0.0.1:8080` and requires a generated token.
- The llama.cpp browser UI is disabled at build and runtime.
- The token is mode 0600; Pi resolves it from a file and Hermes from its private
  `.env`. It is never written to setup state, diagnostics, Git, or shell history.
- Bedrock uses the standard AWS SDK chain. Setup records only readiness and
  region/profile names, never credential values.
- Local/hybrid do not send prompts to cloud unless `pi-sonnet`/`pi-opus` (or an
  explicitly configured cloud action) is invoked.

## Supply Chain

- Pi, Hermes, PrismML llama.cpp, and Bonsai are immutable pins. The Pi workflow package is repository-owned and versioned with this source tree.
- Models and downloaded remediation archives have exact SHA-256/integrity pins.
- Prebuilt runtimes require an archive SHA-256 and verify every manifest file.
- Safe extraction rejects traversal, absolute paths, devices, FIFOs, hardlinks,
  and unsafe symlinks.
- `require-codesign` is available when a trusted release signing identity exists;
  source builds remain the default until a separately verified runtime is released.
- GitHub Actions are pinned to full commits.
- Managed upstream checkouts reject unexpected tracked changes.

The vendored Hermes Feishu and dependency patches remain checksum-pinned in
`versions.env`; doctor and the fail-closed security audit verify them before
accepting reviewed source-remediated advisories.

## Transaction and Data Safety

- Setup acquires an exclusive lock and writes schema state atomically.
- A single backup is created before planned mutation; healthy no-op runs do not
  create backup churn.
- Model downloads resume into `.partial` and become active only after size/hash.
- Runtime archives extract to staging and atomically replace only after complete
  verification; prior runtimes are preserved under a timestamped name.
- Cutover snapshots service/plist state. Hermes Metal plists move only after an
  authenticated Bonsai health response. Failures and handled signals roll back;
  the next run recovers a transaction left by process death.
- Cloud transition stops both inference stacks but does not delete either.

## Logging and Diagnostics

Setup logs are mode 0600 and redact known local tokens, bearer headers, AWS
access-key identifiers, and secret/token/password assignments. Every phase has a
hard timeout. Diagnostics contain platform/pin/state/status data and a redacted
setup log; they exclude environments, config bodies, sessions, memory, models,
and credentials. Review a bundle before external sharing.

## Dependency Audit

```bash
agent-stack security-audit
```

The command runs Hermes OSV audit at `--fail-on low`, has a 20-minute timeout,
requires a positive internally consistent report, and blocks every unknown
finding. Exact source-remediated advisories are accepted only while package,
version, source, pin, and patch evidence all match.

Use `make hermes-regression-test` when changing Hermes pins or patches. Pi's
global advisory remediation is checksum-pinned and checked by doctor. Run
`make secret-scan` against the complete Git history before every release.

## Export and Residual Risk

Normal exports exclude Pi auth, Hermes `.env`, the local token, logs, runtime,
and models. `--include-secrets` creates a plaintext mode-0600 archive—not
encryption.

Residual risks include third-party code running with user authority, malicious
repository instructions, same-user compromise bypassing loopback auth, model
errors, undisclosed dependencies, and changing vulnerability data. Do not
broaden the listen address, disable auth/checksums, float pins, bypass patch
checks, or commit generated/user data.
