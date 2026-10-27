# SPDX-License-Identifier: MIT
#
#   make          build everything below, in dependency order
#   make check    run the test suite
#
# Unit images land in platform/build/images/, MT7996 firmware in firmware/build/,
# host programs under host/. The first build downloads about 500 MB of sources and
# compiles a cross toolchain: about ten minutes on a 20-core host plus downloads;
# later builds are incremental.
# JOBS=N limits parallel compile jobs (default: all CPUs).

PYTHON ?= python3
export PYTHONDONTWRITEBYTECODE := 1

.PHONY: all toolchain linux sdr rootfs loader chainloader dumper restorer images firmware host soapy \
	check check-soapy clean distclean

all: images dumper restorer firmware host

# Buildroot cross toolchain, target packages and the host tools the later steps use.
toolchain:
	$(PYTHON) platform/rootfs/build.py toolchain

# Linux Image and device tree.
linux: toolchain
	$(PYTHON) platform/linux/build.py

# The w1700k_sdr module and the unit-side clients, built against that kernel.
sdr: linux
	$(MAKE) -C platform/linux module reader

# The read-only root filesystem, with the module and clients installed.
rootfs: sdr
	$(PYTHON) platform/rootfs/build.py image

# Our U-Boot, which stock U-Boot loads from NAND. Also builds mkimage.
loader: toolchain
	$(PYTHON) platform/loader/build.py

# The installed second stage: raw MTD + standard UBI, 1 MiB slot.
chainloader: toolchain
	$(PYTHON) platform/chainloader/build.py

# The system and recovery FITs, the root and the durable images (one per NAND part).
images: rootfs chainloader
	$(PYTHON) platform/images.py

# The write-free NAND dumper used for the backup before installation.
dumper: toolchain
	$(PYTHON) platform/dumper/build.py

# The restorer, which writes a backup back to return a unit to stock firmware.
restorer: toolchain
	$(PYTHON) platform/restorer/build.py

# MT7996 ROM patch, WM/WA firmware and the stream programs.
firmware:
	$(MAKE) -C firmware

# The stream service, the spectrum web UI server and the IQ recorder.
host:
	$(MAKE) -C host/stream
	$(MAKE) -C host/spectrum
	$(MAKE) -C host/recv

# Optional: needs libsoapysdr-dev and nlohmann-json3-dev on the host.
soapy:
	$(MAKE) -C host/soapy

check:
	$(PYTHON) tools/check_hw_names.py
	$(PYTHON) -m unittest discover -s tests

check-soapy:
	W1700K_STREAM_BIN=$(CURDIR)/host/stream/build/w1700k-stream \
		$(PYTHON) -m unittest discover -s host/soapy/tests

# Remove build outputs but keep downloads and prepared sources.
clean:
	rm -rf platform/build/buildroot platform/build/linux platform/build/loader \
		platform/build/chainloader \
		platform/build/dumper platform/build/restorer platform/build/mkimage \
		platform/build/bootstrap platform/build/images platform/build/rootfs-overlay \
		platform/build/logs host/soapy/build \
		kernel/w1700k_sdr/build

# Remove everything the build created, downloads included.
distclean:
	rm -rf platform/build kernel/w1700k_sdr/build
