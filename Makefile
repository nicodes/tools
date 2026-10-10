.DEFAULT_GOAL := help
SHELL := /bin/bash
export PATH := $(CURDIR)/.artifacts/venv/bin:$(PATH)
.PHONY: help install lint unit integration test build artifact-check browser-install e2e vuln check dev stop clean fleet-audit
help:
	@echo 'make install  Install the pinned helper toolchain'
	@echo 'make check    Test helper boundaries and validate workflow and JavaScript syntax'
install:
	mise trust .mise.toml
	mise install
	python3 -m venv .artifacts/venv
	.artifacts/venv/bin/python -m pip install -r requirements-tools.txt
unit:
	cd sessionauth && go vet ./... && go test -race ./...
	python3 scripts/test-layer.py unit
	python3 -m unittest discover -s workflow-library -v
	python3 -m unittest discover -s template-tests -v

lint:
	python3 helpers/workflow-standards.py
	@for file in helpers/*.mjs helpers/*.cjs; do node --check "$$file"; done
	actionlint
	actionlint templates/full-stack/ci.yml templates/app-only/ci.yml templates/full-stack/bun-updates.yml templates/app-only/bun-updates.yml templates/static-web/ci.yml templates/pr-preview.yml
integration:
	python3 scripts/test-layer.py integration
	python3 helpers/test-caddy.py
test: unit integration
build artifact-check browser-install e2e vuln dev stop:
	@echo '$@: inapplicable: source-only engineering library; Caddy runtime coverage is in integration'
check: lint test
clean:
	rm -rf .artifacts
fleet-audit:
	@test -n "$(FLEET)" || { echo 'Set FLEET to the caller-owned fleet inventory'; exit 1; }
	python3 helpers/fleet-audit.py --fleet "$(FLEET)"
