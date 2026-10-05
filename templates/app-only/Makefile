SHELL := /bin/bash
.DEFAULT_GOAL := help
PLAYWRIGHT_INSTALL_FLAGS ?= $(if $(CI),--with-deps,)
APP_PORT ?= 8081
.PHONY: help install dev stop check test build vuln clean e2e
help:
	@printf '%s\n' 'make install  Install pinned tools, locked dependencies and Chromium' 'make dev      Select free ports and start this checkout’s stack' 'make stop     Stop only the stack owned by this checkout' 'make check    Full tests, builds, browser journey and online vulnerability scans' 'make test     Frontend, workflow and applicable full Go integration/race checks' 'make build    Release-equivalent web and container artifacts' 'make vuln     Online source dependency vulnerability checks' 'make e2e      Core browser journey against built artifacts or an isolated stack' 'make clean    Remove generated artifacts; preserve local databases'
install:
	mise trust .mise.toml
	mise install
	mise exec -- bun install --cwd app --frozen-lockfile
	cd app && mise exec -- bun x --no-install playwright install $(PLAYWRIGHT_INSTALL_FLAGS) --only-shell chromium
dev:
	@mise exec -- python3 scripts/engineering/helpers/dev.py run -- scripts/dev-app.sh "$(APP_PORT)"
stop:
	@mise exec -- python3 scripts/engineering/helpers/dev.py stop
check: test vuln e2e
test:
	@mise exec -- bash scripts/test.sh
build:
	@mise exec -- bash scripts/build.sh
vuln:
	@mise exec -- bash scripts/vuln.sh
e2e: build
	@mise exec -- bash scripts/e2e.sh
clean:
	@rm -rf .artifacts app/dist app/dist-e2e app/dist-check app/playwright-report app/test-results

