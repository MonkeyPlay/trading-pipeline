# Security Policy

## What this project is

The NQ Opening Forecast Pipeline is **single-user research tooling that runs on your own
machine**. It has no authentication layer, no multi-tenancy, and no notion of untrusted
users. Every default binds to `127.0.0.1`, and nothing in the codebase places orders or
moves money.

Treat the whole pipeline as trusted-local software. Anyone who can reach the dashboard
port or the database port can read and modify everything in the store. That is by design,
not a vulnerability — but it means **exposing any part of it to a network makes you
responsible for putting authentication and a firewall in front of it.**

## Supported versions

Only the current `main` branch is supported. There are no tagged releases, backports, or
long-lived maintenance branches; fixes land on `main`.

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:

> **[Security → Report a vulnerability](https://github.com/MonkeyPlay/trading-pipeline/security/advisories/new)**

Please do not open a public issue for anything exploitable. Include:

- what an attacker can do, and what access they need to start,
- the affected file or component,
- reproduction steps or a proof of concept,
- the commit you tested against (`git rev-parse HEAD`).

This is a personal project maintained by one person on a best-effort basis. Expect an
acknowledgement within about a week, and no guaranteed fix timeline. You are welcome to
disclose publicly after 90 days, or sooner once a fix is on `main`.

## In scope

Findings that break the trusted-local model, or that harm a user who followed the README:

- remote code execution, SQL injection, or path traversal reachable from collected data,
  IB API responses, LLM responses, or dashboard input,
- a default or documented configuration that exposes the dashboard or database beyond
  `localhost` without saying so,
- secrets (API keys, `DATABASE_URL` passwords) leaking into logs, the database, LLM
  prompts, backups, or committed files,
- a dependency vulnerability that is actually reachable from this code,
- data-destroying behaviour that bypasses the existing guards — for example
  `populate_mock_data.py --reset` touching a store that holds non-`MOCK` bars, or
  `migrate_from_sqlite` overwriting a populated target.

## Out of scope

- The absence of authentication on the dashboard or the database. There is none by design;
  see [Hardening](#hardening-if-you-run-it-beyond-your-laptop).
- Anything that requires you to deliberately expose a port (`POSTGRES_BIND=0.0.0.0`,
  `DASHBOARD_HOST=0.0.0.0`) without adding the access control the README tells you to add.
- The default `trading:trading` database credentials on a `localhost`-only bind.
- Forecast quality, model accuracy, or trading losses. This is research tooling, not
  trading infrastructure, and nothing it produces is investment advice.
- Findings that need root or an existing shell on the host — at that point the attacker
  already has everything the pipeline has.
- Denial of service against your own single-user local instance.

## Secrets and data handling

| Where secrets live | How it is handled |
|---|---|
| `.env` | Git-ignored, never committed. Holds `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` and any real `DATABASE_URL` password. |
| `config.py` | Reads environment variables only — no key is ever hardcoded. Defaults are non-secret localhost values. |
| `python config.py` | Prints a redacted DSN via `describe_dsn()`, not the password. |
| `data/backups/*.dump` | Git-ignored, **unencrypted** `pg_dump` output with 30-day retention. Encrypt the directory or the volume if the host is not trusted. |
| `logs/` | Git-ignored. Do not paste raw logs into a public issue without reading them first. |

If you ever commit a key by accident, treat it as compromised: revoke and reissue it at the
provider, then rewrite history. Rotating alone is not enough once it has been pushed.

**Third-party data flow.** With `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` set, the forecaster
sends the pre-open feature snapshot and analogue sessions to that provider's API. Those are
derived market statistics, not personal data, but it is an outbound network call to a third
party. Leave both keys unset and `ForecastClient` uses the offline deterministic baseline
instead, and the pipeline makes no LLM calls at all.

## Hardening if you run it beyond your laptop

The defaults are safe on a single machine. Every step away from that needs work:

1. **Set a real `POSTGRES_PASSWORD`** in `.env`, and put the same password into
   `DATABASE_URL` and `DEV_DATABASE_URL`. The default `trading` is only acceptable behind a
   `127.0.0.1` bind.
2. **Keep the binds local unless you must change them.** `POSTGRES_BIND` and
   `DASHBOARD_HOST` both default to `127.0.0.1`. If the pipeline and the database run on
   different hosts, firewall port 5432 to just the pipeline host and require TLS
   (`?sslmode=require` in the DSN).
3. **Never expose the dashboard directly.** It has no login and no CSRF protection, and it
   does its pandas work synchronously on the server, so any reachable client can also stall
   it. Put it behind an authenticating reverse proxy, or reach it over SSH or a VPN.
4. **Restrict the IB socket.** IB Gateway/TWS on `4001`/`4002` is an unauthenticated local
   API into a brokerage session. Keep it bound to localhost. Use the paper ports
   (`4002`/`7497`) for anything experimental.
5. **Protect the backups and the database volume** with filesystem or disk encryption; both
   contain your full collected history in the clear.

## A note on scripts you schedule

`scripts/run_pipeline.sh` and `scripts/backup_db.sh` are meant for cron and resolve the
project directory from their own location. Whoever can write to the project directory can
therefore run code as your cron user. Keep the checkout owned by, and writable only by,
that user.
