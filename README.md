# orca-link

Run [Herdr](https://herdr.dev) inside an [Orca](https://github.com/stablyai/orca) terminal and let the
coding agents you start in Herdr panes report their status to Orca.

This repository (`herdr-in-orca`) provides the `orca-link` command. It is an independent tool and is
not affiliated with Orca or Herdr.

## Why

Orca gives each of its terminals environment variables (`ORCA_PANE_KEY`, `ORCA_TERMINAL_HANDLE`,
`ORCA_AGENT_HOOK_ENDPOINT`, ...) that its agent hooks use to attribute Claude Code or Codex activity
to that terminal. Herdr panes are started by the Herdr server and inherit its environment: the Orca
variables of whichever terminal started the server, if any, not those of the terminal you are
looking at. Those values also go stale when that terminal closes or Orca restarts.

`orca-link` captures the Orca context of one terminal (a *link*) and starts commands inside Herdr
with that context.

## Requirements

- Linux or macOS.
- Orca with its agent hooks enabled. Orca then sets `ORCA_AGENT_HOOK_ENDPOINT` in its terminals.
- The Orca command-line tool: `orca-ide` on Linux, `orca` on macOS. orca-link calls it from both the
  Orca terminal and Herdr panes, so it must be on `PATH` in both. To use another name or path, set
  `ORCA_CLI_COMMAND` where both inherit it, such as your shell profile.
- [Herdr](https://herdr.dev).
- [uv](https://docs.astral.sh/uv/) (recommended) or pipx. orca-link needs Python 3.11 or newer and has
  no third-party dependencies.

## Install

```sh
uv tool install git+https://github.com/92khsang/herdr-in-orca
```

uv 0.11.17 or newer finds or downloads Python 3.11 or newer automatically; with an older uv, add
`--python 3.11` (or a newer version). To update, run the same command with `--reinstall`. To remove
it, run `uv tool uninstall orca-link`.

With pipx, run `pipx install git+https://github.com/92khsang/herdr-in-orca`, adding
`--python python3.11` (or a newer version) if pipx's default Python is older. Update with
`pipx reinstall orca-link` and remove with `pipx uninstall orca-link`.

## Quick start

In an Orca terminal, in your project:

```sh
orca-link            # same as: orca-link open
```

This binds the terminal and opens Herdr. Then, in a Herdr pane in the same project:

```sh
orca-link claude --continue
orca-link codex resume --last
orca-link my-launcher --some-option
```

## Commands

| Command | Where | What it does |
|---|---|---|
| `orca-link [--link NAME] COMMAND [ARGS...]` | Herdr pane | Runs `COMMAND` with the link's Orca context. |
| `orca-link run NAME COMMAND [ARGS...]` | Herdr pane | Same, with an explicit link name. |
| `orca-link open [NAME]` | Orca terminal | Binds this terminal and opens Herdr. Plain `orca-link` does the same. |
| `orca-link bind NAME` | Orca terminal | Binds this terminal and waits, without opening Herdr. |
| `orca-link check NAME` | anywhere | Checks that the link is live and its Orca terminal still exists. |
| `orca-link --version` | anywhere | Prints the version. |

- Put orca-link's own options before `COMMAND`. Everything after `COMMAND` is passed unchanged.
- A program named `open`, `bind`, `run` or `check` needs `--`: `orca-link -- open file`.
- In a Herdr pane, plain `orca-link` prints the usage.
- Link names are 1-40 letters, digits, hyphens or underscores. `open` defaults to a name derived
  from the project directory.
- Without `--link`, a command uses the live link whose project is the current Git top-level
  directory (or the current directory outside Git), falling back to a link named after the project
  directory. Name links explicitly when several projects share a directory name.
- One link runs one command at a time. To run several commands, bind one link per command in
  separate Orca terminals and choose each with `--link NAME`. When several live links belong to the
  project, a command without `--link` fails and lists them.

## How a link behaves

- Keep the binding terminal open while you work. Bind in a fresh Orca terminal, not inside Herdr.
  One Orca terminal can back only one link at a time.
- Commands are started directly, not through a shell, so shell aliases and functions are not
  available. Put a wrapper script on `PATH`, or set variables inline: `SOME_VAR=value orca-link
  claude`. Your environment is passed through, except `ORCA_*` variables: all of them are removed
  and only the link's captured variables are set.
- Before each command starts, orca-link checks the link's Orca terminal with the Orca CLI. If Orca has
  restarted or the terminal is gone, bind again in a new Orca terminal.
- When you close Herdr after `open`, the binding closes once no linked command is running. After
  that, or with `bind`, `Ctrl-C` in the binding terminal closes the binding at once; while Herdr is
  open there, `Ctrl-C` goes to Herdr. Linked commands keep running after their binding closes, but
  their Orca status updates may stop.
- Running `orca-link` or `orca-link open NAME` again while the link is live, or while its command
  still runs, only reopens Herdr; it never rebinds. Every command started through the link,
  including ones started after reopening, reports to the terminal that bound it. If the binding has
  closed but its command still runs, no link is live: new commands for that name fail until the
  command exits; after that, run `orca-link` in an Orca terminal to bind again. `bind NAME` refuses
  in both cases.

## Limitations

- Only commands started through orca-link are linked.
- After a Herdr server restart, Herdr can resume Claude Code and Codex sessions itself. Those
  sessions start without orca-link and are not linked. To resume them through orca-link instead,
  set `resume_agents_on_restore = false` in the `[session]` section of Herdr's configuration and
  run, for example, `orca-link claude --resume <id>`. With that setting on, exit a restored agent
  before resuming it through orca-link.
- `check` validates the Orca terminal; it does not test that Orca displays agent events.

## State and security

Link state lives in `$XDG_RUNTIME_DIR/orca-link`, or `~/.cache/orca-link` when `XDG_RUNTIME_DIR` is
not set. The directory must be private to you (mode 700), and orca-link refuses to use it otherwise.
It holds lock files and, for each live link, a JSON file with the Orca terminal's identifiers, the
path of Orca's hook endpoint file, Orca's runtime id and the project path. orca-link never stores
Orca's hook port or token, never reads or copies agent credentials, and does not change Orca, Herdr
or agent settings.

[docs/design.md](docs/design.md) describes the binding lifecycle, the lock files and the
environment contract with Orca's hooks.

## Development

```sh
python3 -m unittest discover -s tests -v   # run the tests
python3 orca_link.py --help                # run the checkout without installing
```

## License

MIT
