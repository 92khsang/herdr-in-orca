# Design

This document explains how orca-link works internally. For usage, see the [README](../README.md).

## Problem

Orca starts each of its terminals with environment variables that identify the terminal and tell
agent hooks where to report:

- identity: `ORCA_PANE_KEY`, `ORCA_TAB_ID`, `ORCA_WORKTREE_ID`, `ORCA_WORKSPACE_ID`,
  `ORCA_TERMINAL_HANDLE`;
- hook endpoint: `ORCA_AGENT_HOOK_ENDPOINT`, `ORCA_AGENT_HOOK_ENV`, `ORCA_AGENT_HOOK_VERSION`.

Orca's hook scripts for Claude Code and Codex read these variables from the agent's environment.
`ORCA_AGENT_HOOK_ENDPOINT` names a file (`endpoint.env` on Linux and macOS) that holds the hook
server's port and token. The scripts read that file on every call, so the port and token never need
to be copied.

A Herdr pane is a child of the Herdr server, which keeps the environment of the client that started
it. An agent started in a pane therefore carries the Orca identity of whichever terminal started the
server, if any, instead of the identity of the terminal that is showing Herdr, and that identity
goes stale when its terminal closes or Orca restarts.

## Roles

- The **binder** runs in an Orca terminal (`open` or `bind`). It captures that terminal's context
  into a *link* and keeps the link alive while it runs.
- A **consumer** runs in a Herdr pane (`orca-link COMMAND` or `run`). It starts one command with the
  link's context and waits for it.

## State directory

The state directory is `$XDG_RUNTIME_DIR/orca-link`, or `~/.cache/orca-link` when `XDG_RUNTIME_DIR`
is unset or empty. It must be a real
directory owned by the user with no group or other permission bits. For a link named `NAME` it
holds:

| File | Meaning |
|---|---|
| `NAME.bind.lock` | Locked by the binder for the link's lifetime. A link is *live* while this lock is held. |
| `NAME.run.lock` | Locked by the consumer while its command runs, so a link runs one command at a time. |
| `<sha256 of ORCA_PANE_KEY>.target.lock` | Locked by the binder so one Orca terminal backs at most one link. |
| `NAME.json` | Written by the binder: the captured variables, Orca's runtime id and the project path. |

Locks are non-blocking `flock` locks on empty files. Liveness checks acquire and release the lock
they test, so a process taking ownership of a link lock (the binder's bind lock or the consumer's
run lock) retries three times, 50 ms apart, before it reports the link as busy. Lock files are never deleted: removing a file that another process has open or locked
would let two processes hold "the same" lock on different files. `NAME.json` is written to a
temporary file with mode 600 and renamed into place, and the binder removes it when the binding
ends normally. A file left behind by a killed binder is ignored, because its bind lock is free.

## Binding

The binder refuses to run without `ORCA_PANE_KEY` or inside Herdr (`HERDR_ENV=1`). It then:

1. takes `NAME.bind.lock` and removes any `NAME.json`: a free bind lock means no binder is alive,
   so the file was left by a killed binder, and consumers must report the link as starting
   instead of using it;
2. checks that no consumer holds `NAME.run.lock`;
3. requires an existing `ORCA_AGENT_HOOK_ENDPOINT` file and an `ORCA_TERMINAL_HANDLE`;
4. asks Orca for the terminal with `<cli> terminal show --terminal <handle> --json` and records
   `_meta.runtimeId`, which changes whenever Orca restarts;
5. takes the target lock and writes `NAME.json`;
6. for `open`, runs the Herdr client in the foreground (see [Herdr](#herdr)). When the client exits, the binder waits
   until no consumer holds `NAME.run.lock`, then ends the binding. For `bind`, it waits for Ctrl-C.

Without a name, `open` reuses one of this project's live links. Otherwise it names the new link
after the project directory plus the first 8 hex digits of a SHA-256 hash of the project path, so
two projects with the same directory name do not share lock files.

If `NAME.bind.lock` or `NAME.run.lock` is already held, `open` reattaches the Herdr client instead
of binding again, and `bind` fails. Reattaching does not rebind: later consumers use the existing
`NAME.json`, or find no live link when only a running consumer holds `NAME.run.lock`. It never
changes the environment of commands that are already running.

## Running a command

The consumer resolves the link name: `--link NAME` or `run NAME` when given, otherwise the only live
link whose `project` equals the current project (the Git top-level directory, or the current
directory outside Git). No match and several matches are both errors. It then:

1. takes `NAME.run.lock`;
2. checks that the link is live and that `NAME.json` is private;
3. builds the child environment: the caller's environment without its `ORCA_*` variables except
   the installation-level `INSTALL_KEYS` (`ORCA_USER_DATA_PATH`, `ORCA_CLI_COMMAND`,
   `ORCA_CLI_BIN_DIR`, `ORCA_WSL_CLI_DIR`), plus the captured variables;
4. checks with the Orca CLI, run in that environment so that it reaches the Orca instance that
   issued the terminal handle, that the terminal still exists with the recorded runtime id;
5. starts the command directly, without a shell, and polls once per second. If the binding ends
   while the command runs, the command keeps running and the consumer reports once that hook
   updates may stop. While the command runs, the consumer handles Ctrl-C with a handler that does
   nothing, so `NAME.run.lock` stays held until the command exits; the command itself gets the
   default handling, because exec resets caught signals. The consumer then exits with the
   command's status, 128 + N for signal N, 127 if the command was not found and 126 if it could
   not be started.

`check` resolves a missing name the same way and performs steps 2 to 4.

Captured variables are `ORCA_PANE_KEY`, `ORCA_AGENT_PANE` (Orca's copy of the pane key for hook
runners that drop variables named like secrets), `ORCA_TAB_ID`, `ORCA_WORKTREE_ID`,
`ORCA_WORKSPACE_ID`, `ORCA_TERMINAL_HANDLE`, `ORCA_AGENT_HOOK_ENDPOINT`, `ORCA_AGENT_HOOK_ENV`,
`ORCA_AGENT_HOOK_VERSION`, `ORCA_AGENT_HOOK_ROOT` and `ORCA_USER_DATA_PATH`. Variables that a
binding terminal does not have are simply absent. Orca's Codex hook command runs its script only when
`ORCA_AGENT_HOOK_ROOT` or `ORCA_AGENT_HOOK_PORT` is set. orca-link never passes the port, so when
Orca did not set `ORCA_AGENT_HOOK_ROOT` and `~/.orca/agent-hooks/codex-hook.sh` exists, it sets
`ORCA_AGENT_HOOK_ROOT` to `~/.orca`.

## Herdr

Every process orca-link starts (Git, Herdr, the Orca CLI and linked commands) gets the caller's
environment without its `ORCA_*` variables except `INSTALL_KEYS`. The Herdr client gets nothing
more. When that client starts a new Herdr server, the server and every pane in it carry no terminal
identity, so only commands started through orca-link report to Orca. A server that is already
running keeps the environment it was started with. When a consumer runs in a pane whose
`ORCA_PANE_KEY` differs from its link's, that server was started with another terminal's
environment, usually by running `herdr` directly in an Orca terminal, and the consumer prints a
note saying so.

## Orca command-line tool

orca-link calls `orca-ide` on Linux, where `orca` is usually the GNOME screen reader, and `orca`
elsewhere. `ORCA_CLI_COMMAND` overrides the name or path; each orca-link process reads it from its
own environment, so binders read it in the Orca terminal and consumers in the Herdr pane. Orca sets
it itself in WSL terminals.
