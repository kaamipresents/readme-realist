# ReadMe Realist — Progress & Resume Notes

**Last updated:** 2026-08-19
**Status:** Core application complete and offline-verified. Gemini backend verified against the live API. GitHub integration **not yet tested live**.

This file is the handoff record. Read it top to bottom to know exactly what
exists, what has actually been proven, and where to pick up.

---

## 1. What this is

An automated gatekeeper that stops documentation drift before it merges. It
watches `pull_request.opened` / `synchronize`, extracts the *structural* signal
from the diff (env vars, install steps, CLI flags, endpoints, deps — not
whitespace), checks it against the repo's markdown via an LLM, and reports back
on the PR.

```
[ GitHub PR ] → [ Webhook server ] → [ Code Delta Parser ] → [ LLM ] → [ PR comment + Check Run ]
```

Full architecture, config reference, and setup instructions live in
[`README.md`](README.md). This file only tracks *state and next steps*.

---

## 2. Decisions already made

Do not re-litigate these without a reason; the code and tests assume them.

| Decision | Choice | Why |
| --- | --- | --- |
| Language / framework | **Python + FastAPI** | User choice |
| Delivery model | **GitHub App + webhook server** | Works across many repos from one install |
| Feedback on drift | **PR comment + neutral Check Run** | Advisory; does not block merge while accuracy is being tuned |
| Docs scope | **`README.md` + `docs/**/*.md`** | Config-driven via `DOCS_GLOBS` |
| LLM backend | **Gemini (default), Anthropic optional** | Switched at user request; no Anthropic credits available |
| Default Gemini model | **`gemini-3.7-flash`** | `gemini-2.5-pro` 404s for this account (see §6) |

### Deliberate design positions

- **The LLM is skipped only on provably-safe cases** — empty diff,
  whitespace-only after normalisation, docs-only changes. Code changes with
  *no matched signals still go to the model*. The regexes have blind spots and
  a missed drift is the failure that matters.
- **Static signals are hints, never a ceiling.** The prompt says so explicitly.
- **Our own failures never fail your build.** Any internal error concludes the
  Check Run as `neutral`, never `failure`.
- **Comments are upserted, not appended.** One comment per PR, rewritten on
  each push; resolved drift rewrites it to say so.

---

## 3. What is built

```
app/
├── main.py                 FastAPI factory, lifespan, wiring
├── config.py               pydantic-settings; fail-fast validation
├── logging_config.py       JSON logs + secret redaction
├── worker.py               Background execution, bounded + per-PR dedup
├── routes/{webhooks,health}.py
├── security/signatures.py  X-Hub-Signature-256, constant-time
├── parsers/{payload,diff}.py       ◆ Code Delta Parser
├── services/
│   ├── github/{auth,client,feedback}.py   ◆ Feedback Orchestrator
│   ├── llm/
│   │   ├── prompts.py      Prompt blueprint, verbatim
│   │   ├── schema.py       Canonical JSON schema (+ derived Gemini variant)
│   │   ├── evaluator.py    ◆ Anthropic backend + SupportsDriftEvaluation
│   │   ├── gemini.py       ◆ Gemini backend
│   │   └── factory.py      Provider selection
│   └── orchestrator.py     The pipeline
└── models/domain.py
```

Both backends implement `SupportsDriftEvaluation`. The pipeline cannot tell
them apart; switching is `LLM_PROVIDER`, not a code change.

**Test suite:** 298 tests, 97% coverage, fully offline (GitHub faked via respx
and at the client layer; both LLM backends are recording stubs).

---

## 4. Verification status — read this carefully

The distinction between "tested" and "proven live" matters here.

| Area | Status | Notes |
| --- | --- | --- |
| Offline test suite | ✅ **298 passing, 97% cov** | `ruff` + `mypy --strict` on `app/` also clean |
| Config loads from real env | ✅ Verified | Production-shaped boot smoke test |
| App boots, serves, verifies signatures | ✅ Verified | Via `TestClient` against the real ASGI app |
| Code Delta Parser on real diffs | ✅ Verified | Correctly isolated `REDIS_URL` from a real diff |
| **Gemini backend, live API** | ✅ **3/3 executed scenarios correct** | See table below |
| Anthropic backend, live API | ❌ **Never run** | Account has no API credits (§6) |
| GitHub App auth (JWT → token) | ❌ **Never run live** | Unit-tested against mocks only |
| Real webhook delivery | ❌ **Never run** | No App created yet |
| Real PR comment / Check Run | ❌ **Never run** | No App created yet |

### Live Gemini results (`gemini-3.7-flash`, 2026-08-19)

| # | Scenario | Expected | Got | |
| --- | --- | --- | --- | --- |
| A | Env var added, undocumented | `NEEDS_UPDATE` | `NEEDS_UPDATE` | ✅ |
| B | Env var added, already documented | `UP_TO_DATE` | `UP_TO_DATE` | ✅ |
| D | Internal refactor, no surface change | `UP_TO_DATE` | `UP_TO_DATE` | ✅ |
| C | Default port `8000`→`9000` vs README | `NEEDS_UPDATE` | — | ⏳ blocked on quota |

B and D are the important ones: they prove it does **not** just fire on
everything, which is what would make it useless as a gate. Scenario A produced
a paste-ready edit that slotted into the existing bullet list correctly.

**Scenario C is the one outstanding accuracy check.**

---

## 5. Bugs found and fixed

All four were found by testing, not review. Recorded so they aren't
reintroduced.

| # | Bug | Fix |
| --- | --- | --- |
| 1 | `ReviewPipeline.review()` claimed never to raise, but a non-`GitHubApiError` escaping the *failure handler* propagated out of a background task, where it would vanish silently | Publishing paths made genuinely best-effort; `review()` wraps its own failure reporting |
| 2 | **Production-only:** pydantic-settings JSON-decodes list fields from env *before* validators run, so the documented `DOCS_GLOBS=README.md,docs/**/*.md` raised `JSONDecodeError` at container start. Unit tests missed it because they pass kwargs (init source), not env vars | `NoDecode` annotation + a test class that loads config the way production does |
| 3 | Default `gemini-2.5-pro` is listed by the models API but **404s**: *"no longer available to new users"* | Default → `gemini-3.7-flash` |
| 4 | Gemini path had **no retry**; the Anthropic client retries out of the box, so one transient `503` would fail a real review | `retry_options` on 408/429/5xx, tunable via `GEMINI_MAX_RETRIES` |

Also: automatic function calling was on by default (we send no tools), emitting
an SDK warning per call — now explicitly disabled.

---

## 6. Environment state

### Local

- Repo: `D:\Tech and Development\Coding\readme_realist` — **not a git repo yet** (no `git init`)
- venv: `.venv/` (Python 3.11.9). Interpreter: `.venv/Scripts/python.exe`
- Installed: `-e ".[dev]"` plus `google-genai` 2.18.1, `anthropic` 0.122.0

### `.env` (git-ignored — never commit; values not recorded here)

| Key | State |
| --- | --- |
| `GEMINI_API_KEY` | ✅ Present and working |
| `ANTHROPIC_API_KEY` | ⚠️ Present but **unusable** — account has zero API credits |

Everything else runs on defaults. `GITHUB_*` values are **not yet set** — the
app cannot boot for real until they are.

> **Note:** a Claude.ai Pro/Max subscription does *not* cover API usage. The
> Anthropic API needs credits purchased separately on the Console. This is why
> the Anthropic backend is unverified.

### Known constraint: Gemini free-tier quota

The free tier is the bottleneck, not the code:

- `gemini-3.1-pro-preview` → immediate `429`; **no quota on this plan**
- `gemini-3.7-flash` → exhausted daily quota after roughly 5 calls
- Sustained load also returns `503 UNAVAILABLE`

When quota runs out the app degrades correctly — a neutral Check Run saying the
review could not complete, never a blocked merge — but no verdict is produced.
Enabling pay-as-you-go billing in Google AI Studio removes this; Flash pricing
makes realistic usage cents per month.

---

## 7. Where to continue

### Immediate decision point

The user was asked to choose between:

- **(a)** Move on to GitHub App setup and wire a real PR end to end ← *recommended*
- **(b)** Enable Gemini billing first, then re-run scenario C and a wider accuracy set

Recommendation was **(a)**: the model's reasoning is proven; the untested piece
is now the GitHub plumbing, not the intelligence.

### Path (a) — GitHub App setup, in order

1. **Create the GitHub App** — Settings → Developer settings → GitHub Apps → New.
   - Permissions: Contents `read`, Pull requests `read+write`, Checks `read+write`, Metadata `read`
   - Subscribe to **Pull request** only
2. **Generate + download the private key** (`.pem`), save outside git or in `./secrets/`
3. **Set a webhook secret:** `python -c "import secrets; print(secrets.token_hex(32))"`
4. **Install the App** on a throwaway test repo
5. **Fill `.env`:** `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_PRIVATE_KEY_PATH`
6. **Start a tunnel** and set it as the App's Webhook URL:
   `cloudflared tunnel --url http://localhost:8000` (or ngrok / smee.io)
   > **Not `gh webhook forward`** — it creates a *repository* webhook, whose
   > payload has no `installation` object, and the app rejects those with a 400.
   > Deliveries must come from the App itself.
7. **Run:** `uvicorn app.main:app --reload --port 8000`
8. **Open the proving PR** — in the test repo, ensure `README.md` documents some
   env vars, then open a PR adding one *without* touching the README:
   ```python
   REDIS_URL = os.environ["REDIS_URL"]
   ```
   - Expect: `NEEDS_UPDATE` comment naming `REDIS_URL` + suggested snippet + neutral Check Run
   - Then push a commit adding it to the README → the **same** comment should
     rewrite itself to "up to date" and the check should go green
   - A whitespace-only PR should go green with **zero** LLM spend

**Faster iteration:** the App's **Advanced → Recent Deliveries** page shows the
exact payload and our response, and has a **Redeliver** button — re-runs a
review without pushing.

### Deferred / optional

- [ ] Scenario C accuracy check (blocked on Gemini quota)
- [ ] Anthropic backend live verification (blocked on API credits)
- [ ] `git init` + first commit — repo is currently untracked
- [ ] **Dry-run CLI** (`python -m app.cli review <owner/repo> <pr>`) — evaluate real
      public PRs with no App, webhook, or tunnel. Reuses the existing parser,
      evaluator, and prompts; only auth and the output sink are new. Offered
      but not built.
- [ ] Deployment — `Dockerfile` is ready (non-root, healthcheck, `--proxy-headers`).
      Target platform not chosen.
- [ ] Post-launch metrics — `TokenUsage` already emits per-review cost and
      cache-hit data as the foundation.

---

## 8. Useful commands

All from the repo root. On Windows use `.venv/Scripts/python.exe`; on
macOS/Linux `.venv/bin/python`.

```bash
.venv/Scripts/python.exe -m pytest -q
```

```bash
.venv/Scripts/python.exe -m pytest -q --cov=app --cov-report=term-missing
```

```bash
.venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m ruff format --check .
```

```bash
.venv/Scripts/python.exe -m mypy app tests
```

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
```

### Switching the LLM backend

Set in `.env`:

```
LLM_PROVIDER=gemini      # or: anthropic
```

Only the selected provider's key is required; startup fails fast if it is
missing. Anthropic additionally needs purchased API credits.
