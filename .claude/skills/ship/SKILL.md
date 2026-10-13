---
name: ship
description: Take a finished change in this repository from the working tree to an open pull request with CI running - branch from origin/main, run the tests, commit, push, open the PR and report. Use when the user asks to ship, push, or open a PR for a change. It never merges; merging is the user's /merge.
---

# Ship a change as a pull request

Follow the branch, title and body rules in `AGENTS.md` under Contributing. From the repository root:

1. `git fetch origin`, then `git switch -c <type>/<slug> origin/main`.
2. Run `python3 -m unittest discover -s tests -v`. Stop and report if anything fails.
3. Stage only the intended files with explicit `git add <path>...`, never `git add -A` or `.`, and
   commit with a Conventional Commits header.
4. `git push -u origin HEAD`.
5. Write the PR body to a file as the future commit body, with lines of at most 72 characters
   (GitHub rewraps longer lines in the squash commit), then
   `gh pr create --base main --title "<header>" --body-file <file>`.
6. Post the verification (commands run, pass counts, OS and Python versions) with
   `gh pr comment <n> --body-file <file>`.
7. Run `gh pr checks <n> --watch` in the background and report the result when it finishes.
8. Report the PR URL, the CI result and anything unverified, then stop. Do not merge.
