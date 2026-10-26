#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the unit's Linux kernel and device tree.

    platform/linux/build.py           # source, firmware, config, Image and DTB

Linux comes from kernel.org; the Airoha board support (patches, driver files and
the SoC device tree) from the pinned OpenWrt commit; then our own patches and the
shared BMT translation layer are applied on top. Requires the Buildroot
toolchain (platform/rootfs/build.py toolchain).

Outputs: platform/build/linux/arch/arm64/boot/Image and
platform/build/linux/sdr/an7581-w1700k-sdr.dtb. The external module and the
unit-side clients are built against this tree by `make -C platform/linux module reader`.
"""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request

UNIT = Path(__file__).resolve().parent
sys.path.insert(0, str(UNIT.parent))
from buildlib import (BUILD, CROSS_COMPILE, DOWNLOADS, DTB, KERNEL_IMAGE, LINUX_OUT, LOGS,
                      SRC, apply_patches, build_env, copy_if_changed, jobs,
                      kconfig, main_guard, mark_prepared, patch_stamp, prepared,
                      require, run, sha256_file, sources, write_if_changed)
import board_check
import fetch
import partitions

LINUX_SRC = SRC / 'linux'
FIRMWARE_DIR = BUILD / 'linux-firmware'
PHY_FIRMWARE = FIRMWARE_DIR / 'rtl8261n.bin'
# The kernel's name for it; the RTL8261CE driver requests it by this path.
CE_FIRMWARE = FIRMWARE_DIR / 'rtl_nic/rtl8261c.bin'
LOG = LOGS / 'linux.log'
# OpenWrt's kernel patch series, applied in this order before ours.
OPENWRT_PATCH_DIRS = ('target/linux/generic/backport-6.18',
                      'target/linux/generic/pending-6.18',
                      'target/linux/generic/hack-6.18',
                      'target/linux/airoha/patches-6.18')
OPENWRT_FILE_DIRS = ('target/linux/generic/files',)
OPENWRT_DTS = 'target/linux/airoha/dts'
# Our files copied into drivers/mtd/nand. The BMT core is the one the loader
# also compiles; it is renamed sdr_bmt.c so it does not collide with the
# OpenWrt airoha_bmt.c that stays in the tree uncompiled.
BMT = UNIT.parent / 'bmt'
PROJECT_FILES = {
    'airoha_bmt.h': BMT / 'airoha_bmt.h',
    'sdr_bmt.c': BMT / 'airoha_bmt.c',
    'sdr_bmt_layer.c': UNIT / 'sdr_bmt_layer.c',
    'sdr_bmt_layer.h': UNIT / 'sdr_bmt_layer.h',
    **{name: BMT / name for name in ('airoha_bmt_write.c', 'airoha_bmt_write.h',
                                     'airoha_bmt_transaction.c',
                                     'airoha_bmt_transaction.h',
                                     'airoha_bmt_mtd_write.h')},
}
DTS = UNIT / 'dts/an7581-w1700k-sdr.dts'


def local_patches():
    return sorted((UNIT / 'patches').glob('*.patch'))


def prepare_source():
    """Extract Linux, apply OpenWrt's series and ours; re-extract when either changes."""
    archive = fetch.linux_archive()
    openwrt = fetch.openwrt()
    stamp = patch_stamp(local_patches()) + sources()['linux']['sha256'] + \
        sources()['openwrt']['commit']
    if not prepared(LINUX_SRC, stamp):
        print('linux: preparing the kernel source tree')
        fetch.extract(archive, LINUX_SRC)
        for directory in OPENWRT_FILE_DIRS:
            shutil.copytree(openwrt / directory, LINUX_SRC, dirs_exist_ok=True)
        series = [patch for directory in OPENWRT_PATCH_DIRS
                  for patch in sorted((openwrt / directory).glob('*.patch'))]
        require(series, 'the OpenWrt checkout holds no kernel patches')
        copy_project_files()
        apply_patches(LINUX_SRC, series + local_patches(), LOG)
        mark_prepared(LINUX_SRC, stamp)
    copy_project_files()
    return openwrt


def copy_project_files():
    nand = LINUX_SRC / 'drivers/mtd/nand'
    for name, source in PROJECT_FILES.items():
        copy_if_changed(source, nand / name)
    write_if_changed(nand / 'airoha_bmt_policy.h', partitions.policy_header())


def phy_firmware(openwrt):
    """rtl8261n.bin, the RTL8261N 10 GbE PHY patch, built into the kernel.

    The W1700K's two 10GBASE-T PHYs are RTL8261Ns; the Realtek PHY driver will
    not bind them without it, and it must be present at the first probe, before
    any root is mounted: a failed probe leaves the PHY in reset. It is generated
    exactly as OpenWrt's rtl826x-firmware package does, and the output is checked
    against its hash.
    """
    pin = sources()['rtl8261n']
    if PHY_FIRMWARE.is_file() and sha256_file(PHY_FIRMWARE) == pin['output_sha256']:
        return
    cache = DOWNLOADS / 'rtl8261n'
    with tempfile.TemporaryDirectory(prefix='rtl8261n-') as temporary:
        work = Path(temporary)
        for name, digest in pin['files'].items():
            path = cache / name
            if not path.is_file():
                cache.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(pin['url'] + name, timeout=60) as response:
                    path.write_bytes(response.read())
            require(sha256_file(path) == digest, f'rtl8261n: {name} is not the pinned file')
            shutil.copyfile(path, work / name)
        package = openwrt / 'package/firmware/rtl826x-firmware'
        for name, digest in pin['openwrt_files'].items():
            path = package / name
            require(sha256_file(path) == digest, f'rtl8261n: {name} is not the pinned file')
            if name.startswith('src/'):
                shutil.copyfile(path, work / Path(name).name)
            else:
                run(['patch', '-s', '-p1', '-i', path], cwd=work, log=LOG)
        run(['cc', '-O1', '-w', 'rtl8261n_rtl8264b.c', 'phy_patch.c', '-o', 'phy_patch'],
            cwd=work, log=LOG)
        run(['./phy_patch'], cwd=work, log=LOG)
        generated = work / 'rtl8261n.bin'
        require(sha256_file(generated) == pin['output_sha256'],
                'rtl8261n: the generated firmware does not match the pinned output')
        FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(generated, PHY_FIRMWARE)


def ce_firmware():
    """rtl_nic/rtl8261c.bin, the RTL8261CE's parameters, built into the kernel.

    The W1700K2 (HW 2.1) has RTL8261CEs where the W1700K has RTL8261Ns, on the
    same MDIO addresses, resets and inverted lanes; the driver is chosen by the
    PHY's ID, so one kernel and one device tree serve both. The CE driver loads
    this file when the PHY is first configured. Realtek's file from
    linux-firmware, pinned by commit and checked against its hash.
    """
    pin = sources()['rtl8261c']
    if CE_FIRMWARE.is_file() and sha256_file(CE_FIRMWARE) == pin['sha256']:
        return
    cached = fetch.download(pin['url'], pin['sha256'])
    CE_FIRMWARE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, CE_FIRMWARE)


def kernel_make(*targets):
    run(['make', '-C', LINUX_SRC, f'O={LINUX_OUT}', 'ARCH=arm64',
         f'CROSS_COMPILE={CROSS_COMPILE}', f'-j{jobs()}', *targets],
        env=build_env(), log=LOG)


def configure():
    """allnoconfig plus receiver.config, then require that every request survived."""
    requested = UNIT / 'receiver.config'
    merged = BUILD / 'linux-receiver.config'
    # Relative to the source tree, so no build path ends up in /proc/config.gz.
    firmware = os.path.relpath(FIRMWARE_DIR, LINUX_SRC)
    write_if_changed(merged, requested.read_text() + f'CONFIG_EXTRA_FIRMWARE_DIR="{firmware}"\n')
    LINUX_OUT.mkdir(parents=True, exist_ok=True)
    kernel_make(f'KCONFIG_ALLCONFIG={merged}', 'allnoconfig')
    resolved = kconfig(LINUX_OUT / '.config')
    dropped = [f'{key}={value}' for key, value in kconfig(requested).items()
               if resolved.get(key, 'n') != value]
    require(not dropped, 'Kconfig dropped requested settings: ' + ', '.join(dropped))


def device_tree(openwrt):
    """Preprocess and compile the board DTS against OpenWrt's SoC include."""
    kernel_make('scripts_dtc')
    out = DTB.parent
    out.mkdir(parents=True, exist_ok=True)
    preprocessed = out / 'an7581-w1700k-sdr.dts.tmp'
    run([CROSS_COMPILE + 'gcc', '-E', '-nostdinc', '-undef', '-D__DTS__',
         '-x', 'assembler-with-cpp', '-I', LINUX_SRC / 'include',
         '-I', openwrt / OPENWRT_DTS, DTS, '-o', preprocessed], env=build_env(), log=LOG)
    run([LINUX_OUT / 'scripts/dtc/dtc', '-@', '-I', 'dts', '-O', 'dtb', '-o', DTB,
         preprocessed], log=LOG)


def main():
    partitions.v2_check('platform/linux/dts/an7581-w1700k-sdr.dts', 'linux-v2')
    require(Path(CROSS_COMPILE + 'gcc').is_file(),
            'no cross compiler; run platform/rootfs/build.py toolchain first')
    openwrt = prepare_source()
    phy_firmware(openwrt)
    ce_firmware()
    configure()
    kernel_make('Image', 'modules')
    device_tree(openwrt)
    board_check.board(DTB.read_bytes())
    board_check.linked((LINUX_OUT / 'System.map').read_text())
    print(f'linux: {KERNEL_IMAGE} ({KERNEL_IMAGE.stat().st_size} bytes), {DTB}')
    return 0


if __name__ == '__main__':
    main_guard(main)
