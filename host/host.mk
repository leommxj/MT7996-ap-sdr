# SPDX-License-Identifier: MIT
ifeq ($(shell uname -s),Darwin)
HOMEBREW_PREFIX ?= $(shell brew --prefix 2>/dev/null)
ifneq ($(HOMEBREW_PREFIX),)
CPPFLAGS += -I$(HOMEBREW_PREFIX)/include
LDFLAGS += -L$(HOMEBREW_PREFIX)/lib
endif
else
LDLIBS += -lrt
endif
