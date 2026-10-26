#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The SPI-NAND layout: the parts, partitions, UBI volumes and the BMT write policy.

This is the one definition. The old loader's DTS and the v2 chainloader/Linux
DTS carry regions generated from it; the BMT layer compiles a header from the
vendor-logical view, and the image build and installer size their volumes.

The board carries one of two 512 MiB parts: the W25N04KV (2048-byte pages,
128 KiB blocks) or, on some W1700K2s, the W25N04LV (4096-byte pages, 256 KiB
blocks). The partitions are the same bytes on both; the UBI volumes are sized
per part. Only the W25N04KV has been run on a unit.

    platform/partitions.py            # check the device trees are current
    platform/partitions.py --write    # regenerate their partition regions

The old GEOMETRY view is vendor-logical (stock U-Boot, the old loader and the
backup tools). V2_GEOMETRY is raw physical: the chainloader and v2 Linux use
standard UBI and never translate through the vendor BMT.
"""
import argparse
from pathlib import Path
import sys
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
CHIP_BYTES = 0x20000000


class Nand(NamedTuple):
    """One SPI-NAND part the board may carry."""
    name: str
    id: bytes            # the READ ID bytes: maker, device
    page: int            # main bytes per page
    oob: int             # OOB bytes per page
    block: int           # bytes per eraseblock (64 pages)
    tested: bool         # run on a unit by this project

    @property
    def blocks(self):
        return CHIP_BYTES // self.block

    @property
    def leb(self):
        """A UBI LEB: the block less its two header pages (no subpage writes)."""
        return self.block - 2 * self.page

    @property
    def durable_image(self):
        """The file name of the empty `durable` UBIFS built for this part."""
        return f'durable-{self.name.lower()}.ubifs'


NANDS = (
    Nand('W25N04KV', bytes.fromhex('efaa23'), 0x800, 128, 0x20000, tested=True),
    Nand('W25N04LV', bytes.fromhex('ef8b23'), 0x1000, 256, 0x40000, tested=False),
)
KV = NANDS[0]
# Every partition boundary falls on a block of every part.
ALIGNMENT = max(nand.block for nand in NANDS)


def nand_by_id(ident):
    """The part with these ID bytes, or None."""
    return next((nand for nand in NANDS if nand.id == bytes(ident)), None)


def nand_by_geometry(page, block, oob):
    """The part with this geometry, or None."""
    return next((nand for nand in NANDS
                 if (nand.page, nand.block, nand.oob) == (page, block, oob)), None)


# (offset, length), ascending and contiguous over the whole chip.
GEOMETRY = {
    'bootloader': (0x00000000, 0x00200000),   # vendor: BL2/BL31 and stock U-Boot
    'uenv': (0x00200000, 0x00200000),         # vendor: stock U-Boot environment
    'calibration': (0x00400000, 0x00200000),  # vendor: identity and radio calibration
    'loader': (0x00600000, 0x00200000),       # ours: our U-Boot FIT, read by stock
    'system': (0x00800000, 0x03000000),       # ours: UBI with environment, FIT, root
    'durable': (0x03800000, 0x02000000),      # ours: UBI with the durable UBIFS
    'scratch': (0x05800000, 0x16600000),      # ours: unused, never erased
    'reserved-bmt': (0x1be00000, 0x04200000), # vendor: BMT reserve pool
}

# Read-only partitions per stage. The loader can rewrite everything we own; Linux
# keeps `system` and `scratch` read-only and opens `durable` only on request.
READ_ONLY = {
    'loader': {'bootloader', 'uenv', 'calibration', 'loader', 'reserved-bmt'},
    'linux': {'bootloader', 'uenv', 'calibration', 'loader', 'system', 'scratch',
              'reserved-bmt'},
}
DEVICE_TREES = {
    'platform/loader/an7581-w1700k-sdr-loader.dts': 'loader',
}

# The installed v2 layout is raw physical, not vendor-logical: the vendor BMT
# is not used by the chainloader or by the v2 kernel. Stock's `flash` commands
# and the backup/restore tools continue to use GEOMETRY above.
V2_GEOMETRY = {
    'vendor': (0x00000000, 0x00600000),        # stock BL2/BL31, uenv, calibration
    'chainloader': (0x00600000, 0x00100000),   # our U-Boot, written by stock
    'ubi': (0x00700000, 0x1b700000),           # 439 MiB standard UBI
    'reserved_bmt': (0x1be00000, 0x04200000),  # vendor BMT pool, never touched
}
V2_ALIGNMENT = ALIGNMENT
V2_READ_ONLY = {
    'chainloader': {'vendor', 'chainloader', 'reserved_bmt'},
    'linux-v2': {'vendor', 'chainloader', 'reserved_bmt'},
}
V2_DEVICE_TREES = {
    'platform/chainloader/an7581-w1700k-ubi.dts': 'chainloader',
    'platform/linux/dts/an7581-w1700k-sdr.dts': 'linux-v2',
}
V2_CHAINLOADER_OFFSET, V2_CHAINLOADER_BYTES = V2_GEOMETRY['chainloader']
V2_UBI_OFFSET, V2_UBI_BYTES = V2_GEOMETRY['ubi']
V2_UBI_END = V2_UBI_OFFSET + V2_UBI_BYTES
V2_RESERVED_OFFSET, V2_RESERVED_BYTES = V2_GEOMETRY['reserved_bmt']
V2_FIT_BYTES = 0x2000000          # 32 MiB per A/B slot
V2_RECOVERY_BYTES = 0x1400000     # 20 MiB
BEGIN = '\t\t// >>> generated by platform/partitions.py; edit that, not this'
END = '\t\t// <<< end generated partition table'

# The parts' worst case (NAND_MEMORG's max_bad_eraseblocks_per_lun for both).
MAX_BAD_BLOCKS = 40


def least_usable(nand):
    """The fewest logical blocks the vendor tables can translate on `nand`: the
    pool is 250 where the clamp fixes it (W25N04KV), else total*8/100 plus the
    factory-bad blocks the scan steps over (W25N04LV), and the BBT holds the
    factory-bad blocks below it; together at most MAX_BAD_BLOCKS."""
    floor = nand.blocks * 8 // 100
    return nand.blocks - (250 if floor > 250 else floor) - MAX_BAD_BLOCKS


# The BMT transaction journal: the block after the BBT, which the vendor puts
# on the reserve's first good block and never moves (3847 on every W25N04KV
# seen). The engine checks at attach time that it is erased or holds its own
# journal, and that it is neither a table nor a live replacement; it refuses
# writes when a good block lies between the reserve's start and the BBT.
JOURNAL_AFTER_BBT = 1

# UBI. Both U-Boot and Linux reserve 20 PEBs per 1024 of the whole chip for bad
# blocks, and four more for the layout volume, wear levelling and atomic LEB change.
UBI_INTERNAL_PEBS = 4
ENV_BYTES = 0x4000
# The FIT volume holds the same bytes on both parts: 128 W25N04KV LEBs.
FIT_VOLUME_BYTES = 128 * KV.leb


def ubi_bad_reserve(nand):
    return nand.blocks * 20 // 1024


def ubi_lebs(partition, nand):
    return GEOMETRY[partition][1] // nand.block - ubi_bad_reserve(nand) - UBI_INTERNAL_PEBS


def system_volumes(nand):
    """`system` volumes in id order: (name, type, LEBs). The root takes what is left."""
    fit = -(-FIT_VOLUME_BYTES // nand.leb)
    return (('ubootenv', 'dynamic', 1),
            ('ubootenv2', 'dynamic', 1),
            ('fit', 'static', fit),
            ('rootfs', 'static', ubi_lebs('system', nand) - 2 - fit))


def durable_lebs(nand):
    return ubi_lebs('durable', nand)


# The root must fit the volume of every part.
ROOTFS_VOLUME_BYTES = min(system_volumes(nand)[3][2] * nand.leb for nand in NANDS)


# --------------------------------------------------------------------------
# The v2 raw layout
# --------------------------------------------------------------------------

def v2_ubi_lebs(nand):
    """Usable LEBs in the 439 MiB UBI partition, after UBI's own reserve."""
    return V2_UBI_BYTES // nand.block - ubi_bad_reserve(nand) - UBI_INTERNAL_PEBS


def v2_fit_lebs(nand):
    return -(-V2_FIT_BYTES // nand.leb)


def v2_recovery_lebs(nand):
    return -(-V2_RECOVERY_BYTES // nand.leb)


def v2_data_lebs(nand):
    fit, recovery = v2_fit_lebs(nand), v2_recovery_lebs(nand)
    return v2_ubi_lebs(nand) - 2 - 2 * fit - recovery


def v2_volumes(nand):
    """`ubi` volumes in id order: (name, type, LEBs)."""
    fit, recovery = v2_fit_lebs(nand), v2_recovery_lebs(nand)
    return (('ubootenv', 'dynamic', 1),
            ('ubootenv2', 'dynamic', 1),
            ('fit_a', 'static', fit),
            ('fit_b', 'static', fit),
            ('recovery', 'static', recovery),
            ('data', 'dynamic', v2_data_lebs(nand)))


def v2_check_table():
    """Contiguous, aligned raw partitions; the UBI volume table must add up."""
    cursor = 0
    for name, (offset, length) in V2_GEOMETRY.items():
        if offset != cursor:
            raise ValueError(f'v2: {name} leaves a gap or overlaps at {offset:#x}')
        if not length or length % V2_ALIGNMENT or offset % V2_ALIGNMENT:
            raise ValueError(f'v2: {name} is not block-aligned')
        cursor = offset + length
    if cursor != CHIP_BYTES:
        raise ValueError(f'v2: the table covers {cursor:#x}, not {CHIP_BYTES:#x}')
    for stage, names in V2_READ_ONLY.items():
        if not names <= set(V2_GEOMETRY):
            raise ValueError(f'v2: {stage} names an unknown partition')
    for nand in NANDS:
        fit, recovery = v2_fit_lebs(nand), v2_recovery_lebs(nand)
        data = v2_data_lebs(nand)
        if data < 1:
            raise ValueError(f'v2: the data volume is empty on the {nand.name}')
        total = 2 + 2 * fit + recovery + data
        if total != v2_ubi_lebs(nand):
            raise ValueError(f'v2: volumes add to {total}, not {v2_ubi_lebs(nand)} '
                             f'on the {nand.name}')
        if fit * nand.leb < V2_FIT_BYTES or recovery * nand.leb < V2_RECOVERY_BYTES:
            raise ValueError(f'v2: a static volume is too small on the {nand.name}')


def check_table():
    """Contiguous, ascending, aligned to every part's blocks, covering the chip."""
    cursor = 0
    for name, (offset, length) in GEOMETRY.items():
        if offset != cursor:
            raise ValueError(f'{name} leaves a gap or overlaps at {offset:#x}')
        if not length or length % ALIGNMENT or offset % ALIGNMENT:
            raise ValueError(f'{name} is not block-aligned')
        cursor = offset + length
    if cursor != CHIP_BYTES:
        raise ValueError(f'the table covers {cursor:#x}, not {CHIP_BYTES:#x}')
    for stage, names in READ_ONLY.items():
        if not names <= set(GEOMETRY):
            raise ValueError(f'{stage} names an unknown partition')
    for nand in NANDS:
        if system_volumes(nand)[3][2] < 1:
            raise ValueError(f'the system volumes leave no root on the {nand.name}')
        if GEOMETRY['reserved-bmt'][0] > least_usable(nand) * nand.block:
            raise ValueError(f'the partitions pass the vendor usable area on the {nand.name}')


def policy_header(first_partition='system'):
    """airoha_bmt_policy.h: the bytes the BMT engine may write, from
    `first_partition` to the end of `scratch`. Our loader and Linux write only
    what we own; the restorer also writes back stock's `uenv` and the stock
    images over ours (and is told never to touch `calibration`). The engine
    converts them to blocks of the part it finds."""
    check_table()
    first = GEOMETRY[first_partition][0]
    end = sum(GEOMETRY['scratch'])
    return ('/* Generated by platform/partitions.py; do not edit. */\n'
            '#ifndef AIROHA_BMT_POLICY_H\n#define AIROHA_BMT_POLICY_H\n'
            f'#define BMT_WRITE_FIRST_BYTES {first:#x}ull\n'
            f'#define BMT_WRITE_END_BYTES {end:#x}ull\n'
            f'#define BMT_MIN_BLOCK_BYTES {min(nand.block for nand in NANDS):#x}u\n'
            f'#define BMT_JOURNAL_AFTER_BBT {JOURNAL_AFTER_BBT}u\n'
            '#endif\n')


def fragment(stage):
    """The device-tree partition nodes for one stage."""
    check_table()
    lines = [BEGIN, '\t\t// Vendor-logical offsets; BBT shift + BMT substitution exactly once.']
    for name, (offset, length) in GEOMETRY.items():
        lines += [f'\t\t{name}@{offset:x} {{', f'\t\t\tlabel = "{name}";',
                  f'\t\t\treg = <{offset:#010x} {length:#010x}>;']
        if name in READ_ONLY[stage]:
            lines.append('\t\t\tread-only;')
        lines += ['\t\t};', '']
    lines[-1] = END
    return '\n'.join(lines) + '\n'


def v2_fragment(stage):
    """The raw v2 partition nodes for the chainloader or the v2 Linux DTS."""
    v2_check_table()
    lines = [BEGIN, '\t\t// Raw physical offsets; standard UBI, no vendor BMT.']
    for name, (offset, length) in V2_GEOMETRY.items():
        lines += [f'\t\t{name}@{offset:x} {{', f'\t\t\tlabel = "{name}";',
                  f'\t\t\treg = <{offset:#010x} {length:#010x}>;']
        if name in V2_READ_ONLY[stage]:
            lines.append('\t\t\tread-only;')
        lines += ['\t\t};', '']
    lines[-1] = END
    return '\n'.join(lines) + '\n'


def _region(text, path):
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ValueError(f'{path}: expected one generated partition region')
    start = text.index(BEGIN)
    return start, text.index(END, start) + len(END) + 1


def check(path, stage):
    text = (ROOT / path).read_text()
    start, end = _region(text, path)
    if text[start:end] != fragment(stage):
        raise ValueError(f'{path} has a stale partition table; '
                         'run platform/partitions.py --write')


def write(path, stage):
    target = ROOT / path
    text = target.read_text()
    start, end = _region(text, path)
    updated = text[:start] + fragment(stage) + text[end:]
    if updated != text:
        target.write_text(updated)
        return True
    return False


def v2_check(path, stage):
    text = (ROOT / path).read_text()
    start, end = _region(text, path)
    if text[start:end] != v2_fragment(stage):
        raise ValueError(f'{path} has a stale v2 partition table; '
                         'run platform/partitions.py --write')


def v2_write(path, stage):
    target = ROOT / path
    text = target.read_text()
    start, end = _region(text, path)
    updated = text[:start] + v2_fragment(stage) + text[end:]
    if updated != text:
        target.write_text(updated)
        return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--write', action='store_true',
                        help='regenerate the device-tree partition regions')
    args = parser.parse_args(argv)
    try:
        for path, stage in DEVICE_TREES.items():
            if args.write:
                if write(path, stage):
                    print(f'partitions: rewrote {path}')
            else:
                check(path, stage)
        for path, stage in V2_DEVICE_TREES.items():
            if args.write:
                if v2_write(path, stage):
                    print(f'partitions: rewrote {path}')
            else:
                v2_check(path, stage)
    except ValueError as error:
        sys.exit(f'partitions: {error}')
    print(f'partitions: {len(GEOMETRY)} vendor-logical, {len(V2_GEOMETRY)} raw v2; '
          'device trees current')
    return 0


if __name__ == '__main__':
    sys.exit(main())
