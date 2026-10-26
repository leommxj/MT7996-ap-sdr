# SPDX-License-Identifier: MIT
"""The v2 network defaults: WAN1 static, other ports DHCP clients."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / 'platform/rootfs/overlay/etc/w1700k/network.conf'
NET = ROOT / 'platform/rootfs/overlay/usr/sbin/w1700k-network'
BOOTGOOD = ROOT / 'platform/rootfs/overlay/usr/sbin/bootgood'
FRAGMENT = ROOT / 'platform/rootfs/busybox.fragment'
LEASE = ROOT / 'platform/rootfs/overlay/etc/udhcpc/default.script'


def values():
    found = {}
    for line in CONF.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            found[key] = value.strip('"')
    return found


class Network(unittest.TestCase):
    def test_wan1_keeps_a_static_direct_address(self):
        conf = values()
        self.assertEqual(conf['EXPORT_IF'], 'eth1')
        self.assertEqual(conf['EXPORT_ADDRESS'], '10.77.0.1/24')
        # SDR export needs jumbo frames; the laptop side must match.
        self.assertEqual(conf['EXPORT_MTU'], '9216')

    def test_other_external_ports_are_dhcp_clients(self):
        conf = values()
        self.assertEqual(conf['DHCP_IFS'].split(), ['lan3', 'lan4', 'eth2'])
        self.assertIn('eth1', conf['HEALTH_IFS'].split())
        text = NET.read_text()
        self.assertIn('DHCP_IFS', text)
        self.assertIn('udhcpc', text)
        self.assertTrue(LEASE.is_file())

    def test_busybox_has_the_dhcp_client_and_bootgood_checks_health(self):
        fragment = FRAGMENT.read_text()
        self.assertIn('CONFIG_UDHCPC=y', fragment)
        self.assertNotIn('# CONFIG_UDHCPC is not set', fragment)
        text = BOOTGOOD.read_text()
        self.assertIn('HEALTH_IFS', text)
        self.assertIn('ip addr show', text)
        self.assertIn('inet ', text)


if __name__ == '__main__':
    unittest.main()
