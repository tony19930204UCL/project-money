# Decided issue -> web ChatGPT PR -> Main acceptance

## Ownership

- Main makes architecture, behavior and acceptance decisions; issues must describe the chosen solution rather than delegate design.
- The client manually provides one curated issue batch per web ChatGPT conversation.
- ChatGPT changes source/tests on a topic branch and opens a PR. No broker orders, production database changes, credential configuration, autonomous deployment, or policy/persona changes.
- Main independently reviews the actual PR diff, runs affected tests plus full regression, checks secret/runtime exclusions, and deploys only inside existing client authority with rollback and live readback.
- A merged PR is not proof of deployment. Unit tests are not proof of broker or continuous live acceptance.

## Issue contract

Each issue includes: concrete decided behavior, specific files/interfaces, non-goals, compatibility, source-only reproduction, runnable acceptance, risk and rollback. No runtime logs, accounts or secrets in issues/PRs.

Main opens issues without per-issue notifications. Before a batch handoff Main rechecks open issues/PRs, closes stale/duplicate work, groups related issues into one-conversation-sized batches, and states batch membership and dependency order.

## Urgent exception

Outage, inability to place orders, or wrong accounting requires immediate client notification. Several hours without a client reply authorizes urgent Antigravity repair, not routine implementation. The incident must have a concrete recorded escalation deadline, verified last-reply check, bounded executor, rollback and acceptance evidence. This document does not itself create a running escalation timer.

## Required source regression checks

Pull requests run two GitHub Actions checks from `.github/workflows/source-regression.yml`:

- `Source regression / frontend`: fresh Node 22 install, production frontend build, and Vitest.
- `Source regression / backend`: fresh Python 3.13 install, frontend build required by backend contract tests, then the full Python pytest suite.

The equivalent local commands are:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
cd vendor/shioaji-pro-app
npm ci --ignore-scripts
npm run build
npm test
cd ../..
.venv/bin/python -m pytest
```

Pending or skipped tests are reported separately and are not counted as passes.

Attach concise command results and acceptance boundaries to the PR; do not attach private runtime artifacts. Main supplies any necessary private live validation outside GitHub.
