COOKBOOK_PYTHON ?= python3
COOKBOOK_EXEC ?= env MISE_AUTO_INSTALL=false MISE_EXEC_AUTO_INSTALL=false MISE_TRUSTED_CONFIG_PATHS="$(CURDIR)" mise exec --
COOKBOOK_GAME = $(COOKBOOK_EXEC) $(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/godot_game.py"
COOKBOOK_HAS_DEV := $(filter dev,$(COOKBOOK_CAPABILITIES))

define cookbook_setup
env MISE_TRUSTED_CONFIG_PATHS="$(CURDIR)" $(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/godot_game.py" setup
endef
define cookbook_fmt
$(COOKBOOK_GAME) fmt
endef
define cookbook_build
$(COOKBOOK_GAME) build
endef
define cookbook_validate
$(COOKBOOK_GAME) validate
endef
define cookbook_test_lint
$(COOKBOOK_GAME) test:lint
endef
define cookbook_test_unit
$(COOKBOOK_GAME) test:unit
endef
define cookbook_test_security
$(COOKBOOK_GAME) test:security
endef
define cookbook_test_integration
$(COOKBOOK_GAME) test:integration
endef
define cookbook_test_e2e
$(COOKBOOK_GAME) test:e2e
endef
define cookbook_test_all
$(COOKBOOK_GAME) test
endef
define cookbook_clean
$(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/godot_game.py" clean
endef

ifdef COOKBOOK_HAS_DEV
define cookbook_dev
$(COOKBOOK_GAME) dev
endef
define cookbook_dev_stop
$(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/godot_game.py" dev:stop
endef
define cookbook_dev_logs
$(COOKBOOK_PYTHON) "$(COOKBOOK_ROOT)/cookbook/godot_game.py" dev:logs
endef
endif

include $(call cookbook_path,$(COOKBOOK_ROOT)/cookbook/common.mk)
