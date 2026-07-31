# Release and Publication

## Source Release Gate

Before changing visibility or publishing a tag:

1. Confirm the working tree is clean and commit identity uses a public or GitHub
   `noreply` address.
2. Confirm `LICENSE`, `THIRD_PARTY_NOTICES.md`, `SECURITY.md`, and dependency
   licenses are current.
3. Run `make release-check`, `make integration-test`, full `agent-stack doctor`,
   `make security-audit`, and `make hermes-regression-test`.
4. Run Gitleaks against every commit that will be reachable publicly; inspect
   all findings rather than adding broad allowlists.
5. Push the reviewed branch, wait for pinned CI, then require the `validate`
   check and disable force-push and branch deletion.
6. Publish a signed tag when a signing identity is available. Source releases
   must not contain credentials, user exports, model weights, generated
   LaunchAgents, logs, diagnostics, or runtime archives.
7. Verify an unauthenticated clone and `./install --plan` from a clean home.

## Runtime Artifact

The manual `runtime artifact` workflow uses an Apple Silicon `macos-15` runner,
builds `LLAMA_COMMIT`, packages normalized bytes, verifies SHA-256, and uploads
a short-retention CI artifact. It does not publish a GitHub release
automatically.

Before enabling prebuilt setup by default:

1. download the CI artifact and independently verify `SHA256SUMS`;
2. test extraction and full doctor on a second Apple Silicon Mac;
3. sign and notarize when appropriate, then set signature policy to
   `require-codesign`;
4. attach the exact archive to an immutable release;
5. set `LLAMA_PREBUILT_URL` and `LLAMA_PREBUILT_SHA256` in `versions.env`;
6. rerun clean-machine local onboarding without Xcode; and
7. update the pin only through a normal reviewed commit.

Never reuse an artifact URL with changed bytes. Never publish model weights,
credentials, user exports, setup logs, diagnostics, or generated launchd files.
