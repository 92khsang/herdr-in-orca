"""End-to-end tests: orca-link in real processes against stub Orca and Herdr programs."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ORCA_LINK = Path(__file__).resolve().parents[1] / 'orca_link.py'

# Answers `terminal show --terminal term-1 --json` like the Orca CLI; any other
# handle is unknown.
FAKE_ORCA = '''
import json, sys
args = sys.argv[1:]
handle = args[args.index('--terminal') + 1] if '--terminal' in args else None
if handle == 'term-1':
    print(json.dumps({'ok': True, '_meta': {'runtimeId': 'rt-1'}}))
else:
    print(json.dumps({'ok': False, 'error': {'code': 'terminal_handle_stale'}, '_meta': {'runtimeId': 'rt-1'}}))
    sys.exit(1)
'''

# Records the ORCA_* names it was started with, then waits until the test
# releases it, like a Herdr client the user closes.
FAKE_HERDR = '''
import json, os, time
from pathlib import Path
base = Path(os.environ['LINK_TEST_DIR'])
(base / 'herdr-env.tmp').write_text(json.dumps(sorted(k for k in os.environ if k.startswith('ORCA_'))))
os.replace(base / 'herdr-env.tmp', base / 'herdr-env.json')
deadline = time.monotonic() + 60
while not (base / 'release-herdr').exists() and time.monotonic() < deadline:
    time.sleep(0.05)
'''

# A linked command, started by name: writes its ORCA_* variables to argv[1] and
# exits with argv[2].
DUMP_ENV = '''
import json, os, sys
with open(sys.argv[1], 'w') as out:
    json.dump({k: v for k, v in os.environ.items() if k.startswith('ORCA_')}, out)
sys.exit(int(sys.argv[2]))
'''

# Stands in for the Orca CLI that a Herdr pane might find on PATH; it must never be used.
DECOY = 'import sys; sys.exit(3)'

# A linked command: creates argv[1] when it starts, then waits up to 60 s for argv[2].
WAIT = ('import os, sys, time; open(sys.argv[1], "w").close(); deadline = time.monotonic() + 60\n'
        'while not os.path.exists(sys.argv[2]) and time.monotonic() < deadline: time.sleep(0.05)')

def write_program(directory, name, source):
    """Create program ``name`` in ``directory`` that runs ``source`` with this Python."""
    script = directory / (name + '.py')
    script.write_text(source)
    if os.name == 'nt':
        program = directory / (name + '.cmd')
        program.write_text('@"{}" "{}" %*\n'.format(sys.executable, script))
    else:
        program = directory / name
        program.write_text('#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, script))
        program.chmod(0o755)
    return program

class EndToEndTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name).resolve()
        bin_dir = self.dir / 'bin'
        bin_dir.mkdir()
        home = self.dir / 'home'
        home.mkdir()
        self.state = self.dir / 'state'
        self.state.mkdir(mode=0o700)
        self.project = self.dir / 'work' / 'app'
        self.project.mkdir(parents=True)
        self.other = self.dir / 'other' / 'app'
        self.other.mkdir(parents=True)
        self.endpoint = self.dir / 'endpoint.env'
        self.endpoint.touch()
        cli = write_program(bin_dir, 'orca-test', FAKE_ORCA)
        write_program(bin_dir, 'herdr', FAKE_HERDR)
        write_program(bin_dir, 'dump-env', DUMP_ENV)
        # Herdr panes find only these decoys on PATH, so consumers must use the CLI the
        # binder recorded.
        self.decoys = self.dir / 'decoys'
        self.decoys.mkdir()
        for name in ('orca', 'orca-ide'):
            write_program(self.decoys, name, DECOY)
        # Nothing from the developer's own Orca, Herdr or home directory leaks in.
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ORCA_', 'HERDR_'))}
        env.update({
            'HOME': str(home), 'USERPROFILE': str(home),
            'XDG_RUNTIME_DIR': str(self.state), 'LOCALAPPDATA': str(self.state),
            'PATH': str(bin_dir) + os.pathsep + env.get('PATH', ''),
            'PYTHONDONTWRITEBYTECODE': '1', 'LINK_TEST_DIR': str(self.dir),
        })
        self.env = env
        self.cli = str(cli)
        self.logs = []

    def binder_env(self):
        return {**self.env, 'ORCA_PANE_KEY': 'pane-1', 'ORCA_TAB_ID': 'tab-1', 'ORCA_TERMINAL_HANDLE': 'term-1',
                'ORCA_AGENT_HOOK_ENDPOINT': str(self.endpoint), 'ORCA_AGENT_HOOK_TOKEN': 'secret',
                'ORCA_CLI_COMMAND': self.cli}

    def pane_env(self):
        """A Herdr pane whose server was started with another terminal's environment."""
        return {**self.env, 'HERDR_ENV': '1', 'ORCA_PANE_KEY': 'stale', 'ORCA_AGENT_HOOK_TOKEN': 'leaked',
                'PATH': str(self.decoys) + os.pathsep + self.env['PATH']}

    def start(self, args, env):
        log = self.dir / 'log-{}.txt'.format(len(self.logs))
        self.logs.append(log)
        with open(log, 'w') as out:
            process = subprocess.Popen([sys.executable, str(ORCA_LINK), *args], cwd=self.project, env=env,
                                       stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)

    def orca_link(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(ORCA_LINK), *args], cwd=cwd or self.project, env=self.pane_env(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def wait_until(self, condition, what, process=None, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            if process is not None and process.poll() is not None:
                self.fail('{}: process exited with {}\n{}'.format(what, process.returncode, self.output()))
            time.sleep(0.05)
        self.fail('timed out waiting for {}\n{}'.format(what, self.output()))

    def output(self):
        return '\n'.join(log.read_text(errors='replace') for log in self.logs if log.exists())

    def link_file(self, name):
        return self.state / 'orca-link' / (name + '.json')

    def test_link_lifecycle(self):
        binder = self.start(['open', 'it'], self.binder_env())
        self.addCleanup((self.dir / 'release-herdr').touch)
        self.wait_until((self.dir / 'herdr-env.json').exists, 'Herdr to start', binder)
        # Herdr gets no terminal identity, only installation-level variables.
        self.assertEqual(json.loads((self.dir / 'herdr-env.json').read_text()), ['ORCA_CLI_COMMAND'])
        self.assertNotIn('secret', self.link_file('it').read_text())

        # A linked command, found by name, gets exactly the link's variables and keeps its
        # exit status; the Orca check uses the CLI recorded at bind time.
        dump = self.dir / 'child-env.json'
        result = self.orca_link('--link', 'it', 'dump-env', str(dump), '7')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(dump.read_text()), {
            'ORCA_PANE_KEY': 'pane-1', 'ORCA_TAB_ID': 'tab-1', 'ORCA_TERMINAL_HANDLE': 'term-1',
            'ORCA_AGENT_HOOK_ENDPOINT': str(self.endpoint)})
        self.assertIn('Stop the Herdr server', result.stderr)
        if os.name == 'nt':
            # dump-env is a .cmd file there, and cmd.exe would split this argument.
            refused = self.dir / 'refused.json'
            result = self.orca_link('--link', 'it', 'dump-env', str(refused), 'a&b')
            self.assertEqual(result.returncode, 126, result.stderr)
            self.assertFalse(refused.exists())

        # Without a name, only the link's own project finds it.
        self.assertEqual(self.orca_link('check').returncode, 0)
        other = self.orca_link('check', cwd=self.other)
        self.assertEqual(other.returncode, 1)
        self.assertIn('No live link', other.stderr)

        # Closing Herdr while a command runs keeps the binding until the command exits.
        started, release = self.dir / 'started', self.dir / 'release'
        consumer = self.start(['--link', 'it', sys.executable, '-c', WAIT, str(started), str(release)], self.pane_env())
        self.addCleanup(release.touch)
        self.wait_until(started.exists, 'the command to start', consumer)
        self.assertIn('already in use', self.orca_link('--link', 'it', sys.executable, '-c', 'pass').stderr)
        (self.dir / 'release-herdr').touch()
        self.wait_until(lambda: 'Herdr client closed' in self.output(), 'Herdr to close', binder)
        time.sleep(1.5)  # longer than the binder's 1 s check for a running command
        self.assertIsNone(binder.poll(), 'the binding closed while its command was running')
        release.touch()
        self.assertEqual(consumer.wait(timeout=30), 0)
        self.assertEqual(binder.wait(timeout=30), 0)
        self.assertFalse(self.link_file('it').exists())
        self.assertIn('not live', self.orca_link('check', 'it').stderr)

    def test_a_killed_binder_frees_its_link(self):
        binder = self.start(['bind', 'it'], self.binder_env())
        self.wait_until(self.link_file('it').exists, 'the binding', binder)
        self.assertEqual(self.orca_link('check', 'it').returncode, 0)
        binder.kill()
        binder.wait(timeout=10)
        # Windows releases a dead process's locks after a short delay.
        self.wait_until(lambda: 'not live' in self.orca_link('check', 'it').stderr, 'the link to close')

    @unittest.skipIf(os.name == 'nt', 'sends POSIX signals')
    def test_ctrl_c_does_not_end_a_consumer_before_its_command(self):
        binder = self.start(['bind', 'it'], self.binder_env())
        self.wait_until(self.link_file('it').exists, 'the binding', binder)
        started, release = self.dir / 'started', self.dir / 'release'
        consumer = self.start(['--link', 'it', sys.executable, '-c', WAIT, str(started), str(release)], self.pane_env())
        self.addCleanup(release.touch)
        self.wait_until(started.exists, 'the command to start', consumer)
        for _ in range(2):
            consumer.send_signal(signal.SIGINT)
            time.sleep(0.3)
        self.assertIsNone(consumer.poll(), 'Ctrl-C ended the consumer before its command')
        self.assertIn('already in use', self.orca_link('--link', 'it', sys.executable, '-c', 'pass').stderr)
        release.touch()
        self.assertEqual(consumer.wait(timeout=30), 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows exit codes')
    def test_windows_exit_codes_pass_through(self):
        binder = self.start(['bind', 'it'], self.binder_env())
        self.wait_until(self.link_file('it').exists, 'the binding', binder)
        # 0xC000013A is what a console program reports when Ctrl-C ends it.
        result = self.orca_link('--link', 'it', sys.executable, '-c', 'import os; os._exit(0xC000013A - 2**32)')
        self.assertEqual(result.returncode, 0xC000013A, result.stderr)

if __name__ == '__main__':
    unittest.main()
