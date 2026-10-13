"""Run commands inside Herdr with the Orca terminal context that Orca's agent hooks need.

A binding captures the hook context of one Orca terminal; commands started through
orca-link in a Herdr pane run with that context. No settings are changed and no
daemon runs.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

__version__ = '0.1.0.dev0'

# Captured from the binding terminal and set for every linked command.
KEYS = ('ORCA_PANE_KEY', 'ORCA_AGENT_PANE', 'ORCA_TAB_ID', 'ORCA_WORKTREE_ID',
        'ORCA_WORKSPACE_ID', 'ORCA_TERMINAL_HANDLE', 'ORCA_AGENT_HOOK_ENDPOINT',
        'ORCA_AGENT_HOOK_ENV', 'ORCA_AGENT_HOOK_VERSION', 'ORCA_AGENT_HOOK_ROOT',
        'ORCA_USER_DATA_PATH')
# ORCA_* variables that locate the Orca installation rather than one terminal. They
# are the only ORCA_* values passed on from the caller to Herdr and linked commands.
INSTALL_KEYS = ('ORCA_USER_DATA_PATH', 'ORCA_CLI_COMMAND', 'ORCA_CLI_BIN_DIR', 'ORCA_WSL_CLI_DIR')

def clean_environ():
    """Return a copy of this process's environment without terminal-specific ORCA_* values."""
    return {k: v for k, v in os.environ.items() if not k.startswith('ORCA_') or k in INSTALL_KEYS}

def project_root():
    if shutil.which('git'):
        result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                                env=clean_environ())
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    return Path.cwd().resolve()

def project_name():
    """Return the default link name: the project directory name and a hash of its path."""
    root = project_root()
    base = re.sub(r'[^A-Za-z0-9_-]+', '-', root.name).strip('-')[:31] or 'project'
    return base + '-' + hashlib.sha256(os.fsencode(os.path.normcase(str(root)))).hexdigest()[:8]

def project_links(root):
    """Return the sorted names of the live links bound in the current project."""
    project = str(project_root())
    matches = []
    for path in root.glob('*.json'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', path.stem) or not is_bound(root, path.stem):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get('project') == project:
            matches.append(path.stem)
    return sorted(matches)

def project_link(root, usage):
    """Return the only live link bound in the current project.

    Raises:
        RuntimeError: If no live link or several live links match; the message
            suggests ``usage`` for naming a link explicitly.
    """
    links = project_links(root)
    if len(links) == 1:
        return links[0]
    project = str(project_root())
    if links:
        raise RuntimeError('Several live links belong to ' + project + ' (' + ', '.join(links) + '); use ' + usage)
    raise RuntimeError('No live link for ' + project + '; run orca-link in an Orca terminal in this project, or use ' + usage)

def cli(env=None):
    env = os.environ if env is None else env
    value = env.get('ORCA_CLI_COMMAND') or ('orca-ide' if sys.platform.startswith('linux') else 'orca')
    result = shutil.which(value, path=env.get('PATH'))
    if not result:
        raise RuntimeError('Orca CLI not found: ' + value)
    return result

def rpc(*args, env=None):
    p = subprocess.run([cli(env), *args, '--json'], capture_output=True, text=True, timeout=10, env=env)
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
        subprocess.run(['herdr'], env=clean_environ(), check=False)
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
    # Check the terminal with the environment that consumers will use.
    receipt = rpc('terminal', 'show', '--terminal', env['ORCA_TERMINAL_HANDLE'], env={**clean_environ(), **env})
    # One Orca terminal backs at most one link.
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
                subprocess.run(['herdr'], env=clean_environ(), check=False)
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
    # Pass the caller's environment through (Herdr variables, credentials); the
    # terminal's ORCA_* values come from the link. The Orca CLI runs with the same
    # environment, so it reaches the Orca instance that issued the terminal handle.
    env = clean_environ()
    env.update({k: v for k, v in data['env'].items() if k in KEYS})
    receipt = rpc('terminal', 'show', '--terminal', data['env']['ORCA_TERMINAL_HANDLE'], env=env)
    if data.get('runtime') != receipt.get('_meta', {}).get('runtimeId'):
        raise RuntimeError('Orca restarted; bind again')
    warn_inherited_identity(env.get('ORCA_PANE_KEY'))
    # Codex's installed hook guard requires a hook root before loading endpoint.env.
    native_root = Path.home() / '.orca'
    if not env.get('ORCA_AGENT_HOOK_ROOT') and (native_root / 'agent-hooks/codex-hook.sh').is_file():
        env['ORCA_AGENT_HOOK_ROOT'] = str(native_root)
    return env

def warn_inherited_identity(linked_pane_key):
    """Note when this Herdr pane carries another Orca terminal's identity.

    A pane inherits the environment of the client that started its Herdr server.
    A pane key equal to the link's comes from orca-link itself, for example in a
    shell started through orca-link, and is not reported.
    """
    pane_key = os.environ.get('ORCA_PANE_KEY')
    if os.environ.get('HERDR_ENV') == '1' and pane_key and pane_key != linked_pane_key:
        print("orca-link: note: this Herdr server was started with another Orca terminal's environment, "
              'so commands started here without orca-link report to that terminal. Stop the Herdr '
              'server and start it again with `orca-link open` to fix this.', file=sys.stderr)

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
        usage='orca-link [--link NAME] COMMAND [ARGS...] | open [NAME] | bind NAME | run NAME COMMAND [ARGS...] | check [NAME]',
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
        if action in ('open', 'check'):
            sub.add_argument('name', nargs='?', default=None)
        else:
            sub.add_argument('name')
        if action == 'run':
            sub.add_argument('command', nargs=argparse.REMAINDER)
        a = sub.parse_args(argv[1:])
        name = a.name
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
    if name is None and action == 'open':
        # Reopen this project's live link when there is one; reopening any of
        # several is fine because it only attaches another Herdr client.
        links = project_links(root)
        default = project_name()
        name = default if not links or default in links else links[0]
    elif name is None:
        name = project_link(root, 'orca-link check NAME' if action == 'check' else 'orca-link --link NAME COMMAND')
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
