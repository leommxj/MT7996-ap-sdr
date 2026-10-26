# SPDX-License-Identifier: MIT
"""The packaging steps (platform/images.py, loader, dumper) and, once `make` has
run, the images themselves."""
import gzip
import hashlib
import importlib.util
import lzma
from pathlib import Path
import struct
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'platform'))
import buildlib     # noqa: E402
import fdt          # noqa: E402
import images       # noqa: E402
import partitions   # noqa: E402
import uboot        # noqa: E402

IMAGES = ROOT / 'platform/build/images'


def cpio_entries(archive):
    """{name: (mode, data)} of a newc archive."""
    data = gzip.decompress(archive)
    entries, offset = {}, 0
    while True:
        assert data[offset:offset + 6] == b'070701'
        fields = [int(data[offset + 6 + 8 * i:offset + 14 + 8 * i], 16) for i in range(13)]
        mode, size, name_size = fields[1], fields[6], fields[11]
        name_at = offset + 110
        name = data[name_at:name_at + name_size - 1].decode()
        body_at = name_at + name_size + (-(110 + name_size) % 4)
        if name == 'TRAILER!!!':
            return entries
        entries[name] = (mode, data[body_at:body_at + size])
        offset = body_at + size + (-size % 4)


class Bootstrap(unittest.TestCase):
    def setUp(self):
        busybox = mock.patch.object(images, 'BUSYBOX', mock.Mock(**{'read_bytes.return_value':
                                                                     b'busybox'}))
        busybox.start()
        self.addCleanup(busybox.stop)

    def test_the_initramfs_holds_only_what_mounting_the_root_needs(self):
        entries = cpio_entries(images.bootstrap())
        self.assertEqual(set(entries), {'bin', 'data', 'dev', 'lower', 'newroot', 'proc',
                                        'ram', 'sbin', 'sys', 'init', 'bin/busybox',
                                        'bin/sh', 'sbin/switch_root', 'dev/console'})
        self.assertEqual(entries['init'][1], (ROOT / 'platform/bootstrap/init').read_bytes())
        self.assertEqual(entries['bin/sh'][1], b'busybox')

    def test_the_recovery_initramfs_carries_the_root(self):
        entries = cpio_entries(images.bootstrap(b'hsqs-root'))
        self.assertEqual(entries['rootfs.squashfs'][1], b'hsqs-root')

    def test_archives_are_reproducible(self):
        self.assertEqual(images.bootstrap(), images.bootstrap())


class Fits(unittest.TestCase):
    def test_linux_fit_source_names_one_configuration(self):
        its = images.linux_fit('test', '/k', '/d', '/r', 'UNIT', b'root')
        self.assertIn('default = "conf-sdr";', its)
        self.assertEqual(its.count('algo = "sha256";'), 3)
        self.assertIn('sdr,unit = "UNIT";', its)
        self.assertIn('sdr,rootfs-sha256 = "', its)
        self.assertIn(f'load = <{images.KERNEL_LOAD:#x}>; ', its.replace('\n', ' '))

    def test_stock_fits_carry_sha1_only(self):
        its = uboot.stock_fit('test', 'loader', 'payload', 'lzma', 'dtb')
        self.assertEqual(its.count('algo = "sha1";'), 2)
        self.assertNotIn('sha256', its)
        self.assertIn('kernel = "loader"', its)

    def test_the_loader_payload_uses_the_vendor_lzma_header(self):
        spec = importlib.util.spec_from_file_location('loader_build',
                                                      ROOT / 'platform/loader/build.py')
        loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loader)
        image = bytes(range(256)) * 64
        payload = loader.compress(image)
        self.assertEqual(struct.unpack_from('<I', payload, 1)[0], 0x800000)
        self.assertEqual(struct.unpack_from('<Q', payload, 5)[0], len(image))
        self.assertEqual(lzma.decompress(payload, format=lzma.FORMAT_ALONE), image)


class Data(unittest.TestCase):
    def test_normalisation_fixes_identity_and_timestamps(self):
        magic = struct.pack('<I', 0x06101831)
        superblock = bytearray(4096)
        superblock[:4] = magic
        struct.pack_into('<I', superblock, 16, 4096)
        inode = bytearray(160)
        inode[:4] = magic
        struct.pack_into('<I', inode, 16, 160)
        inode[56:92] = b'\x11' * 36
        struct.pack_into('<I', inode, 4, images._node_crc(inode))
        image = bytes(superblock) + bytes(inode) + bytes(-(4096 + 160) % 0x1f000)
        normal = images.normalize_ubifs(image)
        self.assertEqual(normal[108:124], images.DATA_UUID.bytes)
        node = normal[4096:4096 + 160]
        self.assertEqual(node[56:92], bytes(36))
        self.assertEqual(struct.unpack_from('<I', node, 4)[0], images._node_crc(node))
        self.assertEqual(images.normalize_ubifs(normal), normal)

    def test_an_inode_with_a_bad_crc_is_refused(self):
        magic = struct.pack('<I', 0x06101831)
        superblock = bytearray(4096)
        superblock[:4] = magic
        struct.pack_into('<I', superblock, 16, 4096)
        inode = bytearray(160)
        inode[:4] = magic
        struct.pack_into('<I', inode, 16, 160)
        with self.assertRaisesRegex(buildlib.BuildError, 'bad CRC'):
            images.normalize_ubifs(bytes(superblock) + bytes(inode))


def built(*names):
    paths = [IMAGES / name for name in names]
    return unittest.skipUnless(all(path.is_file() for path in paths),
                               'the images are not built (run make)')


class BuiltImages(unittest.TestCase):
    @built('w1700k-system.itb', 'rootfs.squashfs')
    def test_the_system_fit_carries_the_built_kernel_root_and_attestations(self):
        tree = fdt.nodes((IMAGES / 'w1700k-system.itb').read_bytes())
        self.assertEqual(tree['/images/kernel']['data'], buildlib.KERNEL_IMAGE.read_bytes())
        self.assertEqual(tree['/images/fdt']['data'], buildlib.DTB.read_bytes())
        entries = cpio_entries(tree['/images/ramdisk']['data'])
        root = (IMAGES / 'rootfs.squashfs').read_bytes()
        self.assertEqual(entries['rootfs.squashfs'][1], root)
        for name in ('kernel', 'fdt', 'ramdisk'):
            node = tree[f'/images/{name}']
            self.assertEqual(tree[f'/images/{name}/hash-1']['value'],
                             hashlib.sha256(node['data']).digest())
        conf = tree['/configurations/conf-sdr']
        self.assertEqual(fdt.string(conf['sdr,rootfs-sha256']), hashlib.sha256(root).hexdigest())
        self.assertEqual(fdt.cells(conf['sdr,rootfs-size']), [len(root)])
        self.assertLessEqual((IMAGES / 'w1700k-system.itb').stat().st_size,
                             partitions.V2_FIT_BYTES)
        self.assertLessEqual((IMAGES / 'rootfs.squashfs').stat().st_size,
                             partitions.V2_FIT_BYTES)

    @built('w1700k-recovery.itb', 'rootfs.squashfs')
    def test_the_recovery_fit_carries_the_root(self):
        tree = fdt.nodes((IMAGES / 'w1700k-recovery.itb').read_bytes())
        entries = cpio_entries(tree['/images/ramdisk']['data'])
        self.assertEqual(entries['rootfs.squashfs'][1], (IMAGES / 'rootfs.squashfs').read_bytes())

    @built('w1700k-loader.itb')
    def test_the_loader_fit_is_bootable_by_stock(self):
        data = (IMAGES / 'w1700k-loader.itb').read_bytes()
        tree = fdt.nodes(data)
        kernel = tree['/images/loader']
        self.assertEqual(fdt.string(kernel['compression']), 'lzma')
        self.assertEqual(fdt.cells(kernel['load']), [uboot.ENTRY])
        self.assertEqual(tree['/images/loader/hash-1']['value'],
                         hashlib.sha1(kernel['data']).digest())
        self.assertLessEqual(len(data), partitions.GEOMETRY['loader'][1])

    @built('w1700k-dumper.itb')
    def test_the_dumper_fit_is_small_enough_for_stock_tftp(self):
        data = (IMAGES / 'w1700k-dumper.itb').read_bytes()
        tree = fdt.nodes(data)
        self.assertEqual(fdt.string(tree['/images/dumper']['compression']), 'gzip')
        self.assertLessEqual(-(-len(data) // 32768), 6)

    @built(*(images.data_image(nand).name for nand in partitions.NANDS))
    def test_each_part_has_a_normalised_data_image(self):
        for nand in partitions.NANDS:
            with self.subTest(nand.name):
                data = images.data_image(nand).read_bytes()
                self.assertEqual(images.normalize_ubifs(data), data)
                self.assertLessEqual(len(data), partitions.v2_data_lebs(nand) * nand.leb)
                # The superblock records the part's minimum I/O and LEB sizes.
                sb = data[:4096]
                self.assertEqual(struct.unpack_from('<II', sb, 0x20), (nand.page, nand.leb))


if __name__ == '__main__':
    unittest.main()
