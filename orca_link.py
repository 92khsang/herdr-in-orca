"""Run commands inside Herdr with the Orca terminal context that Orca's agent hooks need.

A binding captures the hook context of one Orca terminal; commands started through
orca-link in a Herdr pane run with that context. No settings are changed and no
daemon runs.
"""
import argparse
import errno
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

__version__ = '0.1.1.dev0'

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
# Windows has no O_NOFOLLOW; there the per-user state directory guards against links.
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
# Program files that CreateProcess can start on Windows.
WINDOWS_RUNNABLE = ('.com', '.exe', '.bat', '.cmd')
# Characters that cmd.exe reinterprets in the arguments of a .bat or .cmd file, and
# those it still expands inside the quotes that always surround the file's path.
BATCH_UNSAFE = frozenset('"%!^&|<>()\r\n\0')
BATCH_PATH_UNSAFE = frozenset('"%!\r\n\0')
# Windows device names, which cannot name a link's state files there.
RESERVED_NAMES = re.compile(r'(con|prn|aux|nul|com[0-9]|lpt[0-9])', re.IGNORECASE)

def clean_environ():
    """Return a copy of this process's environment without terminal-specific ORCA_* values."""
    return {k: v for k, v in os.environ.items() if not k.startswith('ORCA_') or k in INSTALL_KEYS}

def resolve_executable(name, path=None):
    """Return the absolute path of program ``name`` on ``path`` (default: PATH), or None.

    On Windows only the PATH directories are searched, never the current directory,
    and only .com, .exe, .bat and .cmd files count, so a project cannot shadow a
    tool and extensionless shell shims are skipped.
    """
    if os.name == 'nt':
        return windows_executable(name, os.environ.get('PATH', '') if path is None else path,
                                  os.environ.get('PATHEXT', ''))
    found = shutil.which(name, path=path)
    return os.path.abspath(found) if found else None

def windows_executable(name, path, pathext, isfile=os.path.isfile):
    """Resolve ``name`` the way resolve_executable() does on Windows."""
    exts = [e for e in (x.strip().lower() for x in pathext.split(';')) if e in WINDOWS_RUNNABLE]
    exts = exts or list(WINDOWS_RUNNABLE)

    def candidates(base):
        return [base] if ntpath.splitext(base)[1].lower() in exts else [base + e for e in exts]
    if ntpath.dirname(name):
        return next((ntpath.abspath(c) for c in candidates(name) if isfile(c)), None)
    for directory in path.split(';'):
        directory = directory.strip().strip('"')
        # Only entries with a drive or UNC share are searched; "." and relative or
        # drive-relative entries would depend on the current directory.
        if ntpath.splitdrive(directory)[0] and ntpath.isabs(directory):
            for candidate in candidates(ntpath.join(directory, name)):
                if isfile(candidate):
                    return candidate
    return None

def is_batch(program):
    return ntpath.splitext(program)[1].lower() in ('.bat', '.cmd')

def unsafe_batch_argument(program, args):
    """Return the first of ``program`` and ``args`` that cmd.exe would reinterpret, or None.

    Only .bat and .cmd programs are affected: Windows runs them through cmd.exe,
    which parses their command line again. command_line() always quotes their
    path, so only ``%`` and ``!`` (and line breaks) are a problem there.
    """
    if not is_batch(program):
        return None
    if BATCH_PATH_UNSAFE & set(program):
        return program
    return next((arg for arg in args if BATCH_UNSAFE & set(arg)), None)

def command_line(program, args):
    """Return the ``args`` for Popen that start ``program`` with ``args``.

    On Windows a .bat or .cmd file's path is always quoted, because cmd.exe
    parses its command line again and list2cmdline quotes only paths with spaces.
    """
    if os.name == 'nt' and is_batch(program):
        return ' '.join(['"' + program + '"', subprocess.list2cmdline(args)]).rstrip()
    return [program, *args]

def absolute(path):
    """Return whether ``path`` is absolute, with a drive or share on Windows."""
    if os.name == 'nt':
        return bool(ntpath.splitdrive(path)[0]) and ntpath.isabs(path)
    return os.path.isabs(path)

def herdr_program():
    program = resolve_executable('herdr')
    if not program:
        raise RuntimeError('Herdr not found on PATH')
    return program

def project_root():
    git = resolve_executable('git')
    if git:
        result = subprocess.run([git, 'rev-parse', '--show-toplevel'], capture_output=True, env=clean_environ())
        if result.returncode == 0:
            return Path(os.fsdecode(result.stdout).strip()).resolve()
    return Path.cwd().resolve()

def project_name():
    """Return the default link name: the project directory name and a hash of its path."""
    root = project_root()
    base = re.sub(r'[^A-Za-z0-9_-]+', '-', root.name).strip('-')[:31] or 'project'
    return base + '-' + hashlib.sha256(os.fsencode(os.path.normcase(str(root)))).hexdigest()[:8]

def project_links(root):
    """Return the sorted names of the live links bound in the current project."""
    project = os.path.normcase(str(project_root()))
    matches = []
    for path in root.glob('*.json'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', path.stem) or not is_bound(root, path.stem):
            continue
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get('project'), str) and os.path.normcase(data['project']) == project:
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
    result = resolve_executable(value, path=env.get('PATH'))
    if not result:
        raise RuntimeError('Orca CLI not found: ' + value)
    return result

class TerminalGone(RuntimeError):
    """Orca no longer knows the terminal."""

# Orca CLI error codes that mean the terminal handle no longer names a live terminal.
GONE_CODES = ('terminal_handle_stale', 'terminal_gone', 'terminal_exited', 'terminal_not_found')

def rpc(*args, env=None, program=None):
    """Run an Orca CLI command with ``--json`` and return its successful reply.

    ``program`` is the Orca CLI to run; by default it is looked up in ``env``.

    Raises:
        RuntimeError: If the CLI is missing, does not answer with a JSON object,
            or reports an error.
    """
    # The reply goes to a file rather than a pipe: a CLI started through a .cmd file
    # runs in a child process that would keep a pipe open after a timeout.
    with tempfile.TemporaryFile() as out:
        p = subprocess.Popen(command_line(program or cli(env), [*args, '--json']), stdin=subprocess.DEVNULL,
                             stdout=out, stderr=subprocess.DEVNULL, env=env)
        try:
            returncode = p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            kill_tree(p)
            raise RuntimeError('Orca CLI did not answer within 10 seconds') from None
        out.seek(0)
        stdout = out.read()
    try:
        reply = json.loads(stdout)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise RuntimeError('Orca CLI did not return JSON')
    if returncode or not reply.get('ok'):
        error = reply.get('error')
        code = error.get('code') if isinstance(error, dict) else None
        if code in GONE_CODES:
            raise TerminalGone('The Orca terminal is gone')
        if code == 'runtime_unavailable':
            raise RuntimeError('Orca is not running or cannot be reached')
        raise RuntimeError('Orca CLI failed' + (' (' + code + ')' if isinstance(code, str) else ''))
    return reply

def kill_tree(p):
    """Kill process ``p`` and, on Windows, the processes it started."""
    if os.name == 'nt':
        taskkill = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'taskkill.exe')
        subprocess.run([taskkill, '/F', '/T', '/PID', str(p.pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    p.kill()
    p.wait()

def runtime_id(reply):
    """Return the Orca runtime id from a CLI reply, or None."""
    meta = reply.get('_meta')
    return meta.get('runtimeId') if isinstance(meta, dict) else None

def private(path):
    """Return whether ``path`` belongs to this user and grants no group or other access.

    Windows reports synthetic permission bits and no owner. There the state
    directory's place in the user profile, and on current Pythons the owner-only
    ACL that mkdir(mode=0o700) applies, keep the files private.
    """
    if os.name == 'nt':
        return True
    info = path.stat()
    return info.st_uid == os.getuid() and not info.st_mode & 0o077

def directory():
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    else:
        base = Path(os.environ.get('XDG_RUNTIME_DIR') or Path.home() / '.cache')
    p = base / 'orca-link'
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if p.is_symlink() or not private(p):
        raise RuntimeError('orca-link state directory must be private (mode 700) and owned by you')
    return p

class LinkBusy(RuntimeError):
    """Another process holds the lock."""

def _try_lock(fd):
    """Take an exclusive lock on ``fd`` without waiting; return False if it is held elsewhere."""
    if os.name == 'nt':
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EDEADLOCK):
                return False
            raise
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            return False
        raise
    return True

def _unlock(fd):
    """Release a lock taken by _try_lock.

    Closing ``fd`` releases a flock lock at once. Windows releases the locks of a
    closed handle only eventually, so there the lock is released explicitly.
    """
    if os.name == 'nt':
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

class Lock:
    """An exclusive lock on an empty lock file, held until close().

    Usable as a context manager. Lock files are never written or deleted.
    """

    def __init__(self, path, retries=0, delay=0.05):
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR | NOFOLLOW, 0o600)
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

    fd = None

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

    def __del__(self):
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
    herdr = herdr_program()
    print(f'Reopening Herdr for existing link: {name}. Select your workspace in Herdr.', flush=True)
    print('Existing commands keep their original Orca hook target; this does not move them to this terminal.', flush=True)
    try:
        subprocess.run(command_line(herdr, []), env=clean_environ(), check=False)
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
        # Holding the bind lock proves that no binder is alive, so a binding file
        # left by a killed binder is stale; remove it before anyone reads it.
        (root / (name + '.json')).unlink(missing_ok=True)
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
    # Caller owns the binding lock until this function returns.
    path = root / (name + '.json')
    endpoint = os.environ.get('ORCA_AGENT_HOOK_ENDPOINT')
    if not endpoint or not Path(endpoint).is_file():
        raise RuntimeError("Orca hook endpoint file is missing; enable Orca's agent hooks first")
    env = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
    if not env.get('ORCA_TERMINAL_HANDLE'):
        raise RuntimeError('Orca terminal handle is missing')
    herdr = herdr_program() if open_herdr else None
    # Check the terminal with the environment that consumers will use, and record
    # the CLI found here: Herdr panes may not have it on PATH (on Windows they take
    # PATH from the registry).
    check_env = {**clean_environ(), **env}
    program = cli(check_env)
    receipt = rpc('terminal', 'show', '--terminal', env['ORCA_TERMINAL_HANDLE'], env=check_env, program=program)
    # One Orca terminal backs at most one link.
    target = hashlib.sha256(env['ORCA_PANE_KEY'].encode()).hexdigest()
    try:
        target_lock = locked(root / (target + '.target.lock'))
    except LinkBusy:
        raise RuntimeError('This Orca terminal already backs another link') from None
    with target_lock:
        tmp = root / (name + '.tmp')
        with os.fdopen(os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | NOFOLLOW, 0o600), 'w', encoding='utf-8') as f:
            json.dump({'env': env, 'runtime': runtime_id(receipt), 'project': str(project_root()), 'cli': program}, f)
        os.replace(tmp, path)
        print(f'Ready: {name}. In a Herdr pane in this project, run orca-link COMMAND [ARGS...].', flush=True)
        print('Keep this terminal open for hook updates. Ctrl-C closes the binding without stopping the linked command.', flush=True)
        try:
            if open_herdr:
                subprocess.run(command_line(herdr, []), env=clean_environ(), check=False)
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
    invalid = 'Link file of ' + name + ' is invalid; run orca-link open ' + name + ' again'
    try:
        if path.is_symlink() or not private(path):
            raise RuntimeError('Link file is not private')
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise RuntimeError('Link ' + name + ' is starting or closing; try again in a moment') from None
    except ValueError:
        raise RuntimeError(invalid) from None
    env = data.get('env') if isinstance(data, dict) else None
    if (not isinstance(env, dict) or not env.get('ORCA_TERMINAL_HANDLE')
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
            or not isinstance(data.get('project', ''), str) or not isinstance(data.get('cli', ''), str)):
        raise RuntimeError(invalid)
    return data

def context(root, name):
    if not is_bound(root, name):
        raise RuntimeError('Link ' + name + ' is not live; run orca-link open ' + name + ' in an Orca terminal')
    data = load_binding(root, name)
    # Pass the caller's environment through (Herdr variables, credentials); the
    # terminal's ORCA_* values come from the link. The Orca CLI runs with the same
    # environment, so it reaches the Orca instance that issued the terminal handle.
    env = clean_environ()
    env.update({k: v for k, v in data['env'].items() if k in KEYS})
    rebind = 'run orca-link open ' + name + ' in a new Orca terminal'
    # Prefer the CLI the binder found; it may be missing after an Orca update.
    recorded = data.get('cli')
    program = resolve_executable(recorded) if recorded and absolute(recorded) else None
    try:
        receipt = rpc('terminal', 'show', '--terminal', data['env']['ORCA_TERMINAL_HANDLE'], env=env, program=program)
    except TerminalGone:
        raise RuntimeError('The Orca terminal of link ' + name + ' is gone; ' + rebind) from None
    if data.get('runtime') != runtime_id(receipt):
        raise RuntimeError('Orca restarted since link ' + name + ' was bound; ' + rebind)
    warn_inherited_identity(env.get('ORCA_PANE_KEY'))
    # On Linux and macOS, Codex's installed hook guard requires a hook root before
    # loading endpoint.env. Orca's Windows hook commands have no such guard.
    if os.name != 'nt' and not env.get('ORCA_AGENT_HOOK_ROOT'):
        native_root = Path.home() / '.orca'
        if (native_root / 'agent-hooks' / 'codex-hook.sh').is_file():
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
        # The command receives Ctrl-C itself. orca-link ignores it until the command
        # exits, so the link stays busy for as long as the command runs. A Python
        # handler (unlike SIG_IGN) is reset to the default in the started command.
        ignore = lambda signum, frame: None
        signals = [signal.SIGINT] + ([signal.SIGBREAK] if hasattr(signal, 'SIGBREAK') else [])
        previous = [(signum, signal.signal(signum, ignore)) for signum in signals]
        try:
            return wait_for(root, name, command, env)
        finally:
            for signum, handler in previous:
                signal.signal(signum, handler)

def wait_for(root, name, command, env):
    if os.name == 'nt':
        # CreateProcess would search the current directory and append only .exe.
        program = resolve_executable(command[0], path=env.get('PATH'))
        if not program:
            print('orca-link: command not found: ' + command[0], file=sys.stderr)
            return 127
        unsafe = unsafe_batch_argument(program, command[1:])
        if unsafe is not None:
            print('orca-link: cannot run ' + command[0] + ': it is a batch file, and cmd.exe would '
                  'reinterpret ' + repr(unsafe) + '; use a native executable, or keep " % ! ^ & | < > ( ) '
                  'and line breaks out of its arguments and % and ! out of its path', file=sys.stderr)
            return 126
        args = command_line(program, command[1:])
    else:
        args = command
    try:
        p = subprocess.Popen(args, env=env)
    except FileNotFoundError:
        print('orca-link: command not found: ' + command[0], file=sys.stderr)
        return 127
    except OSError as e:
        print('orca-link: cannot run ' + command[0] + ': ' + (e.strerror or str(e)), file=sys.stderr)
        return 126
    warned = False
    while p.poll() is None:
        if not warned:
            try:
                bound = is_bound(root, name)
            except OSError:
                bound = False
            if not bound:
                warned = True
                print('orca-link: binding closed; the linked command keeps running, but its Orca updates may stop.', file=sys.stderr)
        try:
            p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    return exit_status(p.returncode)

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
    if name is not None and RESERVED_NAMES.fullmatch(name):
        parser.error(name + ' is a reserved device name on Windows')
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
        For the run forms, the command's status from exit_status(): its exit
        code, 128 + N if signal N ended it on Linux and macOS, 127 if it was not
        found and 126 if it could not be started. 0 for ``open``, ``bind`` and
        ``check``, including a binding closed with Ctrl-C. 1 after any other
        reported error. 130 if Ctrl-C interrupts orca-link before a binding or
        command starts.

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
