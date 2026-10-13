"""Run commands inside Herdr with the Orca terminal context that Orca's agent hooks need.

A binding captures the hook context of one Orca terminal; commands started through
orca-link in a Herdr pane run with that context. No settings are changed and no
daemon runs.
"""
import argparse
import errno
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
# Liveness checks briefly take the lock they test, so a process that takes
# ownership of a link lock retries this many times, 50 ms apart.
LOCK_RETRIES = 3

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
    """Run an Orca CLI command with ``--json`` and return its successful reply.

    Raises:
        RuntimeError: If the CLI is missing, does not answer with a JSON object,
            or reports an error.
    """
    p = subprocess.run([cli(env), *args, '--json'], capture_output=True, timeout=10, env=env)
    try:
        reply = json.loads(p.stdout)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise RuntimeError('Orca CLI did not return JSON')
    if p.returncode or not reply.get('ok'):
        error = reply.get('error')
        code = error.get('code') if isinstance(error, dict) else None
        if code == 'terminal_handle_stale':
            raise RuntimeError('The Orca terminal of this link is gone; run orca-link open in a new Orca terminal')
        if code == 'runtime_unavailable':
            raise RuntimeError('Orca is not running or cannot be reached')
        raise RuntimeError('Orca CLI failed' + (' (' + code + ')' if isinstance(code, str) else ''))
    return reply

def runtime_id(reply):
    """Return the Orca runtime id from a CLI reply, or None."""
    meta = reply.get('_meta')
    return meta.get('runtimeId') if isinstance(meta, dict) else None

def directory():
    p = Path(os.environ.get('XDG_RUNTIME_DIR') or (Path.home() / '.cache')) / 'orca-link'
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if p.is_symlink() or p.stat().st_uid != os.getuid() or p.stat().st_mode & 0o077:
        raise RuntimeError('orca-link state directory must be private (mode 700) and owned by you')
    return p

class LinkBusy(RuntimeError):
    """Another process holds the lock."""

def _try_lock(fd):
    """Take an exclusive lock on ``fd`` without waiting; return False if it is held elsewhere."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            return False
        raise
    return True

def _unlock(fd):
    """Release a lock taken by _try_lock; closing ``fd`` releases a flock lock anyway."""

class Lock:
    """An exclusive lock on an empty lock file, held until close().

    Usable as a context manager. Lock files are never written or deleted.
    """

    def __init__(self, path, retries=0, delay=0.05):
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            for attempt in range(retries + 1):
                if _try_lock(self.fd):
                    return
                if attempt < retries:
                    time.sleep(delay)
            raise LinkBusy('This link is already in use')
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                _unlock(fd)
            finally:
                os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

def locked(path, retries=0):
    """Return a held Lock on ``path``.

    Raises:
        LinkBusy: If another process still holds the lock after ``retries`` retries.
    """
    return Lock(path, retries)

def is_bound(root, name):
    try:
        f = locked(root / (name + '.bind.lock'))
    except LinkBusy:
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
        binding = locked(root / (name + '.bind.lock'), retries=LOCK_RETRIES)
    except LinkBusy:
        if not open_herdr:
            raise
        attach_existing(name)
        return
    with binding:
        try:
            idle = locked(root / (name + '.run.lock'))
        except LinkBusy:
            if not open_herdr:
                raise
            binding.close()
            attach_existing(name)
            return
        idle.close()
        return bind_new(root, name, open_herdr)

def bind_new(root, name, open_herdr):
    # Caller owns the binding lock until this function returns. Remove a file
    # left by a killed binder, so consumers report "starting" instead of using it.
    path = root / (name + '.json')
    path.unlink(missing_ok=True)
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
    try:
        target_lock = locked(root / (target + '.target.lock'))
    except LinkBusy:
        raise RuntimeError('This Orca terminal already backs another link') from None
    with target_lock:
        tmp = root / (name + '.tmp')
        with os.fdopen(os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'w') as f:
            json.dump({'env': env, 'runtime': runtime_id(receipt), 'project': str(project_root())}, f)
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
                    except LinkBusy:
                        pass
                    else:
                        idle.close()
                        break
                time.sleep(1)
        except KeyboardInterrupt:
            print('\nBinding closed. Linked commands are retained; their Orca hook updates may stop.', flush=True)
        finally:
            # A leftover file is harmless: links whose bind lock is free are ignored.
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

def load_binding(root, name):
    """Return the binding file of link ``name`` after checking its privacy and shape.

    Raises:
        RuntimeError: If the file is missing, not private, or not a valid binding.
    """
    path = root / (name + '.json')
    invalid = 'Link file of ' + name + ' is invalid; run orca-link open again'
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise RuntimeError('Link file is not private')
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise RuntimeError('Link ' + name + ' is starting or closing; try again in a moment') from None
    except ValueError:
        raise RuntimeError(invalid) from None
    env = data.get('env') if isinstance(data, dict) else None
    if (not isinstance(env, dict) or not env.get('ORCA_TERMINAL_HANDLE')
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
            or not isinstance(data.get('project', ''), str)):
        raise RuntimeError(invalid)
    return data

def context(root, name):
    if not is_bound(root, name):
        raise RuntimeError('Link ' + name + ' is not live; run orca-link open in an Orca terminal')
    data = load_binding(root, name)
    # Pass the caller's environment through (Herdr variables, credentials); the
    # terminal's ORCA_* values come from the link. The Orca CLI runs with the same
    # environment, so it reaches the Orca instance that issued the terminal handle.
    env = clean_environ()
    env.update({k: v for k, v in data['env'].items() if k in KEYS})
    receipt = rpc('terminal', 'show', '--terminal', data['env']['ORCA_TERMINAL_HANDLE'], env=env)
    if data.get('runtime') != runtime_id(receipt):
        raise RuntimeError('Orca restarted since link ' + name + ' was bound; run orca-link open in a new Orca terminal')
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

def exit_status(returncode):
    """Return the exit status that reports a child's ``Popen.returncode``."""
    if os.name == 'nt':
        # Python 3.11 and 3.12 truncate exit statuses above 2**31 - 1 on Windows.
        return returncode - (1 << 32) if returncode >= 1 << 31 else returncode
    # A child killed by signal N has return code -N; shells report 128 + N.
    return 128 - returncode if returncode < 0 else returncode

def run(root, name, command):
    """Run ``command`` with link ``name``'s context and return its exit status."""
    if not command:
        raise RuntimeError('Supply a command, e.g. claude or codex')
    with locked(root / (name + '.run.lock'), retries=LOCK_RETRIES):
        env = context(root, name)
        try:
            p = subprocess.Popen(command, env=env)
        except FileNotFoundError:
            print('orca-link: command not found: ' + command[0], file=sys.stderr)
            return 127
        except PermissionError:
            print('orca-link: permission denied: ' + command[0], file=sys.stderr)
            return 126
        warned = False
        while True:
            try:
                while p.poll() is None:
                    if not warned and not is_bound(root, name):
                        warned = True
                        print('orca-link: binding closed; the linked command keeps running, but its Orca updates may stop.', file=sys.stderr)
                    try:
                        p.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                return exit_status(p.returncode)
            except KeyboardInterrupt:
                # The command received the same Ctrl-C. Keep waiting, so the link
                # stays busy until the command exits.
                continue

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
