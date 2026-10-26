# SPDX-License-Identifier: MIT
"""The chainloader default environment and the v2 rootfs boot hooks."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'platform'))
import partitions  # noqa: E402

ENV = ROOT / 'platform/chainloader/defenvs/an7581_xr1710g_chainloader_env'
BOOTGOOD = ROOT / 'platform/rootfs/overlay/usr/sbin/bootgood'
RCS = ROOT / 'platform/rootfs/overlay/etc/init.d/rcS'


def env():
    found = {}
    for line in ENV.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            found[key] = value
    return found


class ChainloaderEnv(unittest.TestCase):
    def test_the_failed_slot_path_flips_and_clears_trial(self):
        values = env()
        alt = values['boot_alt']
        self.assertIn('bootslot a', alt)
        self.assertIn('bootslot b', alt)
        self.assertIn('upgrade_available 0', alt)
        self.assertIn('saveenv', alt)
        self.assertIn('reset', alt)
        self.assertEqual(values['altbootcmd'], 'run boot_alt')

    def test_the_menu_default_checks_the_trial_before_booting(self):
        values = env()
        self.assertEqual(values['bootmenu_0'], 'Boot current slot=run bootcmd')
        self.assertIn('upgrade_available', values['bootcmd'])
        self.assertIn('bootcount', values['bootcmd'])
        self.assertIn('bootlimit', values['bootcmd'])
        self.assertIn('run boot_alt', values['bootcmd'])
        self.assertIn('run boot_recovery', values['bootcmd'])
        self.assertIn('conf-sdr', values['bootconf'])

    def test_the_chainloader_slot_is_one_mib(self):
        self.assertEqual(partitions.V2_CHAINLOADER_BYTES, 0x100000)


class V2Rootfs(unittest.TestCase):
    def test_var_log_is_made_a_real_directory_before_mounting(self):
        text = RCS.read_text()
        self.assertIn('if [ -L /var/log ]; then rm -f /var/log; fi', text)
        self.assertLess(text.index('rm -f /var/log'), text.index('mount -t tmpfs'))

    def test_bootgood_waits_for_the_management_link(self):
        text = BOOTGOOD.read_text()
        self.assertIn('operstate', text)
        self.assertIn('sleep 0.5', text)
        self.assertIn('W1700K_DATA_DEGRADED', text)
        self.assertIn('W1700K_SLOT_NOT_GOOD', text)


if __name__ == '__main__':
    unittest.main()
