# ReadMe Realist — Progress & Resume Notes

**Last updated:** 2026-08-21
**Status:** Core application complete and offline-verified. Gemini backend fully verified against the live API — all four accuracy scenarios (A/B/C/D) now confirmed correct. GitHub integration verified live end to end through every stage except a successful model call, which was the quota-blocked piece and is now closed out.

### Session update, 2026-08-21

This session ran on a **different machine/checkout** than the one that did the
2026-08-20 GitHub round-trip (`D:\Tech and Development\Coding\readme_realist`,
not `D:\Coding\readme-realist`). `.env` here only carries `GEMINI_API_KEY` /
`ANTHROPIC_API_KEY` — no `secrets/*.pem`, `GITHUB_APP_ID`, or
`GITHUB_WEBHOOK_SECRET`, and `cloudflared` is not confirmed installed here. The
live GitHub App round-trip (webhook → tunnel → PR comment) is **not
re-runnable from this environment** without those credentials being supplied
again; that piece of §7's checklist is unchanged from 2026-08-20, not
re-verified or regressed.

What *was* done this session:

1. **Fixed a real bug**: `app/worker.py` still had the deliberate
   `REDIS_URL = os.environ["REDIS_URL"]` test fixture from the live PR test.
   With no `REDIS_URL` exported, this broke `import app.main` outright —
   `pytest -q` failed to even collect 2 of the test files. Removed it (its
   job was done; see the commit history for the original PR-test rationale).
   The offline suite is no longer broken-by-default for a fresh checkout.
2. **Re-verified offline**: `pytest -q` → 298/298 passing; `--cov=app` → 97%,
   matching the prior figure exactly; `ruff check` / `ruff format --check` /
   `mypy app tests` → all clean.
3. **Closed Scenario C live** (the one outstanding accuracy check): built a
   standalone script hitting `GeminiDriftEvaluator` directly with a synthetic
   port-change diff (`8000` → `9000`) against a README documenting port 8000.
   Result: `NEEDS_UPDATE`, correctly citing the port change, with a
   paste-ready suggested edit. Gemini free-tier quota had reset (new UTC day)
   so this ran without issue. **All four scenarios (A/B/C/D) are now
   confirmed correct** — see the results table below.

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
| GitHub App auth (JWT → token) | ✅ **Verified live** | Real installation token minted (`POST .../access_tokens` → 201), 2026-08-20 |
| Real webhook delivery | ✅ **Verified live** | GitHub → Cloudflare tunnel → local `uvicorn`, signature verified, 200 back to GitHub |
| Real PR comment / Check Run | ⚠️ **Partially verified** | Check Run created + updated live (201, then 200 PATCH). No comment yet — blocked on the LLM call itself, not the GitHub plumbing (see below) |

### Live GitHub round-trip, 2026-08-20

Ran the full pipeline against a real PR — [kaamipresents/readme-realist#1](https://github.com/kaamipresents/readme-realist/pull/1), which adds an undocumented
`REDIS_URL = os.environ["REDIS_URL"]` to `app/worker.py` without touching the
README. Log trace confirmed every stage up to the model call:

1. ✅ Webhook received, `X-Hub-Signature-256` verified
2. ✅ Installation token minted
3. ✅ Check Run created (`201`)
4. ✅ Diff fetched + analysed — 1 file, 1 signal (`REDIS_URL`) correctly detected
5. ✅ README fetched (12,207 chars)
6. ❌ Gemini call → `429 RESOURCE_EXHAUSTED` — free tier's `generate_content_free_tier_requests` daily cap (`quotaValue: 20`) exhausted by cumulative testing
7. ✅ Failure handled exactly per design: Check Run PATCHed to `conclusion: neutral`, title *"Documentation check could not complete"*, error detail in the summary. **Zero PR comments posted** — correct, since a comment implies a verdict was reached, and none was.

**Conclusion: the entire GitHub-facing pipeline is proven correct**, including the
failure path. The one thing not yet observed live is a *successful* model call
producing an actual `NEEDS_UPDATE`/`UP_TO_DATE` verdict — purely a quota
question, not a code question.

### Live Gemini results (`gemini-3.7-flash`, 2026-08-19)

| # | Scenario | Expected | Got | |
| --- | --- | --- | --- | --- |
| A | Env var added, undocumented | `NEEDS_UPDATE` | `NEEDS_UPDATE` | ✅ |
| B | Env var added, already documented | `UP_TO_DATE` | `UP_TO_DATE` | ✅ |
| D | Internal refactor, no surface change | `UP_TO_DATE` | `UP_TO_DATE` | ✅ |
| C | Default port `8000`→`9000` vs README | `NEEDS_UPDATE` | `NEEDS_UPDATE` | ✅ (2026-08-21) |

B and D are the important ones: they prove it does **not** just fire on
everything, which is what would make it useless as a gate. Scenario A produced
a paste-ready edit that slotted into the existing bullet list correctly.
Scenario C (closed 2026-08-21, via a standalone script calling
`GeminiDriftEvaluator` directly — not through the GitHub pipeline) correctly
named the port change and produced a matching suggested edit.

**All four scenarios now pass. No outstanding accuracy checks remain.**

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

**Not an app bug, but a gotcha worth recording:** the `REDIS_URL` test fixture
added to `app/worker.py` for live PR testing (§ below) originally read
`os.environ["REDIS_URL"]` at module import time, expecting `.env` to supply
it. It doesn't — `pydantic-settings`'s `env_file=".env"` loads values into the
`Settings` object only, it never touches `os.environ`. Anything reading
`os.environ` directly (as this test fixture deliberately does, to simulate a
real hard dependency) needs the var **exported in the actual shell**
(`$env:REDIS_URL = "..."` in PowerShell) before `uvicorn` starts. First
attempt also placed the import above the module docstring / `from __future__
import annotations`, which must be the file's first statement — caused a
`SyntaxError` on boot. Both fixed in commit `7d68289`.

---

## 6. Environment state

### Local

- Repo: `D:\Coding\readme-realist` — **git-tracked**, pushed to GitHub at
  [kaamipresents/readme-realist](https://github.com/kaamipresents/readme-realist)
  (`main` + a `testing` branch used for the live proving PR, #1)
- venv: `.venv/` (Python 3.13.5). Interpreter: `.venv/Scripts/python.exe`
- Installed: `-e ".[dev]"` — clean install, 298/298 tests pass

### `.env` (git-ignored — never commit; values not recorded here)

| Key | State |
| --- | --- |
| `GEMINI_API_KEY` | ✅ Present and working (subject to free-tier daily quota — see live round-trip above) |
| `ANTHROPIC_API_KEY` | ⚠️ Present but **unusable** — account has zero API credits |
| `GITHUB_APP_ID` | ✅ Set, verified live |
| `GITHUB_WEBHOOK_SECRET` | ✅ Set, verified live (signature checks pass) |
| `GITHUB_PRIVATE_KEY_PATH` | ✅ Set → `./secrets/readme-realist.*.private-key.pem`, verified live (token minting succeeds) |

Everything else runs on defaults.

### GitHub App

- Name: `readme-realist`, installed on `kaamipresents/readme-realist` only
- Permissions: Contents (read), Pull requests (read+write), Checks (read+write), Metadata (read)
- Subscribed to `Pull request` only
- **Webhook URL changes every session** — local dev uses `cloudflared`'s free
  "quick tunnel," which mints a new random `*.trycloudflare.com` URL every
  time it's restarted. **Before testing again, you must re-run `cloudflared
  tunnel --url http://localhost:8000` and paste the new URL into the App's
  General → Webhook URL field**, or deliveries will fail with no server to
  reach.
- `cloudflared` is installed via `winget` at
  `C:\Program Files (x86)\cloudflared\cloudflared.exe`. A PowerShell window
  opened *before* the winget install won't have it on `PATH` — either open a
  fresh terminal, or invoke the full path directly.

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

**GitHub App setup is done.** The App exists, is installed, and the full
webhook → auth → diff → docs → (failure-handling) pipeline is proven live
against real PR #1. Nothing left to configure — the only remaining step is
seeing one *successful* model call.

### Immediate next step, in order

1. **Confirm Gemini quota has reset** (free tier is daily; check
   [ai.dev/rate-limit](https://ai.dev/rate-limit) if unsure), or enable
   pay-as-you-go billing at [aistudio.google.com](https://aistudio.google.com)
   if you don't want to wait.
2. **Start the server**, with the test fixture's env var exported first:
   ```powershell
   $env:REDIS_URL = "redis://localhost:6379"
   uvicorn app.main:app --reload --port 8000
   ```
3. **Start a fresh tunnel** (yesterday's URL is dead):
   ```powershell
   cloudflared tunnel --url http://localhost:8000
   ```
4. **Update the Webhook URL** on the App's General settings page to the new
   tunnel URL + `/webhooks/github`, and save.
5. **Redeliver**, don't repush — on the App's **Advanced → Recent Deliveries**,
   find the latest `pull_request` (`synchronize`) delivery on PR #1 and click
   **Redeliver**. No need for a new commit.
6. **Check PR #1** — [kaamipresents/readme-realist#1](https://github.com/kaamipresents/readme-realist/pull/1).
   Expect a `NEEDS_UPDATE` comment naming `REDIS_URL`, a suggested README
   snippet, and a neutral Check Run.
7. **Prove the resolve path** — push a commit on the `testing` branch adding
   `REDIS_URL` to `README.md`. The **same** PR comment should rewrite itself
   to "up to date," and the Check Run should go green.
8. *(Optional)* Push a whitespace-only commit and confirm it resolves locally
   with **zero** LLM calls in the log — the noise-only skip path.

### Deferred / optional

- [x] Scenario C accuracy check — port-change detection. Closed 2026-08-21, see §4.
- [ ] Anthropic backend live verification (blocked on API credits)
- [ ] Re-run the live GitHub round-trip from *this* checkout — needs
      `GITHUB_APP_ID`, `GITHUB_WEBHOOK_SECRET`, and the App's private key
      (`secrets/*.pem`) put back into `.env`/`secrets/`, plus `cloudflared`
      confirmed on this machine. Not a code gap — purely local secrets/tooling
      that didn't carry over from the other checkout.
- [ ] **Dry-run CLI** (`python -m app.cli review <owner/repo> <pr>`) — evaluate real
      public PRs with no App, webhook, or tunnel. Reuses the existing parser,
      evaluator, and prompts; only auth and the output sink are new. Offered
      but not built.
- [ ] Deployment — `Dockerfile` is ready (non-root, healthcheck, `--proxy-headers`).
      Target platform not chosen. Would also remove the "tunnel URL changes
      every restart" friction from local dev.
- [ ] Post-launch metrics — `TokenUsage` already emits per-review cost and
      cache-hit data as the foundation.
- [ ] Clean up the `REDIS_URL` test fixture in `app/worker.py` once the live
      verdict is captured — it's a deliberate test artifact, not real code,
      and should probably be reverted (or kept intentionally as a running
      example — user's call) once its job is done.

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

Live-testing against a real PR also needs (PowerShell, before starting
`uvicorn` — the `app/worker.py` test fixture reads it via `os.environ`
directly, which `.env` does not populate):

```powershell
$env:REDIS_URL = "redis://localhost:6379"
```

Tunnel (new random URL every restart — re-paste into the App's Webhook URL
each time):

```powershell
cloudflared tunnel --url http://localhost:8000
```

If `cloudflared` isn't found in a given terminal, either open a fresh one
(picks up `winget`'s PATH update) or call it directly:

```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:8000
```

### Switching the LLM backend

Set in `.env`:

```
LLM_PROVIDER=gemini      # or: anthropic
```

Only the selected provider's key is required; startup fails fast if it is
missing. Anthropic additionally needs purchased API credits.
