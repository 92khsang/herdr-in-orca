## Rules

- Use only the Python standard library at runtime; never add runtime dependencies.
- Never read, copy or store agent credentials or configs, or Orca's hook port and token; orca-link passes environment variables only.
- Keep the state directory and its files private to the user; do not weaken the ownership, mode or symlink checks.
- A linked command keeps the environment it started with: never signal, kill or re-target it when its binding closes or is reopened.
- Start every process from `clean_environ()`, so no caller `ORCA_*` value except `INSTALL_KEYS` reaches Git, Herdr or the Orca CLI; linked commands add exactly their binding's `KEYS` plus the `ORCA_AGENT_HOOK_ROOT` fallback described in docs/design.md.
- Keep state paths, lock-file names and binding JSON keys backward compatible and make new keys optional, because binders and consumers from different versions share one state directory.
- No user- or machine-specific details (personal aliases, config directories, host names, usernames, local install notes) in code, tests, docs, commit messages or PR text; the canonical repository URL is exempt.
- Docs describe current behavior only; no change history or legacy notes.
- Write everything that lands in the repository or on GitHub in English, including commit messages, PR text and review comments.
- Tests never start a real Orca, Herdr or agent; replace them with mocks or stub executables.

## Commands

Run from the repository root:

```sh
python3 -m unittest tests.test_orca_link.WrapperTests.test_named_binding   # one test
python3 -m unittest discover -s tests -v                                  # full suite
python3 orca_link.py --help                                               # run this checkout, not the installed orca-link
```

Required checks on `main`: `ci-ok` (test matrix) and `pr-title` (Conventional Commits title).

## Contributing

- Branch from the latest `origin/main` as `<type>/<slug>`, one logical change per branch; never reuse a squash-merged branch.
- The PR title becomes the commit subject on `main`; mark breaking changes with `!`.
- The PR body becomes the commit body on `main`: describe what changed and why, and post test output as a PR comment instead.
- Agents may create branches, push them and open PRs. Merging, release tags and repository settings changes need the user's explicit approval.

## Pointers

- [docs/design.md](docs/design.md) — binding lifecycle, lock files, the `ORCA_*` contract with Orca's hooks and per-OS behavior; read before changing bind/run, locking or state handling.
