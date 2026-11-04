# Shared public command contract. Recipe files supply native implementations.
.DEFAULT_GOAL := help
.NOTPARALLEL: test _cookbook-tests

COOKBOOK_TEST_SUITES ?= lint unit security
COOKBOOK_KNOWN_SUITES := lint unit integration e2e security
ifneq ($(strip $(filter-out $(COOKBOOK_KNOWN_SUITES),$(COOKBOOK_TEST_SUITES))),)
$(error unknown cookbook test suite)
endif
ifneq ($(words $(filter lint unit security,$(sort $(COOKBOOK_TEST_SUITES)))),3)
$(error cookbook recipes must implement lint, unit and security checks)
endif

define cookbook_require
$(if $(strip $(value $(1))),$(call $(1)),$(error recipe does not implement $(2)))
endef

.PHONY: help setup fmt build test _cookbook-tests test\:lint test\:unit test\:security clean

help:
	@printf '%s\n' 'make help       List implemented commands' 'make setup      Prepare pinned tools and dependencies' 'make fmt        Apply formatting' 'make build      Compile, package and validate outputs' 'make test       Run implemented test groups without building' 'make test:lint  Check formatting, code and configuration' 'make test:unit  Run isolated behavior tests' 'make test:security  Run applicable security checks' 'make clean      Remove declared outputs; preserve retained data'
ifdef cookbook_dev
	@printf '%s\n' 'make dev        Run the implemented local stack' 'make dev:stop   Stop this checkout development session' 'make dev:logs   Show this checkout development logs'
endif
ifneq ($(filter integration,$(COOKBOOK_TEST_SUITES)),)
	@printf '%s\n' 'make test:integration  Test real component boundaries'
endif
ifneq ($(filter e2e,$(COOKBOOK_TEST_SUITES)),)
	@printf '%s\n' 'make test:e2e   Run complete product journeys; requires current build'
endif
ifdef cookbook_db_status
	@printf '%s\n' 'make db:status  Show migration status'
endif
ifdef cookbook_db_migrate
	@printf '%s\n' 'make db:migrate Apply migrations to the selected development database'
endif
ifdef cookbook_db_seed
	@printf '%s\n' 'make db:seed    Load development seed data'
endif
ifdef cookbook_db_reset
	@printf '%s\n' 'make db:reset   Explicitly reset the local development database'
endif

setup:
	$(call cookbook_require,cookbook_setup,setup)

fmt:
	$(call cookbook_require,cookbook_fmt,fmt)

build:
	$(call cookbook_require,cookbook_build,build)
	$(call cookbook_require,cookbook_validate,build output validation)

test:
ifdef cookbook_test_all
	$(call cookbook_test_all)
else
	@$(MAKE) --no-print-directory _cookbook-tests
endif

_cookbook-tests: test\:lint test\:unit test\:security

test\:lint:
	$(call cookbook_require,cookbook_test_lint,test:lint)

test\:unit:
	$(call cookbook_require,cookbook_test_unit,test:unit)

test\:security:
	$(call cookbook_require,cookbook_test_security,test:security)

ifneq ($(filter integration,$(COOKBOOK_TEST_SUITES)),)
.PHONY: test\:integration
_cookbook-tests: test\:integration
test\:integration:
	$(call cookbook_require,cookbook_test_integration,test:integration)
endif

ifneq ($(filter e2e,$(COOKBOOK_TEST_SUITES)),)
.PHONY: test\:e2e
_cookbook-tests: test\:e2e
test\:e2e:
	$(call cookbook_require,cookbook_test_e2e,test:e2e)
endif

clean:
	$(call cookbook_require,cookbook_clean,clean)

ifdef cookbook_dev
.PHONY: dev dev\:stop dev\:logs
dev:
	$(call cookbook_require,cookbook_dev,dev)
dev\:stop:
	$(call cookbook_require,cookbook_dev_stop,dev:stop)
dev\:logs:
	$(call cookbook_require,cookbook_dev_logs,dev:logs)
endif

ifdef cookbook_db_status
.PHONY: db\:status
db\:status:
	$(call cookbook_db_status)
endif
ifdef cookbook_db_migrate
.PHONY: db\:migrate
db\:migrate:
	$(call cookbook_db_migrate)
endif
ifdef cookbook_db_seed
.PHONY: db\:seed
db\:seed:
	$(call cookbook_db_seed)
endif
ifdef cookbook_db_reset
.PHONY: db\:reset
db\:reset:
	$(call cookbook_db_reset)
endif
