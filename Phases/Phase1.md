# Phase 1 — Make it Shareable

**Status:** Done (1 item deliberately held)
**Date:** 2026-08-26
**Pull Request:** [#5](https://github.com/kaamipresents/readme-realist/pull/5)
**Branch:** `chore/phase-1-shareable`

---

## The goal in one line

Make it so a stranger is both **allowed** to take the code and **able** to see that it works.

---

## What was covered

* **Added a LICENSE file (MIT).**
  The README said "MIT", but the actual permission file was never created. Without it the code was legally "all rights reserved" — meaning nobody was allowed to use it, no matter who could see it. This was the single biggest blocker and it was invisible.

* **Added automatic testing (CI).**
  Created `.github/workflows/ci.yml`. Every push and pull request now runs the full 298-test suite on Python 3.11, 3.12 and 3.13, plus code style (`ruff`), formatting, and type checks (`mypy`). The tests are fully offline, so CI needs no API keys or secrets to run.

* **Removed dead configuration (`metrics_port`).**
  A setting existed in `app/config.py` that no code read and no document mentioned. It was left over from a live drift-detection test back in commit `ebe47a0`. It was, ironically, exactly the kind of undocumented change this whole project exists to catch.

* **Added badges to the README.**
  CI status, licence, and Python version — so a visitor can see the project is tested and usable without cloning anything.

* **Audited the entire git history for secrets.**
  Checked every commit on every branch for private keys, `.env` files, and live API keys (Gemini, Anthropic, GitHub tokens). **Nothing found.** GitGuardian, already installed on the repo, independently agreed. This means going public is safe whenever wanted.

---

## What was deliberately NOT done

* **The repo is still private.**
  This was held back on purpose — it is the one step in Phase 1 that is hard to undo, since once code is public it can be copied and kept even if made private again. Everything needed to flip it safely is now in place; it just needs a decision.

---

## Checks that passed

| Check | Result |
| :--- | :--- |
| Test suite | 298 / 298 passing |
| `ruff check` | Clean |
| `ruff format --check` | Clean |
| `mypy app/` | Clean |
| CI workflow file | Valid |
| Git history secret scan | Clean |

---

## Known loose end

GitHub has not registered the new CI workflow yet, so no run has appeared. This is normal — workflow files usually aren't picked up until they land on the main branch. It should start running once PR #5 is merged.

---

## What comes next

**Phase 2 — Frictionless.** Build a command-line entry point and ship the tool as a GitHub Action, so anyone can use it by pasting a short config file instead of setting up a server. This is the phase that actually makes the tool usable by other people.
