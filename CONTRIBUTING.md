# Contributing

## Development

This project supports macOS on Apple Silicon and the system Bash 3.2. Read
[`AGENTS.md`](AGENTS.md) before changing installers or service behavior.

```bash
brew bundle
make test
make onboarding-test
make secret-scan
```

Changes affecting an installed stack must also pass:

```bash
make integration-test
agent-stack doctor
make security-audit
```

Do not commit credentials, user configuration, model weights, generated
LaunchAgents, logs, diagnostics, runtime archives, or copied third-party code
without an explicit compatible license.

## Pull Requests

Keep changes scoped, document user-visible behavior, add regression tests, and
update immutable pins intentionally. Pull requests must pass the `validate`
check. By contributing, you certify that you have the right to submit the work
under this repository's MIT license.

Report vulnerabilities through the private process in [`SECURITY.md`](SECURITY.md),
not through a public issue.
