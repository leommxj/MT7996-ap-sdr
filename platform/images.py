#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Package the kernel, device tree and root into the images the installer writes.

    platform/images.py

Outputs in platform/build/images/:

  w1700k-system.itb    one FIT for `fit_a`/`fit_b`: kernel, DTB, bootstrap
                       initramfs and the unit's rootfs.squashfs; `conf-sdr`
                       carries sdr,unit/rootfs-sha256/rootfs-size
  rootfs.squashfs      the unit root embedded in the FIT (kept for provisioning)
  w1700k-recovery.itb  the same kernel/DTB/root, booted from `recovery` in RAM
  data-<part>.ubifs    initial UBIFS for the `data` volume: overlay upper/work,
                       logs, recordings, spectrumd

The FITs are parsed only by our loader, so their hashes are SHA-256.
"""
import gzip
import hashlib
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import uuid
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from buildlib import (BUILD, BUILDROOT_OUT, DTB, IMAGES, KERNEL_IMAGE, LINUX_OUT,
                      LOGS, MKIMAGE, PLATFORM, REPRODUCIBLE_ENV, ROOTFS_IMAGE, main_guard,
                      require, run, write_if_changed)
import fdt
import partitions
import uboot

SYSTEM_FIT = IMAGES / 'w1700k-system.itb'
RECOVERY_FIT = IMAGES / 'w1700k-recovery.itb'
ROOTFS = IMAGES / 'rootfs.squashfs'
BOOTSTRAP = BUILD / 'bootstrap/bootstrap.cpio.gz'
RECOVERY_INITRD = BUILD / 'bootstrap/recovery.cpio.gz'
BUSYBOX = BUILDROOT_OUT / 'target/bin/busybox'
LOG = LOGS / 'images.log'

# Load addresses inside the 2 GiB DRAM bank, clear of the regions the device
# tree reserves (0x80000000 ATF, 0x84000000 NPU, 0x87000000 and 0x89000000 QDMA).
KERNEL_LOAD = 0x80200000
FIT_LOAD = 0x81800000      # where the loader reads or receives a FIT
FDT_LOAD = 0x83000000
INITRD_LOAD = 0x85000000
INITRD_END = 0x87000000
FIT_END = FDT_LOAD
# A fixed UBIFS identity, so the durable image is reproducible.
DATA_UUID = uuid.UUID('d87d243d-7e3c-5e3a-9527-2aa6bc7daf31')


# --------------------------------------------------------------------------
# The bootstrap initramfs
# --------------------------------------------------------------------------

def newc(name, mode, data=b'', inode=1, rdev=(0, 0)):
    """One `newc` cpio record, owned by root, with a zero timestamp."""
    encoded = name.encode() + b'\0'
    fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0, rdev[0], rdev[1], len(encoded), 0)
    record = b'070701' + b''.join(f'{value:08x}'.encode() for value in fields) + encoded
    record += bytes(-len(record) % 4) + data
    return record + bytes(-len(data) % 4)


def bootstrap(root=None):
    """busybox, /init and the mount points; with `root`, the SquashFS too."""
    entries = [(name, stat.S_IFDIR | 0o755, b'', (0, 0))
               for name in ('bin', 'data', 'dev', 'lower', 'newroot', 'proc', 'ram', 'sbin', 'sys')]
    entries += [
        ('init', stat.S_IFREG | 0o755, (PLATFORM / 'bootstrap/init').read_bytes(), (0, 0)),
        ('bin/busybox', stat.S_IFREG | 0o755, BUSYBOX.read_bytes(), (0, 0)),
        ('bin/sh', stat.S_IFLNK | 0o777, b'busybox', (0, 0)),
        ('sbin/switch_root', stat.S_IFLNK | 0o777, b'../bin/busybox', (0, 0)),
        ('dev/console', stat.S_IFCHR | 0o600, b'', (5, 1)),
    ]
    if root is not None:
        entries.append(('rootfs.squashfs', stat.S_IFREG | 0o444, root, (0, 0)))
    archive = bytearray()
    for inode, (name, mode, data, rdev) in enumerate(sorted(entries), 1):
        archive += newc(name, mode, data, inode, rdev)
    archive += newc('TRAILER!!!', stat.S_IFREG, inode=len(entries) + 1)
    archive += bytes(-len(archive) % 512)
    return gzip.compress(bytes(archive), compresslevel=9, mtime=0)


# --------------------------------------------------------------------------
# FITs
# --------------------------------------------------------------------------

def linux_fit(description, kernel, dtb, initrd, unit, rootfs):
    def image(name, path, kind, load, extra=''):
        return f'''        {name} {{
            description = "{description} {name}";
            data = /incbin/("{path}");
            type = "{kind}"; arch = "arm64";{extra} compression = "none";
            load = <{load:#x}>;
            hash-1 {{ algo = "sha256"; }};
        }};
'''
    digest = hashlib.sha256(rootfs).hexdigest()
    return ('/dts-v1/;\n/ {\n'
            f'    description = "{description}";\n    #address-cells = <1>;\n'
            '    images {\n'
            + image('kernel', kernel, 'kernel', KERNEL_LOAD,
                    f' os = "linux"; entry = <{KERNEL_LOAD:#x}>;')
            + image('fdt', dtb, 'flat_dt', FDT_LOAD)
            + image('ramdisk', initrd, 'ramdisk', INITRD_LOAD, ' os = "linux";')
            + '    };\n    configurations {\n        default = "conf-sdr";\n'
            '        conf-sdr {\n'
            '            kernel = "kernel"; fdt = "fdt"; ramdisk = "ramdisk";\n'
            f'            sdr,unit = "{unit}";\n'
            f'            sdr,rootfs-sha256 = "{digest}";\n'
            f'            sdr,rootfs-size = <{len(rootfs)}>;\n'
            '        };\n    };\n};\n')


def check_layout(fit, kernel, initrd_bytes):
    """The FIT, the running kernel and the initrd must not overlap each other."""
    require(kernel[56:60] == b'ARM\x64', 'the kernel is not an ARM64 Image')
    text_offset, runtime, flags = struct.unpack_from('<QQQ', kernel, 8)
    require(not flags & 1 and KERNEL_LOAD % 0x200000 == text_offset % 0x200000,
            'the kernel would relocate away from its load address')
    require(KERNEL_LOAD + runtime <= FIT_LOAD,
            f'the running kernel ({runtime} bytes) reaches the FIT staging area')
    require(FIT_LOAD + fit.stat().st_size <= FIT_END,
            f'{fit.name} ({fit.stat().st_size} bytes) overruns its staging area')
    require(INITRD_LOAD + initrd_bytes <= INITRD_END,
            f'the {fit.name} initrd ({initrd_bytes} bytes) overruns its load window')


def check_fit(path, payloads, unit, rootfs):
    """Each image carries exactly the bytes it was built from, and the SDR
    configuration attests this unit and root."""
    tree = fdt.nodes(path.read_bytes())
    for name, data in payloads.items():
        require(tree[f'/images/{name}']['data'] == data,
                f'{path.name}: the {name} image differs from its input')
        require(fdt.string(tree[f'/images/{name}/hash-1']['algo']) == 'sha256',
                f'{path.name}: the {name} image is not SHA-256 hashed')
    conf = tree['/configurations/conf-sdr']
    require(fdt.string(conf['sdr,unit']) == unit,
            f'{path.name}: sdr,unit is not {unit}')
    require(fdt.string(conf['sdr,rootfs-sha256']) == hashlib.sha256(rootfs).hexdigest(),
            f'{path.name}: sdr,rootfs-sha256 differs from the root')
    require(fdt.cells(conf['sdr,rootfs-size']) == [len(rootfs)],
            f'{path.name}: sdr,rootfs-size differs from the root')


def build_fit(path, description, initrd, unit, rootfs):
    uboot.make_fit(linux_fit(description, KERNEL_IMAGE, DTB, initrd, unit, rootfs), path,
                   LINUX_OUT / 'scripts/dtc', LOG)
    check_fit(path, {'kernel': KERNEL_IMAGE.read_bytes(), 'fdt': DTB.read_bytes(),
                     'ramdisk': initrd.read_bytes()}, unit, rootfs)
    check_layout(path, KERNEL_IMAGE.read_bytes(), initrd.stat().st_size)


# --------------------------------------------------------------------------
# The data UBIFS
# --------------------------------------------------------------------------

def _node_crc(node):
    return zlib.crc32(node[8:]) ^ 0xffffffff


def normalize_ubifs(data):
    """Fix the UBIFS UUID and zero every inode timestamp, rewriting node CRCs."""
    image = bytearray(data)
    magic = struct.pack('<I', 0x06101831)
    require(image[:4] == magic, 'data: not a UBIFS image')
    image[108:124] = DATA_UUID.bytes
    struct.pack_into('<I', image, 4, _node_crc(image[:struct.unpack_from('<I', image, 16)[0]]))
    cursor, inodes = 4, 0
    while (offset := image.find(magic, cursor)) >= 0:
        cursor = offset + 4
        if offset % 8 or offset + 24 > len(image):
            continue
        length = struct.unpack_from('<I', image, offset + 16)[0]
        if image[offset + 20] != 0 or length < 160 or offset + length > len(image):
            continue      # not an inode node
        node = image[offset:offset + length]
        require(struct.unpack_from('<I', node, 4)[0] == _node_crc(node),
                'data: an inode node has a bad CRC')
        image[offset + 56:offset + 92] = bytes(36)
        struct.pack_into('<I', image, offset + 4, _node_crc(image[offset:offset + length]))
        inodes += 1
    require(inodes, 'data: the image has no inode')
    return bytes(image)


def data_image(nand):
    return IMAGES / f'data-{nand.name.lower()}.ubifs'


def data(nand):
    """An empty persistent data volume: overlay upper/work, logs, recordings."""
    mkfs = BUILDROOT_OUT / 'host/sbin/mkfs.ubifs'
    require(mkfs.is_file(), f'{mkfs} is missing; Buildroot builds it (BR2_PACKAGE_HOST_MTD)')
    with tempfile.TemporaryDirectory(prefix='data-', dir=BUILD) as temporary:
        tree = Path(temporary) / 'tree'
        for name in ('overlay/upper', 'overlay/work', 'logs', 'recordings', 'spectrumd'):
            (tree / name).mkdir(parents=True)
        os.utime(tree, (0, 0))
        raw = Path(temporary) / 'data.ubifs'
        env = os.environ.copy()
        env.update(REPRODUCIBLE_ENV)
        run([mkfs, '-U', '-r', tree, '-m', hex(nand.page), '-e', hex(nand.leb),
             '-c', str(partitions.v2_data_lebs(nand)), '-x', 'zstd', '-o', raw],
            env=env, log=LOG)
        write_if_changed(data_image(nand), normalize_ubifs(raw.read_bytes()))


def main():
    for path in (KERNEL_IMAGE, DTB, ROOTFS_IMAGE, BUSYBOX, MKIMAGE):
        require(path.is_file(), f'{path} is missing; build it first (see the top-level Makefile)')
    IMAGES.mkdir(parents=True, exist_ok=True)
    root_path = Path(os.environ.get('W1700K_ROOTFS', ROOTFS_IMAGE))
    require(root_path.is_file(), f'{root_path} is missing')
    root = root_path.read_bytes()
    unit = os.environ.get('W1700K_UNIT', 'UNPROVISIONED')
    write_if_changed(ROOTFS, root)
    write_if_changed(BOOTSTRAP, bootstrap(root))
    write_if_changed(RECOVERY_INITRD, bootstrap(root))
    build_fit(SYSTEM_FIT, 'W1700K SDR system', BOOTSTRAP, unit, root)
    require(SYSTEM_FIT.stat().st_size <= partitions.V2_FIT_BYTES,
            f'the system FIT ({SYSTEM_FIT.stat().st_size} bytes) exceeds the 32 MiB slot')
    build_fit(RECOVERY_FIT, 'W1700K SDR recovery', RECOVERY_INITRD, unit, root)
    require(RECOVERY_FIT.stat().st_size <= partitions.V2_RECOVERY_BYTES,
            f'the recovery FIT ({RECOVERY_FIT.stat().st_size} bytes) exceeds 20 MiB')
    for nand in partitions.NANDS:
        data(nand)
    for path in (SYSTEM_FIT, ROOTFS, RECOVERY_FIT,
                 *(data_image(nand) for nand in partitions.NANDS)):
        print(f'images: {path} ({path.stat().st_size} bytes)')
    return 0


if __name__ == '__main__':
    main_guard(main)
