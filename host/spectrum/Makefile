# SPDX-License-Identifier: MIT
# w1700k-spectrum: a C++17 compiler, zlib and FFTW3 (single precision). The page in web/ is
# compiled into the binary; the stream comes from w1700k-stream (../stream). Tune for the build host
# with CXXFLAGS="-O3 -march=native".
CXX ?= c++
CXXFLAGS ?= -O3
WARNINGS ?= -Wall -Wextra -Werror
PREFIX ?= /usr/local
override CXXFLAGS += -std=c++17 -pthread -I../stream/include $(WARNINGS)
LDLIBS += -pthread -lz -lfftw3f
include ../host.mk

SOURCES := $(wildcard src/*.cpp)
OBJECTS := $(patsubst src/%.cpp,build/%.o,$(SOURCES)) build/web_assets.o

.PHONY: all clean install
all: build/w1700k-spectrum

build/w1700k-spectrum: $(OBJECTS)
	$(CXX) $(LDFLAGS) $^ $(LDLIBS) -o $@

build/%.o: src/%.cpp $(wildcard src/*.h ../stream/include/*.h) | build
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

build/web_assets.cpp: embed.sh $(wildcard web/*) | build
	sh embed.sh web > $@

build/web_assets.o: build/web_assets.cpp src/web.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -Isrc -c $< -o $@

build:
	mkdir -p $@

install: build/w1700k-spectrum
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 755 $< $(DESTDIR)$(PREFIX)/bin/w1700k-spectrum

clean:
	rm -rf build
