# Project Money

Private, source-only engineering baseline for CIO Market Lab / Project Money.

This repository contains application code, portable unit/regression tests, plugin code, strategy code, and attributed frontend source. It does **not** contain broker credentials, client accounts, runtime databases, captured private research, model-session logs, local service configuration, deployment artifacts, or the original Git history.

## Local setup

Python 3.11+ (handoff validated with Python 3.13); Node.js and npm.

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

Run the API locally with a new disposable runtime root. Do not point an experimental clone at production accounts or databases.

```sh
.venv/bin/python -m uvicorn cio_market_lab.api.app:app --host 127.0.0.1 --port 8787
```

The frontend is served at `/static/index.html` after its build. API and browser tests are not broker/live acceptance. Desktop integration and authenticated research intake require separately configured local host services; credentials are never included here.

## Acceptance boundary

The source-only snapshot was independently installed and tested before publication. See `docs/ACCEPTANCE.md` for exact counts, exclusions, and unresolved original live acceptance. This baseline is not a claim of complete deployment or real-money readiness.

## Contribution workflow

Main owns design and creates decided implementation issues. The client hands a curated batch to web ChatGPT. ChatGPT implements on a branch and opens a PR. Main independently reviews, tests and deploys. See `CONTRIBUTING.md` and the issue/PR templates. Creating this repository does not connect a ChatGPT GitHub connector or automatically merge/deploy PRs.

## Third-party source

Frontend source under `vendor/shioaji-pro-app` retains its upstream repository reference, commit reference and license. Generated frontend `dist` and dependency directories are rebuilt locally and excluded from Git.
