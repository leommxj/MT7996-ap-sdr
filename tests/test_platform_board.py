# SPDX-License-Identifier: MIT
"""The FDT reader, and the board checks against the compiled device tree."""
import copy
from pathlib import Path
import struct
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'platform'), str(ROOT / 'platform/linux')]
import board_check  # noqa: E402
import fdt    # noqa: E402

DTB = ROOT / 'platform/build/linux/sdr/an7581-w1700k-sdr.dtb'


def minimal_fdt():
    """A root node with one cell property, `value = <0x12345678>`."""
    structure = struct.pack('>IIIII', 1, 0, 3, 4, 0) + struct.pack('>III', 0x12345678, 2, 9)
    names = b'value\0'
    header = struct.pack('>10I', fdt.MAGIC, 56 + len(structure) + len(names), 56,
                         56 + len(structure), 40, 17, 16, 0, len(names), len(structure))
    return header + bytes(16) + structure + names


class Reader(unittest.TestCase):
    def test_properties_are_big_endian_cells(self):
        tree = fdt.nodes(minimal_fdt())
        self.assertEqual(fdt.cells(tree['/']['value']), [0x12345678])

    def test_truncated_or_inconsistent_headers_are_refused(self):
        data = minimal_fdt()
        for size in (0, 39, 55, len(data) - 1):
            with self.subTest(size=size), self.assertRaises(fdt.FdtError):
                fdt.nodes(data[:size])
        for offset, value in ((8, 55), (12, 57), (16, 41), (36, 0xffffffff)):
            changed = bytearray(data)
            struct.pack_into('>I', changed, offset, value)
            with self.subTest(offset=offset), self.assertRaises(fdt.FdtError):
                fdt.nodes(bytes(changed))

    def test_bad_tokens_and_property_bounds_are_refused(self):
        data = bytearray(minimal_fdt())
        struct.pack_into('>I', data, 56, 99)
        with self.assertRaisesRegex(fdt.FdtError, 'token'):
            fdt.nodes(bytes(data))
        data = bytearray(minimal_fdt())
        struct.pack_into('>I', data, 72, 0xffffffff)
        with self.assertRaisesRegex(fdt.FdtError, 'property bounds'):
            fdt.nodes(bytes(data))


@unittest.skipUnless(DTB.is_file(), 'the device tree is not built (run make)')
class CompiledBoard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = fdt.nodes(DTB.read_bytes())

    def board(self, tree):
        with mock.patch.object(fdt, 'nodes', return_value=tree):
            board_check.board(b'')

    def node(self, tree, label):
        return tree[fdt.string(tree['/__symbols__'][label])]

    def test_the_built_tree_passes(self):
        board_check.board(DTB.read_bytes())

    def test_a_disabled_device_is_caught(self):
        tree = copy.deepcopy(self.tree)
        self.node(tree, 'gdm2')['status'] = b'disabled\0'
        with self.assertRaisesRegex(Exception, 'gdm2'):
            self.board(tree)

    def test_a_moved_partition_is_caught(self):
        tree = copy.deepcopy(self.tree)
        for props in tree.values():
            if props.get('label') == b'ubi\0':
                props['reg'] = struct.pack('>II', 0x700000, 0x3100000)
        with self.assertRaisesRegex(Exception, 'partitions'):
            self.board(tree)

    def test_a_second_bad_block_manager_is_caught(self):
        tree = copy.deepcopy(self.tree)
        self.node(tree, 'spi_nand')['mediatek,bmt-v2'] = b''
        with self.assertRaisesRegex(Exception, 'bad-block manager'):
            self.board(tree)


class LinkedKernel(unittest.TestCase):
    def test_required_drivers_and_forbidden_ones(self):
        system_map = '\n'.join(f'ffff0000 T {name}' for name in board_check.LINKED)
        board_check.linked(system_map)
        with self.assertRaisesRegex(Exception, 'mtk_bmt_attach'):
            board_check.linked(system_map + '\nffff0000 T mtk_bmt_attach')
        with self.assertRaisesRegex(Exception, 'ubi_attach_mtd_dev'):
            board_check.linked(system_map.replace('ubi_attach_mtd_dev', 'other'))
        with self.assertRaisesRegex(Exception, 'rtl8261c'):
            board_check.linked(system_map.replace('_fw_rtl_nic_rtl8261c_bin_bin', 'other'))


if __name__ == '__main__':
    unittest.main()
