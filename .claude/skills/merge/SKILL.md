---
name: merge
description: Squash-merge an approved pull request into main and sync the local checkout. Only the user starts this, as /merge <PR number>.
argument-hint: "[pr-number]"
disable-model-invocation: true
---

# Merge pull request $0

Running this command is the user's merge approval for pull request $0 only.

1. Check the required checks: `gh pr checks $0 --required`. If any is failing or missing, stop and
   report instead of merging.
2. If GitHub reports the branch is behind `main` (the required checks are strict):
   - for a Dependabot PR, comment `@dependabot rebase`, then stop and report;
   - for any other PR, run `gh pr update-branch $0 --rebase`, then stop and report.
   Run this command again after the checks pass on the updated branch.
3. Merge with the repository defaults, so the PR title and body become the commit message:
   `gh pr merge $0 --squash --delete-branch`. Do not pass `--subject`, `--body` or `--admin`.
4. Sync: `git switch main && git pull --ff-only && git fetch --prune`, then delete the local branch
   if it still exists.
5. Report the merge commit on `main`.
