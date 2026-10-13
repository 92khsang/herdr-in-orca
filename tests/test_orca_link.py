import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch

# Import the checkout's module whatever the working directory or -P/-I flags.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orca_link  # noqa: E402

M = vars(orca_link)

def assert_herdr_started(test, mock, env):
    """Herdr must start once, with exactly ``env``."""
    mock.assert_called_once()
    args, kwargs = mock.call_args
    test.assertEqual(args, (['herdr'],))
    test.assertIs(kwargs.get('check'), False)
    test.assertEqual(kwargs['env'], env)

class WrapperTests(unittest.TestCase):
    def invoke(self, argv, env=None):
        with patch.object(sys, 'argv', ['orca-link', *argv]), patch.dict(os.environ, env or {}, clear=True), patch.dict(M, {'directory': lambda: Path('/state'), 'project_name': lambda: 'project', 'project_link': lambda root, usage: 'matched'}), patch.dict(M, {'run': lambda root, name, cmd: (name, cmd), 'bind': lambda root, name, open_herdr: (name, open_herdr)}):
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
        with patch.dict(M, {'directory': lambda: Path('/state'), 'project_link': lambda root, usage: 'matched', 'run': fail}), \
             patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(err):
            self.assertEqual(M['main'](['tool']), 1)
        self.assertEqual(err.getvalue(), 'orca-link: boom\n')

    def test_interrupt_becomes_exit_status_130(self):
        def interrupt(root, name, cmd):
            raise KeyboardInterrupt
        with patch.dict(M, {'directory': lambda: Path('/state'), 'project_link': lambda root, usage: 'matched', 'run': interrupt}), \
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

    def open_without_name(self, links, argv=()):
        calls = []
        with patch.dict(os.environ, {}, clear=True), \
             patch.dict(M, {'directory': lambda: Path('/state'), 'project_name': lambda: 'project',
                            'project_links': lambda root: links,
                            'bind': lambda root, name, open_herdr: calls.append((name, open_herdr))}):
            self.assertEqual(M['main'](list(argv)), 0)
        return calls

    def test_plain_orca_link_binds_the_default_name(self):
        self.assertEqual(self.open_without_name([]), [('project', True)])
        self.assertEqual(self.open_without_name([], ['open']), [('project', True)])

    def test_open_reopens_a_live_project_link(self):
        self.assertEqual(self.open_without_name(['live']), [('live', True)])
        self.assertEqual(self.open_without_name(['first', 'second']), [('first', True)])
        self.assertEqual(self.open_without_name(['other', 'project']), [('project', True)])

    def test_check_without_name_uses_the_project_link(self):
        calls = []

        def project_link(root, usage):
            calls.append(usage)
            return 'live'
        with patch.dict(os.environ, {}, clear=True), \
             patch.dict(M, {'directory': lambda: Path('/state'), 'project_link': project_link,
                            'context': lambda root, name: calls.append(name)}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M['main'](['check']), 0)
        self.assertEqual(calls, ['orca-link check NAME', 'live'])

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

class IdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def bind_link(self, name, project, env=None):
        """Create a live link: its JSON file plus a held bind lock."""
        lock = M['locked'](self.root / (name + '.bind.lock'))
        self.addCleanup(lock.close)
        path = self.root / (name + '.json')
        path.write_text(json.dumps({'env': env or {'ORCA_TERMINAL_HANDLE': 'term'}, 'runtime': 'rt', 'project': str(project)}))
        path.chmod(0o600)

    def test_clean_environ_keeps_only_installation_keys(self):
        env = {'ORCA_PANE_KEY': 'stale', 'ORCA_AGENT_HOOK_TOKEN': 'secret', 'ORCA_USER_DATA_PATH': 'data',
               'ORCA_CLI_COMMAND': 'orca-test', 'OTHER': 'kept'}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(M['clean_environ'](), {'ORCA_USER_DATA_PATH': 'data', 'ORCA_CLI_COMMAND': 'orca-test', 'OTHER': 'kept'})

    def test_project_link_matches_the_exact_project_only(self):
        self.bind_link('app-a', Path('/a/app'))
        self.bind_link('app-b', Path('/b/app'))
        with patch.dict(M, {'project_root': lambda: Path('/b/app')}):
            self.assertEqual(M['project_link'](self.root, 'USAGE'), 'app-b')
        with patch.dict(M, {'project_root': lambda: Path('/c/app')}):
            self.assertEqual(M['project_links'](self.root), [])
            with self.assertRaisesRegex(RuntimeError, 'No live link.*USAGE'):
                M['project_link'](self.root, 'USAGE')

    def test_project_links_skip_links_that_are_not_live_or_not_objects(self):
        (self.root / 'gone.json').write_text(json.dumps({'env': {}, 'runtime': 'rt', 'project': str(Path('/a/app'))}))
        lock = M['locked'](self.root / 'odd.bind.lock')
        self.addCleanup(lock.close)
        (self.root / 'odd.json').write_text('[]')
        with patch.dict(M, {'project_root': lambda: Path('/a/app')}):
            self.assertEqual(M['project_links'](self.root), [])

    def test_several_links_for_one_project_are_an_error(self):
        self.bind_link('one', Path('/a/app'))
        self.bind_link('two', Path('/a/app'))
        with patch.dict(M, {'project_root': lambda: Path('/a/app')}):
            self.assertEqual(M['project_links'](self.root), ['one', 'two'])
            with self.assertRaisesRegex(RuntimeError, r'one, two.*USAGE'):
                M['project_link'](self.root, 'USAGE')

    def test_commands_without_a_name_need_a_live_link_in_this_project(self):
        # A live link named after the directory but bound in another project must not match.
        self.bind_link('app', Path('/other/app'))
        binds = []
        err = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stderr(err), \
             patch.dict(M, {'directory': lambda: self.root, 'project_root': lambda: Path('/mine/app'),
                            'bind': lambda root, name, open_herdr: binds.append(name)}):
            expected = M['project_name']()
            self.assertEqual(M['main'](['tool']), 1)
            self.assertEqual(M['main'](['check']), 1)
            self.assertEqual(M['main'](['open']), 0)
        self.assertEqual(err.getvalue().count('No live link'), 2)
        self.assertIn('orca-link check NAME', err.getvalue())
        self.assertEqual(binds, [expected])

    def test_default_names_differ_per_path(self):
        names = []
        for path in ('/a/app', '/b/app', '/c/' + 'x' * 60, '/d/...'):
            with patch.dict(M, {'project_root': lambda path=path: Path(path)}):
                names.append(M['project_name']())
        self.assertTrue(names[0].startswith('app-'))
        self.assertNotEqual(names[0], names[1])
        self.assertTrue(names[3].startswith('project-'))
        for name in names:
            self.assertRegex(name, r'^[A-Za-z0-9_-]{1,40}$')

    def linked_context(self, caller, bound):
        """Run context() for a live link with ``bound`` variables; return (env, rpc env, stderr)."""
        self.bind_link('it', Path('/a/app'), bound)
        rpc_envs = []

        def rpc(*args, env=None):
            rpc_envs.append(dict(env))
            return {'_meta': {'runtimeId': 'rt'}}
        err = io.StringIO()
        with patch.dict(os.environ, caller, clear=True), patch.dict(M, {'rpc': rpc}), contextlib.redirect_stderr(err):
            env = M['context'](self.root, 'it')
        return env, rpc_envs[0], err.getvalue()

    def test_context_builds_the_linked_environment(self):
        home = self.root / 'home'
        (home / '.orca' / 'agent-hooks').mkdir(parents=True)
        (home / '.orca' / 'agent-hooks' / 'codex-hook.sh').touch()
        bound = {'ORCA_PANE_KEY': 'bound', 'ORCA_TERMINAL_HANDLE': 'term', 'ORCA_USER_DATA_PATH': 'bound-data'}
        caller = {'ORCA_PANE_KEY': 'stale', 'ORCA_AGENT_HOOK_TOKEN': 'secret', 'ORCA_USER_DATA_PATH': 'caller-data',
                  'ORCA_CLI_COMMAND': 'orca-test', 'HOME': str(home), 'USERPROFILE': str(home), 'OTHER': 'kept'}
        env, rpc_env, _ = self.linked_context(caller, bound)
        expected = {'ORCA_CLI_COMMAND': 'orca-test', 'HOME': str(home), 'USERPROFILE': str(home), 'OTHER': 'kept', **bound}
        self.assertEqual(rpc_env, expected)
        self.assertEqual(env, {**expected, 'ORCA_AGENT_HOOK_ROOT': str(home / '.orca')})

    def test_context_notes_an_identity_inherited_from_herdr(self):
        bound = {'ORCA_PANE_KEY': 'bound', 'ORCA_TERMINAL_HANDLE': 'term'}
        _, _, note = self.linked_context({'HERDR_ENV': '1', 'ORCA_PANE_KEY': 'stale'}, bound)
        self.assertIn('Stop the Herdr server', note)

    def test_context_is_quiet_for_its_own_or_no_identity(self):
        bound = {'ORCA_PANE_KEY': 'bound', 'ORCA_TERMINAL_HANDLE': 'term'}
        for caller in ({'HERDR_ENV': '1', 'ORCA_PANE_KEY': 'bound'}, {'HERDR_ENV': '1'}, {'ORCA_PANE_KEY': 'stale'}):
            with self.subTest(caller=caller):
                lock_dir = tempfile.TemporaryDirectory()
                self.addCleanup(lock_dir.cleanup)
                self.root = Path(lock_dir.name)
                _, _, note = self.linked_context(caller, bound)
                self.assertEqual(note, '')

    def test_context_names_the_link_when_its_terminal_is_gone(self):
        self.bind_link('it', Path('/a/app'), {'ORCA_PANE_KEY': 'bound', 'ORCA_TERMINAL_HANDLE': 'term'})

        def rpc(*args, env=None):
            raise M['TerminalGone']('gone')
        with patch.dict(os.environ, {}, clear=True), patch.dict(M, {'rpc': rpc}):
            with self.assertRaisesRegex(RuntimeError, 'terminal of link it is gone; run orca-link open it '):
                M['context'](self.root, 'it')

    def test_rpc_runs_the_cli_with_the_given_environment(self):
        env = {'ORCA_CLI_COMMAND': 'orca-test', 'PATH': 'test-path'}
        seen = {}

        def which(name, path=None):
            seen['which'] = (name, path)
            return 'resolved-orca'

        def run(argv, **kwargs):
            seen['run'] = (argv, kwargs.get('env'))
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true, "_meta": {"runtimeId": "rt"}}', stderr='')
        with patch.object(M['shutil'], 'which', which), patch.object(M['subprocess'], 'run', run):
            M['rpc']('terminal', 'show', '--terminal', 'h', env=env)
        self.assertEqual(seen['which'], ('orca-test', 'test-path'))
        self.assertEqual(seen['run'], (['resolved-orca', 'terminal', 'show', '--terminal', 'h', '--json'], env))

class HardeningTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_lock_without_retries_never_sleeps(self):
        held = M['locked'](self.root / 'x.lock')
        self.addCleanup(held.close)
        with patch.object(M['time'], 'sleep', side_effect=AssertionError('slept')):
            with self.assertRaises(M['LinkBusy']):
                M['locked'](self.root / 'x.lock')

    def test_owners_retry_and_probes_do_not(self):
        held = M['locked'](self.root / 'it.bind.lock')
        self.addCleanup(held.close)
        with patch.object(M['time'], 'sleep', side_effect=AssertionError('probe slept')):
            self.assertTrue(M['is_bound'](self.root, 'it'))
        with patch.object(M['time'], 'sleep') as sleep, self.assertRaises(M['LinkBusy']):
            M['locked'](self.root / 'it.bind.lock', retries=M['LOCK_RETRIES'])
        self.assertEqual(sleep.call_args_list, [call(0.05)] * 3)

    def test_lock_retries_until_the_holder_releases(self):
        held = M['locked'](self.root / 'x.lock')
        with patch.object(M['time'], 'sleep', side_effect=lambda delay: held.close()) as sleep:
            with M['locked'](self.root / 'x.lock', retries=2):
                pass
        sleep.assert_called_once()

    def test_lock_reports_file_errors_as_they_are(self):
        with self.assertRaises(FileNotFoundError):
            M['locked'](self.root / 'missing' / 'x.lock')

    @unittest.skipIf(os.name == 'nt', 'flock is POSIX-only')
    def test_lock_failures_other_than_contention_are_errors(self):
        with patch.object(M['fcntl'], 'flock', side_effect=OSError(errno.ENOLCK, 'No locks available')):
            with self.assertRaises(OSError):
                M['locked'](self.root / 'x.lock')

    def test_binder_retries_its_bind_lock(self):
        held = M['locked'](self.root / 'it.bind.lock')
        calls = []
        with patch.dict(os.environ, {'ORCA_PANE_KEY': 'pane'}, clear=True), \
             patch.object(M['time'], 'sleep', side_effect=lambda delay: held.close()), \
             patch.dict(M, {'bind_new': lambda root, name, open_herdr: calls.append(name),
                            'attach_existing': lambda name: self.fail('reattached')}):
            M['bind'](self.root, 'it', open_herdr=True)
        self.assertEqual(calls, ['it'])

    def test_exit_status_on_each_platform(self):
        with patch.object(M['os'], 'name', 'nt'):
            nt = [M['exit_status'](rc) for rc in (7, 2**31 - 1, 2**31, 0xC000013A)]
        with patch.object(M['os'], 'name', 'posix'):
            posix = [M['exit_status'](rc) for rc in (0, 7, -15, -2)]
        self.assertEqual(nt, [7, 2**31 - 1, -2**31, 0xC000013A - 2**32])
        self.assertEqual(posix, [0, 7, 143, 130])

    def fake_child(self, returncode, interrupts=0, timeouts=0, check=lambda: None):
        """Return a stand-in for Popen that sends SIGINT during its first waits."""
        class Child:
            waits = 0

            def poll(self):
                return getattr(self, 'returncode', None)

            def wait(self, timeout=None):
                self.waits += 1
                check()
                if self.waits <= interrupts:
                    signal.raise_signal(signal.SIGINT)
                if self.waits <= interrupts + timeouts:
                    raise subprocess.TimeoutExpired('tool', timeout)
                self.returncode = returncode
                return returncode
        return Child()

    def run_with(self, child, is_bound=lambda root, name: True, popen=None):
        with patch.dict(M, {'context': lambda root, name: {}, 'is_bound': is_bound}), \
             patch.object(M['subprocess'], 'Popen', popen or (lambda *args, **kwargs: child)):
            return M['run'](self.root, 'test', ['tool'])

    def test_run_ignores_ctrl_c_until_the_command_exits(self):
        previous = signal.getsignal(signal.SIGINT)

        def run_lock_is_held():
            with self.assertRaises(M['LinkBusy']):
                M['locked'](self.root / 'test.run.lock')
        child = self.fake_child(5, interrupts=2, check=run_lock_is_held)
        try:
            self.assertEqual(self.run_with(child), 5)
        except KeyboardInterrupt:
            self.fail('Ctrl-C ended run() before the command exited')
        self.assertEqual(child.waits, 3)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_consumer_retries_its_run_lock(self):
        held = M['locked'](self.root / 'test.run.lock')
        with patch.object(M['time'], 'sleep', side_effect=lambda delay: held.close()):
            self.assertEqual(self.run_with(self.fake_child(0)), 0)

    def test_run_maps_the_command_status(self):
        returncode = 0xC000013A if os.name == 'nt' else -2
        self.assertEqual(self.run_with(self.fake_child(returncode)), M['exit_status'](returncode))
        self.assertNotEqual(M['exit_status'](returncode), returncode)

    def test_run_reports_commands_that_cannot_start(self):
        cases = ((FileNotFoundError(errno.ENOENT, 'missing'), 127, 'command not found: tool'),
                 (PermissionError(errno.EACCES, 'denied'), 126, 'cannot run tool: denied'),
                 (OSError(errno.ENOEXEC, 'Exec format error'), 126, 'cannot run tool: Exec format error'))
        for error, status, message in cases:
            with self.subTest(error=error):
                def popen(*args, **kwargs):
                    raise error
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(self.run_with(None, popen=popen), status)
                self.assertIn(message, err.getvalue())

    def test_run_reports_a_closed_binding_once(self):
        def gone(root, name):
            raise OSError(errno.ENOENT, 'state directory removed')
        for is_bound in (lambda root, name: False, gone):
            with self.subTest(is_bound=is_bound):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(self.run_with(self.fake_child(0, timeouts=2), is_bound=is_bound), 0)
                self.assertEqual(err.getvalue().count('binding closed'), 1)

    def test_missing_command_exits_127(self):
        err = io.StringIO()
        with patch.dict(M, {'context': lambda root, name: dict(os.environ), 'is_bound': lambda root, name: True}), \
             contextlib.redirect_stderr(err):
            self.assertEqual(M['run'](self.root, 'test', ['orca-link-test-no-such-command']), 127)
        self.assertIn('command not found', err.getvalue())

    def rpc_with(self, returncode, stdout):
        reply = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=b'')

        def run(*args, **kwargs):
            # The reply must reach json.loads as bytes, not decoded with the locale.
            self.assertFalse({'text', 'encoding', 'errors', 'universal_newlines'} & set(kwargs))
            return reply
        with patch.dict(M, {'cli': lambda env=None: 'orca-test'}), patch.object(M['subprocess'], 'run', run):
            return M['rpc']('terminal', 'show')

    def test_rpc_reports_orca_errors_by_code(self):
        cases = [
            (1, b'{"ok": false, "error": {"code": "terminal_handle_stale"}}', 'terminal is gone'),
            (1, b'{"ok": false, "error": {"code": "terminal_gone"}}', 'terminal is gone'),
            (1, b'{"ok": false, "error": {"code": "runtime_unavailable"}}', 'not running'),
            (1, b'{"ok": false, "error": {"code": "other_error"}}', r'failed \(other_error\)'),
            (0, b'not json', 'did not return JSON'),
            (0, b'[]', 'did not return JSON'),
        ]
        for returncode, stdout, message in cases:
            with self.subTest(stdout=stdout), self.assertRaisesRegex(RuntimeError, message):
                self.rpc_with(returncode, stdout)

    def test_rpc_accepts_a_reply_without_metadata(self):
        reply = self.rpc_with(0, b'\xef\xbb\xbf{"ok": true, "_meta": null}')
        self.assertIsNone(M['runtime_id'](reply))

    def write_binding(self, content, mode=0o600):
        path = self.root / 'it.json'
        path.write_text(content)
        path.chmod(mode)

    def test_load_binding_rejects_invalid_files(self):
        valid = {'env': {'ORCA_TERMINAL_HANDLE': 'term'}, 'runtime': 'rt', 'project': 'p'}
        self.write_binding(json.dumps(valid))
        self.assertEqual(M['load_binding'](self.root, 'it'), valid)
        for content in ('not json', '[]', '{"env": []}', '{"env": {}}', '{"env": {"ORCA_TERMINAL_HANDLE": 1}}',
                        '{"env": {"ORCA_TERMINAL_HANDLE": "term"}, "project": 1}'):
            with self.subTest(content=content):
                self.write_binding(content)
                with self.assertRaisesRegex(RuntimeError, 'invalid'):
                    M['load_binding'](self.root, 'it')

    def binder_env(self):
        endpoint = self.root / 'endpoint'
        endpoint.touch()
        return {'ORCA_PANE_KEY': 'pane', 'ORCA_TERMINAL_HANDLE': 'term', 'ORCA_AGENT_HOOK_ENDPOINT': str(endpoint)}

    def test_bind_removes_a_stale_binding_file_before_checking_orca(self):
        (self.root / 'it.json').write_text('{"stale": true}')
        seen = []

        def rpc(*args, env=None):
            seen.append((self.root / 'it.json').exists())
            raise RuntimeError('stop')
        with patch.dict(os.environ, self.binder_env(), clear=True), patch.dict(M, {'rpc': rpc}):
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                M['bind'](self.root, 'it')
        self.assertEqual(seen, [False])

    def test_bind_refuses_a_terminal_that_backs_another_link(self):
        held = M['locked'](self.root / (hashlib.sha256(b'pane').hexdigest() + '.target.lock'))
        self.addCleanup(held.close)
        with patch.dict(os.environ, self.binder_env(), clear=True), \
             patch.dict(M, {'rpc': lambda *args, env=None: {'_meta': {'runtimeId': 'rt'}}, 'project_root': lambda: self.root}):
            with self.assertRaisesRegex(RuntimeError, 'already backs another link'):
                M['bind'](self.root, 'it')
        self.assertFalse((self.root / 'it.json').exists())

    def test_load_binding_reports_a_missing_or_shared_file(self):
        with self.assertRaisesRegex(RuntimeError, 'starting or closing'):
            M['load_binding'](self.root, 'it')
        if os.name != 'nt':
            self.write_binding('{}', mode=0o644)
            with self.assertRaisesRegex(RuntimeError, 'not private'):
                M['load_binding'](self.root, 'it')

class BindingExitTests(unittest.TestCase):
    def test_reopen_existing_binding_or_orphan_consumer(self):
        for suffix in ('bind', 'run'):
            with self.subTest(lock=suffix), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                # Also covers a consumer that outlived its binding JSON.
                with M['locked'](root / ('test.' + suffix + '.lock')), \
                     patch.dict(os.environ, {'ORCA_PANE_KEY': 'pane', 'ORCA_AGENT_PANE': 'pane',
                                             'ORCA_CLI_COMMAND': 'orca-test', 'OTHER': 'kept'}, clear=True), \
                     patch.object(M['subprocess'], 'run') as attach, \
                     patch.dict(M, {'bind_new': lambda *a: self.fail('Must not replace context')}), \
                     contextlib.redirect_stdout(io.StringIO()):
                    M['bind'](root, 'test', open_herdr=True)
                    assert_herdr_started(self, attach, {'ORCA_CLI_COMMAND': 'orca-test', 'OTHER': 'kept'})
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
                'ORCA_PANE_KEY': 'test-pane', 'ORCA_AGENT_PANE': 'test-pane', 'ORCA_TERMINAL_HANDLE': 'test-terminal',
                'ORCA_AGENT_HOOK_ENDPOINT': str(endpoint), 'ORCA_AGENT_HOOK_TOKEN': 'secret',
                'ORCA_USER_DATA_PATH': 'data', 'ORCA_CLI_COMMAND': 'orca-test', 'OTHER': 'kept',
            }, clear=True))
            rpc_envs = []

            def rpc(*args, env=None):
                rpc_envs.append(dict(env))
                return {'_meta': {'runtimeId': 'test'}}
            stack.enter_context(patch.dict(M, {'rpc': rpc, 'project_root': lambda: root}))
            # What a binder captures: the KEYS it has, never the hook token.
            captured = {'ORCA_PANE_KEY': 'test-pane', 'ORCA_AGENT_PANE': 'test-pane', 'ORCA_TERMINAL_HANDLE': 'test-terminal',
                        'ORCA_AGENT_HOOK_ENDPOINT': str(endpoint), 'ORCA_USER_DATA_PATH': 'data'}
            clean = {'ORCA_USER_DATA_PATH': 'data', 'ORCA_CLI_COMMAND': 'orca-test', 'OTHER': 'kept'}
            held = []
            bindings = []

            def start_command():
                if active and not held:
                    held.append(stack.enter_context(M['locked'](root / 'test.run.lock')))

            def herdr(*args, **kwargs):
                bindings.append(json.loads((root / 'test.json').read_text()))
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
            self.assertEqual(rpc_envs, [{**clean, **captured}])
            if open_herdr:
                assert_herdr_started(self, run_mock, clean)
                self.assertEqual(bindings, [{'env': captured, 'runtime': 'test', 'project': str(root)}])
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
