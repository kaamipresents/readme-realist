# Phase 2 — Make it Frictionless

**Status:** Code complete (2 items blocked or out of repo)
**Date:** 2026-08-26
**Branch:** `chore/phase-1-shareable`

---

## The goal in one line

Drop the setup cost from about 20 minutes to about 2, so people actually try it.

---

## The problem this solves

Before this phase, using ReadMe Realist meant: register a GitHub App, generate a private key, run a server, and keep a public tunnel open to it. Most people would give up halfway.

There was a much easier route sitting unused: **GitHub will run the tool for you.** A user pastes one small file into their project, adds their own AI key, and it works. Nothing to host, and it costs the project nothing — each user pays for their own AI usage.

---

## What was covered

* **Added a "token mode" to the settings.**
  The app previously demanded GitHub App credentials — an app ID, a webhook secret, a private key — from every deployment, even ones that had no use for them. There is now a switch, `GITHUB_AUTH_MODE`, that says which set of credentials is actually required. The server path is completely unchanged; the new path just asks for a plain token instead.

* **Added a simple token login (`StaticTokenAuth`).**
  A GitHub Actions runner already hands you a working token, so there is nothing to sign or mint. This slots into the exact same place the App login used, which meant the GitHub client needed no changes at all.

* **Taught the client to look up a pull request.**
  The server learns a PR's details from the webhook message GitHub sends it. The command-line version has no such message, so it now asks GitHub directly instead.

* **Built the command-line tool (`python -m app.cli`).**
  Runs the identical review pipeline — same parser, same AI evaluation, same verdict. Options include `--dry-run` (report but write nothing), `--fail-on-drift` (treat stale docs as a build failure), plus overrides for which docs to check and which AI provider to use. This also closes the last open item from the old Milestone 7.

* **Built the GitHub Action (`action.yml`).**
  Deliberately a *composite* action rather than a Docker one. A Docker action rebuilds its image on every single run in every user's repository — roughly 60–90 seconds of waiting per pull request, forever. The composite version skips that entirely.

* **Added run summaries.**
  When it runs inside GitHub Actions, the result is written to the workflow summary page, so you can see the verdict without digging through logs.

* **Added a copy-paste workflow file.**
  `examples/readme-realist.yml`, with every option explained in comments.

* **Documented the new path everywhere.**
  README now leads with the Action. GUIDE now opens with a plain comparison of the two options and tells most readers to skip straight to the easy one.

* **Added 32 tests.**
  Covering argument handling, rebuilding PR details from the API, every exit code, and — importantly — proving that `--dry-run` writes nothing to the pull request. Total suite is now 330.

---

## What is blocked or out of scope

* **Publishing to the GitHub Marketplace is blocked.**
  It requires the repository to be public and to have a `v1` release tag. The repo is still private by choice from Phase 1, so this cannot proceed yet. Until then, the workflow snippet in the README points at an address that does not resolve for anyone else.

* **The website button was not changed.**
  The site at readme2.kamipresents.com lives in a different codebase that is not in this repository, so the change has to be made there. The snippet it should use is in `examples/readme-realist.yml`.

---

## Checks that passed

| Check | Result |
| :--- | :--- |
| Test suite | 330 / 330 passing (was 298) |
| `ruff check` | Clean |
| `ruff format --check` | Clean |
| `mypy app/` | Clean |
| `action.yml` valid | Yes |
| Example workflow valid | Yes |
| CLI runs (`--version`, `--help`, error path) | Yes |

---

## One thing worth knowing

The settings file reads a local `.env` when one is present. That is intentional and handy for running the tool on your own machine, but it meant the first draft of the new tests were quietly reading real local credentials. The tests now run in an isolated empty folder, which is exactly the situation a GitHub runner is in.

---

## What comes next

**Phase 3 — Hosted.** Put the server on a permanent address so the zero-config App version works for other people too, and finish the three live checks left over from Milestone 6.

**Immediately unblocking:** making the repository public completes Phase 1's last item *and* unblocks the Marketplace listing, which is the only thing standing between this code and other people actually using it.
