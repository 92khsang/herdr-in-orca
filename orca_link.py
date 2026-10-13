"""Run commands inside Herdr with the Orca terminal context that Orca's agent hooks need.

A binding captures the hook context of one Orca terminal; commands started through
orca-link in a Herdr pane run with that context. No settings are changed and no
daemon runs.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

__version__ = '0.1.0.dev0'

KEYS = ('ORCA_PANE_KEY', 'ORCA_TAB_ID', 'ORCA_WORKTREE_ID',
        'ORCA_WORKSPACE_ID', 'ORCA_TERMINAL_HANDLE', 'ORCA_AGENT_HOOK_ENDPOINT',
        'ORCA_AGENT_HOOK_ENV', 'ORCA_AGENT_HOOK_VERSION', 'ORCA_AGENT_HOOK_ROOT')

def project_root():
    if shutil.which('git'):
        result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    return Path.cwd().resolve()

def project_name():
    root = project_root()
    name = re.sub(r'[^A-Za-z0-9_-]+', '-', root.name).strip('-')
    if not name:
        import hashlib
        name = 'project-' + hashlib.sha256(str(root).encode()).hexdigest()[:8]
    return name[:40]

def project_link(root):
    project = str(project_root())
    matches = []
    for path in root.glob('*.json'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', path.stem) or not is_bound(root, path.stem):
            continue
        try:
            if json.loads(path.read_text()).get('project') == project:
                matches.append(path.stem)
        except (OSError, ValueError):
            continue
    if len(matches) > 1:
        raise RuntimeError('Multiple links for this project. Use orca-link --link NAME COMMAND: ' + ', '.join(matches))
    return matches[0] if matches else project_name()

def cli():
    value = os.environ.get('ORCA_CLI_COMMAND') or ('orca-ide' if sys.platform.startswith('linux') else 'orca')
    result = shutil.which(value)
    if not result:
        raise RuntimeError('Orca CLI not found: ' + value)
    return result

def rpc(*args):
    p = subprocess.run([cli(), *args, '--json'], capture_output=True, text=True, timeout=10)
    try:
        d = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError('Orca CLI did not return JSON') from None
    if p.returncode or not d.get('ok'):
        raise RuntimeError('Orca terminal unavailable; bind again in a new Orca terminal')
    return d

def directory():
    p = Path(os.environ.get('XDG_RUNTIME_DIR') or (Path.home() / '.cache')) / 'orca-link'
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if p.is_symlink() or p.stat().st_uid != os.getuid() or p.stat().st_mode & 0o077:
        raise RuntimeError('orca-link state directory must be private (mode 700) and owned by you')
    return p

def locked(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    f = os.fdopen(fd, 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError('This link is already in use') from None
    return f

def is_bound(root, name):
    try:
        f = locked(root / (name + '.bind.lock'))
    except RuntimeError:
        return True
    f.close()
    return False

def attach_existing(name):
    print(f'Reopening Herdr for existing link: {name}. Select your workspace in Herdr.', flush=True)
    print('Existing commands keep their original Orca hook target; this does not move them to this terminal.', flush=True)
    try:
        subprocess.run(['herdr'], check=False)
    except KeyboardInterrupt:
        pass

def bind(root, name, open_herdr=False):
    if not os.environ.get('ORCA_PANE_KEY') or os.environ.get('HERDR_ENV') == '1':
        raise RuntimeError('Bind from a fresh Orca terminal, outside Herdr')
    # A live consumer may outlast its binding. Reattach Herdr without
    # replacing its immutable child environment or starting another command.
    try:
        binding = locked(root / (name + '.bind.lock'))
    except RuntimeError:
        if not open_herdr:
            raise
        attach_existing(name)
        return
    with binding:
        try:
            idle = locked(root / (name + '.run.lock'))
        except RuntimeError:
            if not open_herdr:
                raise
            binding.close()
            attach_existing(name)
            return
        idle.close()
        return bind_new(root, name, open_herdr)

def bind_new(root, name, open_herdr):
    # Caller owns the binding lock until this function returns.
    endpoint = os.environ.get('ORCA_AGENT_HOOK_ENDPOINT')
    if not endpoint or not Path(endpoint).is_file():
        raise RuntimeError("Orca hook endpoint file is missing; enable Orca's agent hooks first")
    env = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
    if not env.get('ORCA_TERMINAL_HANDLE'):
        raise RuntimeError('Orca terminal handle is missing')
    receipt = rpc('terminal', 'show', '--terminal', env['ORCA_TERMINAL_HANDLE'])
    # One Orca terminal backs at most one link.
    import hashlib
    target = hashlib.sha256(env['ORCA_PANE_KEY'].encode()).hexdigest()
    with locked(root / (target + '.target.lock')):
        path = root / (name + '.json')
        tmp = root / (name + '.tmp')
        with os.fdopen(os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'w') as f:
            json.dump({'env': env, 'runtime': receipt.get('_meta', {}).get('runtimeId'), 'project': str(project_root())}, f)
        os.replace(tmp, path)
        print(f'Ready: {name}. In a Herdr pane in this project, run orca-link COMMAND [ARGS...].', flush=True)
        print('Keep this terminal open for hook updates. Ctrl-C closes the binding without stopping the linked command.', flush=True)
        try:
            if open_herdr:
                subprocess.run(['herdr'], check=False)
                print('Herdr client closed. Waiting only if a linked command is still running.', flush=True)
            while True:
                if open_herdr:
                    try:
                        idle = locked(root / (name + '.run.lock'))
                    except RuntimeError:
                        pass
                    else:
                        idle.close()
                        break
                time.sleep(1)
        except KeyboardInterrupt:
            print('\nBinding closed. Linked commands are retained; their Orca hook updates may stop.', flush=True)
        finally:
            path.unlink(missing_ok=True)

def context(root, name):
    if not is_bound(root, name):
        raise RuntimeError('Link is not bound; run orca-link bind ' + name + ' in Orca')
    path = root / (name + '.json')
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise RuntimeError('Link file is not private')
    data = json.loads(path.read_text())
    receipt = rpc('terminal', 'show', '--terminal', data['env']['ORCA_TERMINAL_HANDLE'])
    if data.get('runtime') != receipt.get('_meta', {}).get('runtimeId'):
        raise RuntimeError('Orca restarted; bind again')
    # Pass the caller's environment through (Herdr variables, credentials); every
    # ORCA_* value comes from the link instead.
    env = {k: v for k, v in os.environ.items() if not k.startswith('ORCA_')}
    env.update({k: v for k, v in data['env'].items() if k in KEYS})
    # Codex's installed hook guard requires a hook root before loading endpoint.env.
    native_root = Path.home() / '.orca'
    if not env.get('ORCA_AGENT_HOOK_ROOT') and (native_root / 'agent-hooks/codex-hook.sh').is_file():
        env['ORCA_AGENT_HOOK_ROOT'] = str(native_root)
    return env

def run(root, name, command):
    if not command:
        raise RuntimeError('Supply a command, e.g. claude or codex')
    with locked(root / (name + '.run.lock')):
        env = context(root, name)
        p = subprocess.Popen(command, env=env)
        try:
            while p.poll() is None:
                if not is_bound(root, name):
                    print('orca-link: binding closed; the linked command keeps running, but its Orca updates may stop.', file=sys.stderr)
                    return p.wait()
                try:
                    p.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            return p.returncode
        except KeyboardInterrupt:
            # The foreground process receives the same terminal Ctrl-C.
            return p.wait()

def _main(argv):
    parser = argparse.ArgumentParser(
        prog='orca-link',
        description="Wrap any command with the current project's Orca hook context.",
        usage='orca-link [--link NAME] COMMAND [ARGS...] | open [NAME] | bind NAME | run NAME COMMAND [ARGS...] | check NAME',
        epilog='In an Orca terminal: orca-link open. In a Herdr pane: orca-link COMMAND. Use -- before commands named open, bind, run or check.')
    parser.add_argument('--version', action='version', version='orca-link ' + __version__)
    parser.add_argument('--link', metavar='NAME', help='Use the link named NAME instead of the one matched to the current project')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    if not argv:
        if os.environ.get('HERDR_ENV') == '1':
            parser.print_help()
            return 0
        argv = ['open']
    builtins = ('open', 'bind', 'run', 'check')
    if argv[0] in builtins:
        action = argv[0]
        sub = argparse.ArgumentParser(prog='orca-link ' + action)
        if action == 'open':
            sub.add_argument('name', nargs='?', default=None)
        else:
            sub.add_argument('name')
        if action == 'run':
            sub.add_argument('command', nargs=argparse.REMAINDER)
        a = sub.parse_args(argv[1:])
        name = a.name if a.name is not None else project_name()
        command = a.command if action == 'run' else []
        if command[:1] == ['--']:
            command = command[1:]
        if action == 'run' and not command:
            sub.error('COMMAND is required')
    else:
        a = parser.parse_args(argv)
        command = a.command
        if command[:1] == ['--']:
            command = command[1:]
        if not command:
            parser.error('COMMAND is required')
        action = 'run'
        name = a.link
    if name is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', name):
        parser.error('name must contain 1-40 letters, digits, hyphens or underscores')
    root = directory()
    if name is None:
        name = project_link(root)
    if action in ('bind', 'open'):
        bind(root, name, open_herdr=action == 'open')
    elif action == 'run':
        return run(root, name, command)
    else:
        context(root, name)
        print('Link is live; Orca terminal validated. UI event delivery is not tested by check.')
    return 0

def main(argv=None):
    """Run orca-link and return its exit status.

    Args:
        argv: Arguments without the program name; defaults to ``sys.argv[1:]``.

    Returns:
        For the run forms, the linked command's ``Popen.returncode`` (``-N`` if
        signal N killed it). 0 for ``open``, ``bind`` and ``check``, including a
        binding closed with Ctrl-C. 1 after a reported error. 130 if Ctrl-C
        interrupts orca-link before a binding or command starts.

    Raises:
        SystemExit: From argparse, with status 0 for ``--help`` and ``--version``
            and 2 for usage errors.
    """
    try:
        return _main(sys.argv[1:] if argv is None else list(argv))
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        print('orca-link: ' + str(e), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
