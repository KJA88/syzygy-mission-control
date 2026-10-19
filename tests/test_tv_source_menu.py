import ast
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


class AnchorTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parent.parent / 'scripts/tv-mcp/server.py').read_text())
        selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ('_steps', '_goto_keys')], type_ignores=[])
        self.run = Mock(return_value=SimpleNamespace(stdout='Transmit Status: OK', stderr=''))
        self.save = Mock()
        self.key = Mock()
        self.state = Mock()
        self.remote = Mock()
        self.remote.__enter__ = Mock(return_value=self.remote)
        self.remote.__exit__ = Mock(return_value=False)
        cycle = ('livetv', 'soundbar', 'dashboard', 'cable')
        self.ns = dict(CYCLE=cycle, INDEX={v:i for i,v in enumerate(cycle)},
                       TV_HOST='test', subprocess=SimpleNamespace(run=self.run, SubprocessError=subprocess.SubprocessError),
                       time=SimpleNamespace(sleep=Mock()), _save_input=self.save,
                       _send_key=self.key, STATE_PATH=self.state, _tv=Mock(return_value=self.remote))
        exec(compile(selected, 'server.py', 'exec'), self.ns)

    def test_dashboard_only_uses_cec(self):
        out = self.ns['_goto_keys']('dashboard')
        self.assertTrue(out['ok'])
        self.assertEqual(self.run.call_count, 1)
        self.assertIn('phys-addr=2.0.0.0', self.run.call_args.args[0])
        self.remote.send_key.assert_not_called()

    def test_every_target_reanchors(self):
        for target, keys in [('cable', ['KEY_SOURCE','KEY_DOWN','KEY_ENTER']), ('dashboard', []), ('cable', ['KEY_SOURCE','KEY_DOWN','KEY_ENTER'])]:
            self.run.reset_mock()
            self.remote.send_key.reset_mock()
            out = self.ns['_goto_keys'](target)
            self.assertEqual(out['key_hdmi_presses'], 0)
            self.assertEqual(self.run.call_count, 1)
            self.assertEqual([c.args[0] for c in self.remote.send_key.call_args_list], keys)

    def test_uncalibrated_routes_do_nothing(self):
        for target in ('soundbar', 'livetv'):
            self.assertEqual(self.ns['_goto_keys'](target)['error'], 'INPUT_ROUTE_NOT_CALIBRATED')
        self.run.assert_not_called()
        self.remote.send_key.assert_not_called()
        self.save.assert_not_called()

    def test_cec_failure_sends_no_keys_or_state(self):
        for failure in (subprocess.CalledProcessError(1, 'cec-ctl'), subprocess.TimeoutExpired('cec-ctl',10)):
            self.run.side_effect = failure
            self.assertFalse(self.ns['_goto_keys']('cable')['ok'])
        self.remote.send_key.assert_not_called()
        self.save.assert_not_called()

    def test_cec_report_failure_is_not_success(self):
        self.run.return_value.stdout = 'Transmit Status: Not Acknowledged'
        self.assertFalse(self.ns['_goto_keys']('cable')['ok'])
        self.remote.send_key.assert_not_called()

    def test_key_failure_does_not_save_target(self):
        self.remote.send_key.side_effect = OSError('disconnected')
        out = self.ns['_goto_keys']('cable')
        self.assertFalse(out['ok'])
        self.save.assert_called_once_with('dashboard')
        self.state.unlink.assert_called_once_with(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
