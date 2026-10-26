#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the XR1710G SDR chainloader as a FIT stock U-Boot can boot.

    platform/chainloader/build.py -> platform/build/images/w1700k-chainloader.itb

The chainloader is a U-Boot 2026.07 image based on OpenWrt's W1700K UBI
reference. It uses raw MTD and standard UBI, keeps its environment in the
`ubootenv`/`ubootenv2` UBI volumes, and boots one of two FIT slots or the
recovery FIT. Stock U-Boot remains the first stage and writes this image into
the 1 MiB `chainloader` partition.
"""
import lzma
from pathlib import Path
import struct
import sys

UNIT = Path(__file__).resolve().parent
sys.path.insert(0, str(UNIT.parent))
from buildlib import BUILD, IMAGES, LOGS, kconfig, main_guard, require, write_if_changed
import partitions
import uboot

FIT = IMAGES / 'w1700k-chainloader.itb'
DEFCONFIG = 'an7581_xr1710g_chainloader_defconfig'
ENV = 'an7581_xr1710g_chainloader_env'
ENTRY = 0x81E00000
FDT_LOAD = 0x82000000
LIMIT = FDT_LOAD
LZMA_DICTIONARY = 0x800000
FILES = {
    f'configs/{DEFCONFIG}': UNIT / 'configs' / DEFCONFIG,
    f'defenvs/{ENV}': UNIT / 'defenvs' / ENV,
    'dts/upstream/src/arm64/airoha/an7581-w1700k-ubi.dts':
        UNIT / 'an7581-w1700k-ubi.dts',
    'arch/arm/dts/an7581-w1700k-ubi-u-boot.dtsi':
        UNIT / 'an7581-w1700k-ubi-u-boot.dtsi',
}
ENABLED = ('POSITION_INDEPENDENT', 'FIT', 'SHA256', 'CMD_BOOTM', 'CMD_MTD',
           'CMD_UBI', 'CMD_TFTPBOOT', 'CMD_HASH', 'AUTOBOOT', 'BOOTCOUNT_LIMIT',
           'BOOTCOUNT_ENV', 'MTD_SPI_NAND', 'AIROHA_SNFI_SPI', 'AIROHA_ETH',
           'ENV_IS_IN_UBI', 'ENV_REDUNDANT', 'CMD_SAVEENV', 'AUTOBOOT_MENU_SHOW',
           'CMD_BOOTMENU', 'ENV_USE_DEFAULT_ENV_TEXT_FILE')
DISABLED = ('ENV_IS_IN_MMC', 'ENV_IS_NOWHERE', 'ENV_IS_IN_MTD', 'BOOTSTD',
            'EFI_LOADER', 'MMC', 'PCI', 'USB', 'CMD_MMC', 'CMD_GO')
LINKED = ('env_ubi_media_ready', 'ubi_part_ro', 'ubi_part_rw',
          'w25n04lv_ooblayout_ecc')


def patches():
    # OpenWrt's board support, our env-UBI safety patches, and the shared
    # NAND/network fixes used by the old loader. The BMT patches are not used:
    # the chainloader addresses the chip through standard UBI.
    env = sorted((UNIT.parent / 'loader/patches').glob('000[4-7]-*.patch'))
    return (sorted((UNIT / 'patches').glob('*.patch'))
            + env + list(uboot.SHARED_PATCHES))


def compress(image):
    """LZMA-alone with the vendor's dictionary size and the size in the header."""
    compressor = lzma.LZMACompressor(
        format=lzma.FORMAT_ALONE,
        filters=[{'id': lzma.FILTER_LZMA1, 'preset': 9 | lzma.PRESET_EXTREME,
                  'dict_size': LZMA_DICTIONARY}])
    payload = bytearray(compressor.compress(image) + compressor.flush())
    require(payload[5:13] == b'\xff' * 8, 'chainloader: unexpected LZMA header')
    struct.pack_into('<Q', payload, 5, len(image))
    require(lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(bytes(payload)) == image,
            'chainloader: the LZMA payload does not decompress to u-boot.bin')
    return bytes(payload)


def main():
    partitions.v2_check('platform/chainloader/an7581-w1700k-ubi.dts', 'chainloader')
    src = uboot.prepare('chainloader', patches(), FILES)
    # The upstream AN7581 target does not select a text-offset base. The
    # chainloader links at 0x81e00000; record a zero offset in the Image header.
    board_kconfig = src / 'arch/arm/mach-airoha/Kconfig'
    text = board_kconfig.read_text()
    if 'config LNX_KRNL_IMG_TEXT_OFFSET_BASE' not in text:
        write_if_changed(board_kconfig, text + '\nconfig LNX_KRNL_IMG_TEXT_OFFSET_BASE\n'
                         '\tdefault 0x81E00000 if TARGET_AN7581\n')
    uboot.mkimage(src)
    out = uboot.build('chainloader', src, DEFCONFIG)
    config = kconfig(out / '.config')
    uboot.require_config('chainloader', config, ENABLED, DISABLED)
    image, dtb, linked = uboot.check_image('chainloader', out, config, LIMIT, entry=ENTRY)
    missing = [name for name in LINKED if name not in linked]
    require(not missing, 'chainloader: not linked: ' + ', '.join(missing))

    payload = BUILD / 'chainloader/u-boot.bin.lzma'
    write_if_changed(payload, compress(image))
    IMAGES.mkdir(parents=True, exist_ok=True)
    its = uboot.stock_fit('XR1710G SDR chainloader', 'chainloader', payload, 'lzma',
                          out / 'u-boot.dtb', entry=ENTRY, fdt_load=FDT_LOAD)
    uboot.make_fit(its, FIT, out / 'scripts/dtc', LOGS / 'chainloader.log')
    size = FIT.stat().st_size
    require(size <= partitions.V2_CHAINLOADER_BYTES,
            f'chainloader: the FIT ({size} bytes) does not fit its '
            f'{partitions.V2_CHAINLOADER_BYTES}-byte partition')
    print(f'chainloader: {FIT} ({size} bytes)')
    return 0


if __name__ == '__main__':
    main_guard(main)
