# SPDX-License-Identifier: MIT
"""Root login defaults: password auth plus optional authorized keys."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / 'platform/rootfs/config.seed'
DROPBEAR = ROOT / 'platform/rootfs/overlay/etc/default/dropbear'


class RootLogin(unittest.TestCase):
    def test_dropbear_accepts_passwords_and_uses_the_persistent_host_key(self):
        text = DROPBEAR.read_text()
        self.assertNotIn('-s ', text)
        self.assertIn('-r /etc/dropbear/dropbear_ed25519_host_key', text)

    def test_buildroot_sets_the_default_root_password(self):
        text = SEED.read_text()
        self.assertIn('BR2_TARGET_ENABLE_ROOT_LOGIN=y', text)
        self.assertIn('BR2_TARGET_GENERIC_ROOT_PASSWD="123456"', text)


if __name__ == '__main__':
    unittest.main()
