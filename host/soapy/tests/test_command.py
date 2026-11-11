# SPDX-License-Identifier: MIT
"""Exercise the real C++ control session against a child that can fail between replies."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CLIENT = r'''
#include "Command.hpp"
#include <nlohmann/json.hpp>
#include <iostream>
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    w1700k::Controller controller(argv[1], true, std::chrono::milliseconds(300));
    std::string line;
    while (std::getline(std::cin, line)) {
        nlohmann::json reply;
        try {
            controller.run(nlohmann::json::parse(line).get<std::vector<std::string>>());
            reply["ok"] = true;
        } catch (const std::exception &error) {
            reply = {{"ok", false}, {"error", error.what()}};
        }
        const auto state = controller.state();
        reply["frequency"] = state.lo_hz;
        reply["band"] = state.band;
        reply["rate"] = state.sample_rate;
        reply["lna"] = state.lna;
        reply["agc"] = state.agc;
        std::cout << reply.dump() << std::endl;
    }
}
'''

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
state_path, log_path = os.environ['CONTROL_STATE'], os.environ['CONTROL_LOG']
assert sys.argv[1:] == ['serve']
with open(log_path, 'a') as log:
    log.write(json.dumps(dict(pid=os.getpid(), start=True)) + '\n')
for line in sys.stdin:
    request = json.loads(line)
    with open(log_path, 'a') as log:
        log.write(json.dumps(dict(pid=os.getpid(), request=request)) + '\n')
    state = json.load(open(state_path))
    hz = request.get('hz', 0)
    print(json.dumps(dict(event='state', state=state)), flush=True)
    if hz == 5300000001:
        print(json.dumps(dict(id=request['id'], ok=False, error='synthesizer unlocked')), flush=True)
        continue
    if request['op'] == 'tune': state['radio']['lo_hz'] = hz
    elif request['op'] == 'configure':
        state['radio']['band'] = request['band']
        state['radio']['sample_rate'] = request.get('sample_rate', state['radio']['sample_rate'])
    elif request['op'] == 'gain':
        state['rf']['agc'] = request.get('agc', False)
        state['rf']['lna'] = request.get('lna')
        state['rf']['lpf'] = request.get('lpf')
    json.dump(state, open(state_path, 'w'))
    if hz == 5300000002: sys.exit(3)
    if hz == 5300000003:
        print(json.dumps(dict(id=request['id'] + 1, ok=True, state=state)), flush=True)
        continue
    if hz == 5300000004:
        print('not json', flush=True)
        continue
    if hz == 5300000005: time.sleep(30)
    if hz == 5300000009:
        print(json.dumps(dict(id=request['id'], ok=True, state=dict(radio=dict(band='bad')))), flush=True)
        continue
    if hz == 5300000007:
        sys.stderr.write('trace\n' * 20000)
        sys.stderr.flush()
    reply = json.dumps(dict(id=request['id'], ok=True, state=state))
    sys.stdout.write(reply[:9])
    sys.stdout.flush()
    sys.stdout.write(reply[9:] + '\n')
    sys.stdout.flush()
    if hz == 5300000008: sys.exit(0)
'''


class CommandSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = os.environ.get('CXX', 'c++')
        if not shutil.which(compiler):
            raise unittest.SkipTest('no C++ compiler')
        probe = subprocess.run([compiler, '-std=c++17', '-x', 'c++', '-fsyntax-only', '-'],
                               input='#include <nlohmann/json.hpp>\n', text=True, capture_output=True)
        if probe.returncode:
            raise unittest.SkipTest('nlohmann-json headers missing')
        cls.build = tempfile.TemporaryDirectory(prefix='soapy-command-')
        source = Path(cls.build.name) / 'client.cpp'
        source.write_text(CLIENT)
        cls.client = str(source.with_suffix(''))
        result = subprocess.run([compiler, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-pthread',
                                 '-I' + str(ROOT / 'src'), '-I' + str(ROOT.parent / 'stream/include'),
                                 str(source), str(ROOT / 'src/Command.cpp'), str(ROOT / 'src/Session.cpp'),
                                 '-o', cls.client], capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='soapy-session-')
        self.addCleanup(self.tmp.cleanup)
        self.fake = Path(self.tmp.name) / 'w1700k'
        self.fake.write_text(FAKE)
        self.fake.chmod(0o755)
        self.state = self.fake.with_suffix('.json')
        self.state.write_text(json.dumps(dict(radio=dict(band=1, lo_hz=5250000000, sample_rate=40000000),
                                              rf=dict(agc=True, lna=None, lpf=None))))
        self.log = self.fake.with_suffix('.log')

    def requests(self, commands):
        env = dict(os.environ, CONTROL_STATE=str(self.state), CONTROL_LOG=str(self.log))
        result = subprocess.run([self.client, str(self.fake)], env=env, text=True, capture_output=True,
                                input=''.join(json.dumps(command) + '\n' for command in commands), timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = list(map(json.loads, result.stdout.splitlines()))
        self.assertEqual(len(rows), len(commands), result.stdout)
        entries = list(map(json.loads, self.log.read_text().splitlines()))
        return rows, entries

    def test_one_child_handles_events_fragmented_replies_and_settings(self):
        rows, entries = self.requests([['status', '--rf'], ['tune', '5400000000'], ['band', '2', '--rate', '160'],
                                      ['gain', '--lna', '2', '--lpf', '5'], ['gain', '--agc']])
        self.assertTrue(all(row['ok'] for row in rows), rows)
        self.assertEqual(rows[1]['frequency'], 5400000000)
        self.assertEqual((rows[2]['band'], rows[2]['rate']), (2, 160000000))
        self.assertEqual((rows[3]['lna'], rows[3]['agc'], rows[4]['agc']), (2, False, True))
        self.assertEqual(sum(entry.get('start', False) for entry in entries), 1)
        self.assertEqual([entry['request']['id'] for entry in entries if 'request' in entry], [1, 2, 3, 4, 5])

    def test_radio_error_keeps_session_and_last_confirmed_state(self):
        rows, entries = self.requests([['tune', '5300000001'], ['tune', '5400000000']])
        self.assertFalse(rows[0]['ok'])
        self.assertIn('synthesizer unlocked', rows[0]['error'])
        self.assertEqual(rows[0]['frequency'], 5250000000)
        self.assertTrue(rows[1]['ok'])
        self.assertEqual(sum(entry.get('start', False) for entry in entries), 1)

    def test_partial_operation_is_not_replayed_after_child_exit(self):
        rows, entries = self.requests([['tune', '5300000002'], ['status', '--rf'], ['tune', '5400000000']])
        self.assertFalse(rows[0]['ok'])
        self.assertEqual((rows[1]['ok'], rows[1]['frequency']), (True, 5300000002))
        self.assertTrue(rows[2]['ok'])
        self.assertEqual(sum(entry.get('start', False) for entry in entries), 2)
        self.assertEqual(sum(entry.get('request', {}).get('op') == 'tune' for entry in entries), 2)

    def test_wrong_id_and_malformed_json_close_session(self):
        for hz, message in [(5300000003, 'reply id'), (5300000004, 'invalid JSON')]:
            with self.subTest(hz=hz):
                rows, entries = self.requests([['tune', str(hz)], ['status', '--rf']])
                self.assertFalse(rows[0]['ok'])
                self.assertIn(message, rows[0]['error'])
                self.assertTrue(rows[1]['ok'])

    def test_timeout_ends_child_and_next_request_reconnects(self):
        rows, entries = self.requests([['tune', '5300000005'], ['status', '--rf']])
        self.assertIn('timed out', rows[0]['error'])
        self.assertTrue(rows[1]['ok'])
        for entry in entries:
            if entry.get('start'):
                with self.assertRaises(ProcessLookupError):
                    os.kill(entry['pid'], 0)

    def test_stderr_is_drained_while_waiting_for_reply(self):
        rows, _ = self.requests([['tune', '5300000007']])
        self.assertTrue(rows[0]['ok'], rows)

    def test_malformed_state_closes_session_without_publishing_it(self):
        rows, entries = self.requests([['tune', '5300000009'], ['status', '--rf']])
        self.assertFalse(rows[0]['ok'])
        self.assertEqual(rows[0]['frequency'], 5250000000)
        self.assertTrue(rows[1]['ok'])
        self.assertEqual(sum(entry.get('start', False) for entry in entries), 2)

    def test_closed_input_cannot_send_sigpipe_to_application(self):
        rows, _ = self.requests([['tune', '5300000008'], ['status', '--rf'], ['status', '--rf']])
        self.assertTrue(rows[0]['ok'])
        self.assertTrue(rows[-1]['ok'])


if __name__ == '__main__':
    unittest.main()
