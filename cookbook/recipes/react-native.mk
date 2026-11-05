COOKBOOK_PYTHON ?= python3
COOKBOOK_EXEC ?= env MISE_AUTO_INSTALL=false MISE_EXEC_AUTO_INSTALL=false MISE_TRUSTED_CONFIG_PATHS="$(CURDIR)" mise exec --
COOKBOOK_REACT_NATIVE = $(COOKBOOK_EXEC) $(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/react_native.py"

define cookbook_setup
env MISE_TRUSTED_CONFIG_PATHS="$(CURDIR)" $(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/react_native.py" setup
endef
define cookbook_fmt
$(COOKBOOK_REACT_NATIVE) fmt
endef
define cookbook_build
$(COOKBOOK_REACT_NATIVE) build
endef
define cookbook_validate
$(COOKBOOK_REACT_NATIVE) validate
endef
define cookbook_test_lint
$(COOKBOOK_REACT_NATIVE) test:lint
endef
define cookbook_test_unit
$(COOKBOOK_REACT_NATIVE) test:unit
endef
define cookbook_test_integration
$(COOKBOOK_REACT_NATIVE) test:integration
endef
define cookbook_test_e2e
$(COOKBOOK_REACT_NATIVE) test:e2e
endef
define cookbook_test_security
$(COOKBOOK_REACT_NATIVE) test:security
endef
define cookbook_test_all
$(COOKBOOK_REACT_NATIVE) test
endef
define cookbook_clean
$(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/react_native.py" clean
endef

include $(call cookbook_path,$(COOKBOOK_ROOT)/cookbook/common.mk)
