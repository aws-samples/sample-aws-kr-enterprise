import json
import os
import tempfile
import unittest
from pathlib import Path

from kiro_agent_sdk import CLIConnectionError, ForkSessionResult, ProtocolError, fork_session, rename_session

FAKE = os.path.join(os.path.dirname(__file__), 'fake_kiro.py')


class SessionControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_rename_and_fork_do_not_open_control_sessions(self):
        with tempfile.TemporaryDirectory() as cwd:
            log = Path(cwd) / 'requests.jsonl'
            env = {'FAKE_LOG_REQUESTS': str(log)}
            await rename_session('source', '  New title  ', cwd=cwd, cli_path=FAKE, env=env)
            result = await fork_session('source', cwd=cwd, cli_path=FAKE, env=env,
                                        up_to_message_id='message-1', title='  branch  ')
            self.assertIsInstance(result, ForkSessionResult)
            self.assertEqual(result.session_id, 'forked')
            requests = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([request['method'] for request in requests],
                             ['initialize', '_kiro/session/rename', 'initialize', 'session/fork'])
            rename = requests[1]['params']
            self.assertEqual((rename['sessionId'], rename['title']), ('source', 'New title'))
            fork = requests[3]['params']
            self.assertEqual(fork['sessionId'], 'source')
            self.assertEqual(fork['_meta']['kiro'], {'messageId': 'message-1', 'title': 'branch'})

    async def test_control_failures_are_explicit(self):
        with self.assertRaises(ValueError):
            await rename_session('../outside', 'title', cli_path=FAKE)
        with self.assertRaises(ValueError):
            await rename_session('source', ' ', cli_path=FAKE)
        with self.assertRaises(ValueError):
            await fork_session('source', up_to_message_id='', cli_path=FAKE)
        with self.assertRaisesRegex(ProtocolError, 'did not confirm'):
            await rename_session('source', 'title', cli_path=FAKE, env={'FAKE_RENAME_FAIL': '1'})
        with self.assertRaisesRegex(ProtocolError, 'does not advertise'):
            await fork_session('source', cli_path=FAKE, env={'FAKE_NO_FORK': '1'})
        with self.assertRaisesRegex(ProtocolError, 'distinct'):
            await fork_session('test-session', cli_path=FAKE, env={'FAKE_FORK_SAME': '1'})
        with self.assertRaisesRegex(CLIConnectionError, 'Timed out'):
            await rename_session('source', 'title', cli_path=FAKE, timeout=0.1,
                                 env={'FAKE_CONTROL_HANG': '1'})
