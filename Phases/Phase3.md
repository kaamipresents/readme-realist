# Phase 3 — Hosted

**Status:** Repo work done; deploy + registration steps need an owner
**Date:** 2026-08-27
**Branch:** `main`

---

## The goal in one line

Put the server on a permanent address so the zero-config App version works for
other people, not just on a laptop behind a tunnel that dies when the laptop
sleeps.

---

## The problem this solves

The engine has only ever run locally, reachable through an ephemeral
`cloudflared` tunnel whose URL changes on every restart. Nobody else can
install the GitHub App against a URL that keeps moving and is offline half the
day. Phase 2 gave people the Action, which needs no host at all — this phase
makes the *other* path real too, for repositories that would rather install
once and forget.

---

## What was covered in the repo

* **Verified the production image end to end.**
  `docker build` is clean. The container boots, `/healthz` and `/readyz`
  return 200, and Docker's own `HEALTHCHECK` reaches `healthy`.

* **Fixed a bug that stopped the container booting at all.**
  The `Dockerfile` launched the server with `uvicorn --log-config /dev/null`.
  The intent was to hand all logging to the app's own structured-JSON setup.
  Current uvicorn no longer treats that as "no config" — it passes the path to
  `logging.config.fileConfig`, which rejects an empty file, and the process
  exits 1 before serving a single request. The `HEALTHCHECK` in the file could
  never have passed. The container now starts through `python -m app.main`,
  whose entrypoint already calls `uvicorn.run(..., log_config=None)`; that
  entrypoint also gained `proxy_headers=True` (client IP and scheme survive the
  platform load balancer) and reads `$PORT`, so the identical image runs on
  Cloud Run, Fly.io, or Railway with nothing changed.

* **Committed the deployment assets.**
  * `fly.toml` — a Fly.io config that keeps one machine warm (a suspended
    machine can miss GitHub's webhook delivery window) and health-checks
    `/healthz`.
  * `.dockerignore` — keeps the build context small and guarantees no local
    secret or cache is ever sent to the daemon.
  * `DEPLOY.md` — the runbook: the full env-var table, a local verify step,
    copy-paste deploys for Fly.io / Cloud Run / Railway, the steps to repoint
    the GitHub App webhook off the tunnel, the three Milestone 6 live checks to
    close once the URL is stable, and the make-public flip.

* **Updated `README.md`** to describe the new launch command and link
  `DEPLOY.md`.

---

## What is blocked or out of scope

Every remaining Phase 3 item needs credentials and consoles this repository
does not contain:

* **The actual deploy.** Needs a cloud account (Fly.io / Cloud Run / Railway)
  and the real App private key + webhook secret + Gemini key loaded as
  platform secrets. `DEPLOY.md` §3 is the exact sequence.
* **Repointing the GitHub App.** A GitHub UI change on the App registration —
  webhook URL from the tunnel to the permanent host. `DEPLOY.md` §4.
* **Closing the three Milestone 6 live checks.** They need the permanent URL
  receiving real webhooks. `DEPLOY.md` §5.
* **Making the App public.** One toggle in the App's Advanced settings.
  `DEPLOY.md` §6.
* **The website install link.** Different codebase; not in this repo.

---

## Checks that passed

| Check | Result |
| :--- | :--- |
| `docker build` | Clean |
| Container boot | Starts, stays up |
| `GET /healthz` | 200 `{"status":"ok",...}` |
| `GET /readyz` | 200 `{"status":"ready",...}` |
| Docker `HEALTHCHECK` | `healthy` |
| Test suite | 330 / 330 passing |
| `ruff check` / `ruff format --check` | Clean |
| `mypy app/` | Clean |

---

## One thing worth knowing

The `--log-config /dev/null` trick is a common way to silence uvicorn's default
logging, and it used to work. It breaks silently on a uvicorn upgrade with an
error that names `/dev/null` rather than the flag, so it reads like an
environment problem, not a config one. Launching through a small Python
entrypoint that passes `log_config=None` is the stable form — there is no
sentinel file for a library to misinterpret later.

---

## What comes next

**The owner runs `DEPLOY.md`.** Deploy, repoint the App, close the three live
checks, flip to public, update the website link. After that the App path is
real for third parties and Phase 3 is done.

**Phase 4 — Product (metering, multi-tenancy).** Deliberately deferred until
Phases 1–3 show real demand.
