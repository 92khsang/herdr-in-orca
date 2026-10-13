import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# Import the checkout's module whatever the working directory or -P/-I flags.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orca_link  # noqa: E402

M = vars(orca_link)

class WrapperTests(unittest.TestCase):
    def invoke(self, argv, env=None):
        with patch.object(sys, 'argv', ['orca-link', *argv]), patch.dict(os.environ, env or {}, clear=True), patch.dict(M, {'directory': lambda: Path('/state'), 'project_name': lambda: 'project', 'project_link': lambda root: 'matched'}), patch.dict(M, {'run': lambda root, name, cmd: (name, cmd), 'bind': lambda root, name, open_herdr: (name, open_herdr)}):
            return M['main']()

    def test_auto_command(self):
        self.assertEqual(self.invoke(['claude', '--continue']), ('matched', ['claude', '--continue']))

    def test_arbitrary_launcher(self):
        self.assertEqual(self.invoke(['custom-launcher']), ('matched', ['custom-launcher']))

    def test_command_options_not_parsed_by_wrapper(self):
        args = ['tool', '--help', '--version', '--link', 'child-value', 'a b', '$(not-shell)', ';']
        self.assertEqual(self.invoke(args), ('matched', args))

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            self.invoke(['--version'])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), 'orca-link ' + orca_link.__version__)

    def test_errors_become_exit_status(self):
        def fail(root, name, cmd):
            raise RuntimeError('boom')
        err = io.StringIO()
        with patch.dict(M, {'directory': lambda: Path('/state'), 'project_link': lambda root: 'matched', 'run': fail}), \
             patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(err):
            self.assertEqual(M['main'](['tool']), 1)
        self.assertEqual(err.getvalue(), 'orca-link: boom\n')

    def test_interrupt_becomes_exit_status_130(self):
        def interrupt(root, name, cmd):
            raise KeyboardInterrupt
        with patch.dict(M, {'directory': lambda: Path('/state'), 'project_link': lambda root: 'matched', 'run': interrupt}), \
             patch.dict(os.environ, {}, clear=True):
            self.assertEqual(M['main'](['tool']), 130)

    def test_named_binding(self):
        self.assertEqual(self.invoke(['--link', 'vim', 'codex', 'resume', '--last']), ('vim', ['codex', 'resume', '--last']))

    def test_run_subcommand(self):
        self.assertEqual(self.invoke(['run', 'vim', 'claude', '--continue']), ('vim', ['claude', '--continue']))

    def test_builtin_name_escape(self):
        self.assertEqual(self.invoke(['--', 'open', 'file']), ('matched', ['open', 'file']))

    def test_empty_run_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.invoke(['run', 'vim'])
        self.assertEqual(caught.exception.code, 2)

    def test_inside_no_default_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.invoke([], {'HERDR_ENV':'1'}), 0)
        self.assertIn('COMMAND', out.getvalue())

    def test_outside_opens_herdr(self):
        with patch.object(sys, 'argv', ['orca-link']), patch.dict(os.environ, {}, clear=True), patch.dict(M, {'directory':lambda:Path('/state'), 'project_name':lambda:'project'}), patch.dict(M, {'bind':lambda root,name,open_herdr:self.assertEqual((name,open_herdr),('project',True))}):
            M['main']()

    def test_invalid_link(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke(['--link', '../bad', 'tool'])

    def test_real_child_exit_args_and_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);out=root/'child.json'
            script='import json,os,sys; open(sys.argv[1],"w").write(json.dumps([sys.argv[2:],os.environ["PASSTHROUGH_TEST"]]));sys.exit(7)'
            args=[sys.executable,'-c',script,str(out),'a b','$(literal)','--help']
            with patch.dict(M, {'context':lambda r,n:{**os.environ,'PASSTHROUGH_TEST':'preserved'}, 'is_bound':lambda r,n:True}):
                self.assertEqual(M['run'](root,'test',args),7)
            self.assertEqual(json.loads(out.read_text()),[['a b','$(literal)','--help'],'preserved'])

class BindingExitTests(unittest.TestCase):
    def test_reopen_existing_binding_or_orphan_consumer(self):
        for suffix in ('bind', 'run'):
            with self.subTest(lock=suffix), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                # Also covers a consumer that outlived its binding JSON.
                with M['locked'](root / ('test.' + suffix + '.lock')), \
                     patch.dict(os.environ, {'ORCA_PANE_KEY': 'pane'}, clear=True), \
                     patch.object(M['subprocess'], 'run') as attach, \
                     patch.dict(M, {'bind_new': lambda *a: self.fail('Must not replace context')}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    M['bind'](root, 'test', open_herdr=True)
                    attach.assert_called_once_with(['herdr'], check=False)
                    self.assertFalse((root / 'test.json').exists())
                    with self.assertRaises(RuntimeError):
                        M['locked'](root / ('test.' + suffix + '.lock'))
                    with self.assertRaises(RuntimeError):
                        M['bind'](root, 'test', open_herdr=False)

    def test_reopen_preserves_live_binding_metadata_on_interrupt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 'test.json'
            state.write_text('{"original":true}')
            with M['locked'](root / 'test.bind.lock'), \
                 patch.dict(os.environ, {'ORCA_PANE_KEY': 'pane'}, clear=True), \
                 patch.object(M['subprocess'], 'run', side_effect=KeyboardInterrupt), \
                 contextlib.redirect_stdout(io.StringIO()):
                M['bind'](root, 'test', open_herdr=True)
                self.assertTrue(M['is_bound'](root, 'test'))
                self.assertEqual(state.read_text(), '{"original":true}')

    def exercise_exit(self, *, open_herdr=True, active=False, interrupt_herdr=False):
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            root = Path(temp)
            endpoint = root / 'endpoint'
            endpoint.touch()
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.dict(os.environ, {
                'ORCA_PANE_KEY': 'test-pane', 'ORCA_TERMINAL_HANDLE': 'test-terminal',
                'ORCA_AGENT_HOOK_ENDPOINT': str(endpoint),
            }, clear=True))
            stack.enter_context(patch.dict(M, {
                'rpc': lambda *args: {'_meta': {'runtimeId': 'test'}},
                'project_root': lambda: root,
            }))
            held = []

            def start_command():
                if active and not held:
                    held.append(stack.enter_context(M['locked'](root / 'test.run.lock')))

            def herdr(*args, **kwargs):
                start_command()
                if interrupt_herdr:
                    raise KeyboardInterrupt

            def interrupt_wait(*args):
                start_command()
                raise KeyboardInterrupt

            run_mock = stack.enter_context(patch.object(M['subprocess'], 'run', side_effect=herdr))
            sleep_mock = stack.enter_context(patch.object(M['time'], 'sleep', side_effect=interrupt_wait))
            M['bind'](root, 'test', open_herdr=open_herdr)
            self.assertFalse((root / 'test.json').exists())
            self.assertFalse(M['is_bound'](root, 'test'))
            for lock in root.glob('*.target.lock'):
                with M['locked'](lock):
                    pass
            if active:
                # Closing the binding must not release or terminate the consumer.
                with self.assertRaises(RuntimeError):
                    M['locked'](root / 'test.run.lock')
                self.assertIn('Linked commands are retained', output.getvalue())
            if open_herdr:
                run_mock.assert_called_once_with(['herdr'], check=False)
            else:
                run_mock.assert_not_called()
            if open_herdr and (not active or interrupt_herdr):
                sleep_mock.assert_not_called()
            else:
                sleep_mock.assert_called_once()

    def test_ctrl_c_exits_after_herdr_detach_with_live_command(self):
        self.exercise_exit(active=True)

    def test_herdr_detach_without_command_exits_immediately(self):
        self.exercise_exit()

    def test_bind_ctrl_c_retains_live_command(self):
        self.exercise_exit(open_herdr=False, active=True)

    def test_ctrl_c_during_herdr_cleans_binding(self):
        self.exercise_exit(active=True, interrupt_herdr=True)

if __name__ == '__main__':
    unittest.main()
