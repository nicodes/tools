COOKBOOK_PYTHON ?= python3
COOKBOOK_ADDON = $(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/godot_addon.py"

define cookbook_setup
$(COOKBOOK_ADDON) setup
endef
define cookbook_fmt
$(COOKBOOK_ADDON) fmt
endef
define cookbook_build
$(COOKBOOK_ADDON) build
endef
define cookbook_validate
$(COOKBOOK_ADDON) validate
endef
define cookbook_test_lint
$(COOKBOOK_ADDON) test:lint
endef
define cookbook_test_unit
$(COOKBOOK_ADDON) test:unit
endef
define cookbook_test_security
$(COOKBOOK_ADDON) test:security
endef
define cookbook_test_integration
$(COOKBOOK_ADDON) test:integration
endef
define cookbook_test_e2e
$(COOKBOOK_ADDON) test:e2e
endef
define cookbook_test_all
$(COOKBOOK_ADDON) test
endef
define cookbook_clean
$(COOKBOOK_ADDON) clean
endef

include $(call cookbook_path,$(COOKBOOK_ROOT)/cookbook/common.mk)
