# ReadMe Realist

[![CI](https://github.com/kaamipresents/readme-realist/actions/workflows/ci.yml/badge.svg)](https://github.com/kaamipresents/readme-realist/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

An automated gatekeeper that stops documentation drift before it merges.

ReadMe Realist watches your pull requests. When a diff changes something a
reader of your README would need to know — an environment variable, an install
step, a CLI flag, an endpoint, a dependency — it checks the repository's
documentation against that change and reports back on the PR.

```
[ GitHub PR ] → [ Webhook server ] → [ Code Delta Parser ] → [ LLM ] → [ PR comment + Check Run ]
                        or
                  [ GitHub Action ] ↗
```

---

## Quick start — as a GitHub Action

The fastest way to use this. No server, no webhook, no GitHub App, no tunnel.
Add one workflow file and one secret.

```yaml
# .github/workflows/readme-realist.yml
name: Documentation drift

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write
  checks: write

jobs:
  readme-realist:
    runs-on: ubuntu-latest
    steps:
      - uses: kaamipresents/readme-realist@v2
        with:
          gemini-api-key: ${{ secrets.GEMINI_API_KEY }}
```

Then add `GEMINI_API_KEY` under **Settings → Secrets and variables → Actions**
([get a key](https://aistudio.google.com/apikey); the free tier is enough to
try it). That's the whole setup — a copy-paste version with every option
commented lives in [`examples/readme-realist.yml`](examples/readme-realist.yml).

The runner's built-in `GITHUB_TOKEN` covers GitHub access, and each repository
uses its own model key, so there is nothing to host and nothing to pay for
beyond your own inference.

### Action inputs

| Input | Default | Notes |
| --- | --- | --- |
| `gemini-api-key` | — | Required when `llm-provider` is `gemini` |
| `anthropic-api-key` | — | Required when `llm-provider` is `anthropic` |
| `llm-provider` | `gemini` | `gemini` or `anthropic` |
| `docs-globs` | `README.md,docs/**/*.md` | What the diff is checked against |
| `fail-on-drift` | `false` | `true` makes stale docs fail the job |
| `dry-run` | `false` | Print the verdict, post nothing |
| `github-token` | `${{ github.token }}` | Needs `pull-requests: write` + `checks: write` |
| `pull-request-number` | the triggering PR | Only needed outside `pull_request` events |
| `python-version` | `3.12` | |

### Or from the command line

The same engine, same pipeline, same verdict — useful for trying the tool
against a real PR before wiring anything up:

```bash
GITHUB_TOKEN=... GEMINI_API_KEY=... python -m app.cli review owner/repo 42 --dry-run
```

`--dry-run` writes nothing to the pull request. Drop it to publish the comment
and Check Run. Exit codes: `0` clean or skipped, `1` drift found (only with
`--fail-on-drift`), `2` the review could not complete.

---

## What it does

| Trigger | `pull_request.opened`, `pull_request.synchronize` |
| --- | --- |
| **Reads** | The PR's unified diff (`Accept: application/vnd.github.v3.diff`) and the raw markdown on the head ref (`README.md` + `docs/**/*.md` by default) |
| **Decides** | The model returns a strict JSON verdict: `UP_TO_DATE` or `NEEDS_UPDATE`, with a reason and a ready-to-paste markdown snippet. Gemini by default; Anthropic via one env var |
| **Reports** | `UP_TO_DATE` → a green Check Run. `NEEDS_UPDATE` → a PR comment with the suggested edit, plus a Check Run (neutral by default, so it informs without blocking merge) |

The comment is **upserted**, not appended: push ten times and you get one
comment reflecting the latest state, not ten stale ones. When a previously
flagged PR comes back clean, that comment is rewritten to say so.

---

## Architecture

```
app/
├── main.py                 FastAPI factory, lifespan, dependency wiring
├── cli.py                  `python -m app.cli` — the CLI / GitHub Action path
├── config.py               pydantic-settings; fails fast on a malformed key
├── logging_config.py       JSON logs + secret redaction filter
├── worker.py               Background execution, bounded + per-PR deduplicated
├── routes/
│   ├── webhooks.py         POST /webhooks/github — verifies, parses, returns 202
│   └── health.py           GET /healthz, GET /readyz
├── security/
│   └── signatures.py       X-Hub-Signature-256, constant-time comparison
├── parsers/
│   ├── payload.py          webhook → PullRequestContext
│   └── diff.py             ◆ Code Delta Parser
├── services/
│   ├── github/
│   │   ├── auth.py         App JWT → cached installation tokens; static-token auth
│   │   ├── client.py       REST calls, retry/backoff, rate-limit aware
│   │   └── feedback.py     ◆ Feedback Orchestrator
│   ├── llm/
│   │   ├── prompts.py      The prompt blueprint, verbatim
│   │   ├── schema.py       Strict JSON schema + validated verdict model
│   │   ├── evaluator.py    ◆ Semantic Verification Module (Anthropic)
│   │   ├── gemini.py       ◆ Semantic Verification Module (Gemini)
│   │   └── factory.py      Provider selection
│   └── orchestrator.py     The pipeline
└── models/domain.py        Typed domain values
```

### The Code Delta Parser

Two passes over the raw diff.

**Noise filtering.** Every changed line is normalised — whitespace runs
collapsed, trailing commas and semicolons stripped. If a file's added lines
normalise to the same multiset as its removed lines, the change is
reformatting and the file is dropped. Lock files, `node_modules`, `vendor/`,
minified bundles, and source maps are dropped on sight: they're derived
artefacts, and the manifest they derive from carries the meaning.

Normalisation is deliberately conservative. `foo bar` and `foobar` stay
different, and so do `TIMEOUT = 30` and `TIMEOUT = 60` — collapsing runs of
whitespace is not the same as deleting it.

**Signal isolation.** What survives is scanned for the changes that actually
invalidate docs:

| Signal | Detected from |
| --- | --- |
| Environment variables | `os.environ[...]`, `os.getenv(...)`, `process.env.X`, `System.getenv(...)`, Dockerfile `ENV`/`ARG`, `KEY=` in `.env.example` |
| Dependencies | Any change to `requirements.txt`, `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`, `Gemfile`, `pom.xml`, … |
| Install & run steps | Dockerfile `RUN`/`CMD`/`ENTRYPOINT`/`WORKDIR`, Makefile targets, `npm` scripts, compose files |
| CLI flags | `add_argument("--x")`, `@click.option`, commander `.option()`, Go `flag.*Var` |
| Endpoints | FastAPI/Flask decorators, Express/Gin routers, Django `urlpatterns` |
| Ports | `EXPOSE`, `port=`, `--port` |

Signals are passed to the model as **hints, explicitly not as a ceiling** — the
regexes have blind spots, and a missed drift is the failure that matters.

### When the LLM is skipped

Calling a frontier model on a whitespace PR is pure cost, so three cases
resolve locally with a green check and no API call:

- the diff has no files;
- every change normalises to whitespace or formatting (`SKIP_LLM_ON_NOISE_ONLY`);
- only documentation files changed — there's no code change that could have
  made them stale.

Anything with real code in it goes to the model **even when the signal scan
came up empty**. Under-calling here means shipping stale docs, which is exactly
what this tool exists to prevent.

### Swapping the model backend

`LLM_PROVIDER=gemini` (default) or `LLM_PROVIDER=anthropic`. Only the selected
provider's API key is required — startup fails fast if it's missing, and never
asks a Gemini-only deployment to invent an Anthropic key.

Both backends implement the same `SupportsDriftEvaluation` protocol: identical
prompt blueprint, identical verdict schema, identical failure semantics. The
pipeline cannot tell them apart, so switching is a config change, not a code
change.

### Prompt caching

**Anthropic only** — Gemini has no per-block cache breakpoint and applies
implicit caching to a repeated prefix automatically. The block ordering below is
used on both paths regardless, so implicit caching has the best chance of
hitting, and `cache_read_input_tokens` is logged either way.

The prompt is laid out so the stable part comes first:

```
system  : role instruction          ┐ cached prefix
user #1 : [EXISTING DOCUMENTATION]  ┘ ← cache breakpoint
user #2 : [INCOMING CODE DIFF] + [TASK]   ← volatile, never cached
```

Documentation rarely changes between pushes to the same PR, so re-reviews
replay that prefix at roughly a tenth of the input cost. `cache_read_input_tokens`
is logged on every evaluation — if it's zero across repeated reviews of one PR,
something upstream is invalidating the prefix.

### Structured output

The verdict schema is sent to the provider so generation is constrained to it,
rather than parsed hopefully afterwards — `output_config.format` on Anthropic,
`response_json_schema` + `response_mime_type` on Gemini. Gemini's dialect is an
OpenAPI 3.0 subset with no `additionalProperties`, so its variant is *derived*
from the canonical schema rather than hand-maintained, and a test asserts the
two cannot drift apart:

```json
{
  "status": "UP_TO_DATE" | "NEEDS_UPDATE",
  "reason": "…",
  "suggested_edit": "…"
}
```

The canonical schema is hand-written to use only the keywords structured
outputs accepts (no `$defs`, no length constraints, `additionalProperties:
false`, every field required). Pydantic then validates and reconciles the result — a verdict of
`UP_TO_DATE` carrying a suggested edit has the edit cleared rather than being
rejected, because a usable verdict beats a failed review.

---

## Setup — as a GitHub App

The Action above needs none of this. Follow this path when you want the App
shape instead: zero config in each repository, one installation covering many
repos, and reviews that run without consuming the repo's Actions minutes.

### 1. Create the GitHub App

Settings → Developer settings → GitHub Apps → **New GitHub App**.

**Permissions** (Repository):

| Permission | Access | Why |
| --- | --- | --- |
| Contents | Read-only | Fetch README/docs from the head ref |
| Pull requests | Read & write | Fetch the diff, post the comment |
| Checks | Read & write | Publish the Check Run |
| Metadata | Read-only | Mandatory |

**Subscribe to events:** `Pull request` only.

**Webhook URL:** `https://<your-host>/webhooks/github`
**Webhook secret:** generate one and keep it —

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Then **Generate a private key** and save the `.pem`. Install the App on the
repositories you want gated.

### 2. Configure

```bash
cp .env.example .env
```

Fill in `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY_PATH`
(or `GITHUB_PRIVATE_KEY`), and `GEMINI_API_KEY` (from
[aistudio.google.com/apikey](https://aistudio.google.com/apikey)). Every value is validated at
startup — a malformed PEM or a short webhook secret stops the process rather
than failing on the first webhook.

### 3. Run

```bash
python -m venv .venv
```

Activate it — `source .venv/bin/activate` on macOS/Linux, `.venv\Scripts\activate`
on Windows — then:

```bash
pip install -e ".[dev]"
```

```bash
uvicorn app.main:app --reload --port 8000
```

Expose it for local testing:

GitHub has to reach your machine, so put a tunnel in front of it and set that
URL as the **App's** webhook URL (Settings → your App → Webhook URL):

```bash
cloudflared tunnel --url http://localhost:8000
```

`ngrok http 8000` or a [smee.io](https://smee.io) channel work equally well.

> **Not `gh webhook forward`.** That creates a *repository* webhook, whose
> payload carries no `installation` object — ReadMe Realist authenticates as a
> GitHub App and rejects those deliveries with a 400. Deliveries have to come
> from the App itself.

Once the App redelivers, **Advanced → Recent Deliveries** in the App settings
shows the exact payload and our response, and has a **Redeliver** button — the
fastest way to re-run a review without pushing again.

### 4. Test

```bash
pytest
```

The suite runs entirely offline — GitHub is faked at the transport layer
(respx) and at the client layer, and both model backends are recording stubs.
No credentials required.

---

## Configuration reference

| Variable | Default | Notes |
| --- | --- | --- |
| `GITHUB_AUTH_MODE` | `app` | `app` for the webhook server, `token` for the CLI / Action. Decides which credentials below are required |
| `GITHUB_APP_ID` | — | Required in `app` mode |
| `GITHUB_WEBHOOK_SECRET` | — | Required in `app` mode, ≥16 chars |
| `GITHUB_PRIVATE_KEY` / `GITHUB_PRIVATE_KEY_PATH` | — | Exactly one required in `app` mode |
| `GITHUB_TOKEN` | — | Required in `token` mode; supplied automatically inside Actions |
| `GITHUB_API_BASE_URL` | `https://api.github.com` | Set for GitHub Enterprise |
| `LLM_PROVIDER` | `gemini` | `gemini` or `anthropic` |
| `GEMINI_API_KEY` | — | Required when provider is `gemini` |
| `GEMINI_MODEL` | `gemini-2.5-flash` | |
| `GEMINI_MAX_OUTPUT_TOKENS` | `8000` | |
| `ANTHROPIC_API_KEY` | — | Required when provider is `anthropic` |
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `ANTHROPIC_MAX_TOKENS` | `8000` | Caps thinking **and** response together |
| `ANTHROPIC_EFFORT` | `high` | `low`/`medium` cut cost noticeably |
| `DOCS_GLOBS` | `README.md,docs/**/*.md` | `**` matches across separators |
| `DOCS_MAX_FILES` / `DOCS_MAX_TOTAL_CHARS` / `DOCS_MAX_FILE_CHARS` | `40` / `200000` / `60000` | Truncation is README-first |
| `DIFF_MAX_CHARS` | `120000` | |
| `SKIP_LLM_ON_NOISE_ONLY` | `true` | |
| `DRIFT_CHECK_CONCLUSION` | `neutral` | `failure` makes it a hard merge gate |
| `POST_PR_COMMENT` / `PUBLISH_CHECK_RUN` | `true` / `true` | |
| `SKIP_DRAFT_PULL_REQUESTS` | `true` | |
| `MAX_CONCURRENT_REVIEWS` | `4` | |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | |

### Turning it into a hard gate

Set `DRIFT_CHECK_CONCLUSION=failure`, then add the check to your branch
protection rules. Worth tuning accuracy on `neutral` for a few weeks first — a
false positive on a blocking check stops somebody's merge.

---

## Operational notes

- **Webhooks return 202 immediately.** Reviews run in a background worker with
  bounded concurrency. Pushing three times in a row cancels the first two
  reviews — they were about a `head_sha` nobody is looking at any more.
- **Our own failures never fail your build.** If the evaluation errors, the
  Check Run concludes `neutral` with the error in its summary. A bug here must
  not block someone else's merge.
- **Secrets are redacted from logs** by a filter on the root handler, so a
  stray exception repr can't leak the private key.
- **Installation tokens are cached** per installation and refreshed five
  minutes before expiry, with a per-installation lock so concurrent PRs on one
  repo mint one token rather than racing.

---

## Deployment

A `Dockerfile` is included:

```bash
docker build -t readme-realist .
docker run -p 8000:8000 --env-file .env readme-realist
```

The container runs as a non-root user, starts through `python -m app.main`
(structured-JSON logging, `proxy_headers` on, honours `$PORT`), and ships a
`HEALTHCHECK` against `/healthz`. Point the GitHub App's webhook URL at
`https://<host>/webhooks/github` wherever you host it — the app is a plain
stateless ASGI service, so anything that runs a container works.

**[DEPLOY.md](DEPLOY.md)** has copy-paste runbooks for Fly.io, Cloud Run, and
Railway, plus the steps to repoint the GitHub App and take it public.

## License

MIT — see [LICENSE](LICENSE).
