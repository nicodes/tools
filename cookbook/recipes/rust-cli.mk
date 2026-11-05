COOKBOOK_PYTHON ?= python3
COOKBOOK_EXEC ?= env MISE_AUTO_INSTALL=false MISE_EXEC_AUTO_INSTALL=false MISE_TRUSTED_CONFIG_PATHS="$(CURDIR)" mise exec --
COOKBOOK_RUST = $(COOKBOOK_EXEC) $(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/rust_cli.py"

define cookbook_setup
env MISE_TRUSTED_CONFIG_PATHS="$(CURDIR)" $(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/rust_cli.py" setup
endef
define cookbook_fmt
$(COOKBOOK_RUST) fmt
endef
define cookbook_build
$(COOKBOOK_RUST) build
endef
define cookbook_validate
$(COOKBOOK_RUST) validate
endef
define cookbook_test_lint
$(COOKBOOK_RUST) test:lint
endef
define cookbook_test_unit
$(COOKBOOK_RUST) test:unit
endef
define cookbook_test_security
$(COOKBOOK_RUST) test:security
endef
define cookbook_test_integration
$(COOKBOOK_RUST) test:integration
endef
define cookbook_test_e2e
$(COOKBOOK_RUST) test:e2e
endef
define cookbook_test_all
$(COOKBOOK_RUST) test
endef
define cookbook_clean
$(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/rust_cli.py" clean
endef

include $(call cookbook_path,$(COOKBOOK_ROOT)/cookbook/common.mk)
