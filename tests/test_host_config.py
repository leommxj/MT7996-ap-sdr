# SPDX-License-Identifier: MIT
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'host'))
from w1700k import config


class MacAddress(unittest.TestCase):
    def lookup(self, ip, text):
        with mock.patch.object(config.sys, 'platform', 'darwin'), mock.patch.object(
                config.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, text, '')):
            return config.local_mac_for(ip)

    def test_mac_matches_address_and_alias_on_the_right_interface(self):
        text = ('lo0: flags=8049<UP> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n'
                'en0: flags=8863<UP> mtu 1500\n\tether 00:11:22:33:44:55\n'
                '\tinet 10.77.0.110 netmask 0xffffff00\n'
                'en9: flags=8863<UP> mtu 9216\n\tether AA:BB:CC:DD:EE:FF\n'
                '\tinet 192.168.0.2 netmask 0xffffff00\n\tinet 10.77.0.11 netmask 0xffffff00\n')
        self.assertEqual(self.lookup('10.77.0.11', text), 'aa:bb:cc:dd:ee:ff')
        with self.assertRaisesRegex(config.ConfigError, 'no local interface'):
            self.lookup('10.77.0.1', text)
        with self.assertRaisesRegex(config.ConfigError, 'no Ethernet MAC'):
            self.lookup('127.0.0.1', text)

    def test_mac_reports_ifconfig_failure(self):
        with mock.patch.object(config.sys, 'platform', 'darwin'), mock.patch.object(
                config.subprocess, 'run', side_effect=OSError('ifconfig unavailable')):
            with self.assertRaisesRegex(config.ConfigError, 'set export.dst_mac'):
                config.local_mac_for('10.77.0.11')
