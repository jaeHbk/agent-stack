SHELL := /bin/bash

.PHONY: help install preflight test onboarding-test integration-test hermes-regression-test security-audit secret-scan release-check doctor status start stop restart export runtime-package uninstall

help:
	@printf '%s\n' \
	  'make install           Run guided setup/reconciliation' \
	  'make preflight         Report all onboarding prerequisites' \
	  'make test              Run static and unit tests' \
	  'make onboarding-test   Run isolated onboarding scenarios' \
	  'make integration-test  Run tests against the installed stack' \
	  'make hermes-regression-test' \
	  '                       Test vendored Hermes fixes in locked environments' \
	  'make security-audit    Query OSV and enforce the reviewed allowlist' \
	  'make secret-scan       Scan the complete Git history for secrets' \
	  'make release-check     Run public source-release validation' \
	  'make doctor            Run the full health check' \
	  'make status            Show concise service and client status' \
	  'make start|stop        Control the Bonsai service' \
	  'make export            Create a portable user-data archive' \
	  'make runtime-package   Package the current verified runtime' \
	  'make uninstall         Remove stack wiring; preserve data/runtime'

install:
	./install

preflight:
	./bin/agent-stack preflight

test:
	./tests/test_scripts.sh
	python3 -m unittest discover -s tests -p 'test_*.py' -v

onboarding-test:
	./tests/test_onboarding.sh

integration-test:
	./tests/test_live.sh

hermes-regression-test:
	./scripts/test-hermes-feishu.sh

security-audit:
	./scripts/security-audit.sh

secret-scan:
	@command -v gitleaks >/dev/null 2>&1 || { echo 'gitleaks is required; run brew install gitleaks' >&2; exit 2; }
	gitleaks git --redact --no-banner .

release-check: secret-scan test onboarding-test

doctor:
	./bin/agent-stack doctor

status:
	./bin/agent-stack status

start:
	./bin/agent-stack start

stop:
	./bin/agent-stack stop

restart:
	./bin/agent-stack restart

export:
	./bin/agent-stack export

runtime-package:
	./scripts/package-runtime.sh

uninstall:
	./scripts/uninstall.sh
