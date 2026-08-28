# Step-by-Step Guide to Running ReadMe Realist

This guide walks you through configuring, running, and testing **ReadMe Realist** locally, with Docker, and in production as a GitHub App.

---

## Start here: do you actually need any of this?

There are two ways to run ReadMe Realist, and most people want the first one.

| | **GitHub Action** | **GitHub App** |
| --- | --- | --- |
| Setup | One workflow file + one secret | Register an App, host a server, expose a webhook |
| Time | ~2 minutes | ~20 minutes, plus ongoing hosting |
| Runs on | GitHub's runners | A server you operate |
| Covers | One repository per workflow file | Every repo in an installation |
| Costs you | Nothing beyond your own model usage | Hosting |

**If you just want the tool working on a repository, use the Action.** Copy
[`examples/readme-realist.yml`](examples/readme-realist.yml) to
`.github/workflows/readme-realist.yml`, add a `GEMINI_API_KEY` repository
secret, and you are done — skip the rest of this guide.

Everything below is the App path: worth it when you want to cover many
repositories from one installation, or want reviews that do not consume each
repo's Actions minutes.

### Trying it first, without installing anything

The CLI runs the identical pipeline against a real PR and, with `--dry-run`,
writes nothing back:

```bash
GITHUB_TOKEN=... GEMINI_API_KEY=... python -m app.cli review owner/repo 42 --dry-run
```

---

## Table of Contents
1. [Prerequisites](#1-prerequisites)
2. [Step 1: Create & Configure a GitHub App](#step-1-create--configure-a-github-app)
3. [Step 2: Set Up Local Environment & Dependencies](#step-2-set-up-local-environment--dependencies)
4. [Step 3: Configure Environment Variables (`.env`)](#step-3-configure-environment-variables-env)
5. [Step 4: Run the Application Locally](#step-4-run-the-application-locally)
6. [Step 5: Test Webhooks Locally (Tunneling with Smee / ngrok)](#step-5-test-webhooks-locally-tunneling-with-smee--ngrok)
7. [Step 6: Run Tests & Linters](#step-6-run-tests--linters)
8. [Step 7: Run with Docker](#step-7-run-with-docker)

---

## 1. Prerequisites

Before starting, ensure you have:
* **Python 3.11+** installed (or [uv](https://docs.astral.sh/uv/) / [Docker](https://www.docker.com/))
* An API key for an LLM provider:
  * **Google Gemini API Key** (from [Google AI Studio](https://aistudio.google.com/apikey)) OR
  * **Anthropic API Key** (from [Anthropic Console](https://console.anthropic.com/))
* A GitHub account with permissions to create GitHub Apps.

---

## Step 1: Create & Configure a GitHub App

ReadMe Realist interacts with GitHub repositories via a GitHub App.

1. Navigate to **GitHub Settings** > **Developer settings** > **GitHub Apps** > **[New GitHub App](https://github.com/settings/apps/new)**.
2. Fill in the basic application details:
   * **GitHub App name**: `ReadMe-Realist-<your-org-or-name>`
   * **Homepage URL**: `https://github.com/<your-username>/readme-realist`
3. Configure the **Webhook**:
   * **Webhook URL**: Your server's endpoint: `https://<your-domain>/webhooks/github` *(for local testing, see [Step 5](#step-5-test-webhooks-locally-tunneling-with-smee--ngrok))*.
   * **Webhook secret**: Generate a secure secret:
     ```bash
     python -c "import secrets; print(secrets.token_hex(32))"
     ```
     *(Save this secret for `.env`)*.
4. Set **Repository Permissions**:
   * **Checks**: `Read and write` (to post check runs)
   * **Contents**: `Read-only` (to fetch documentation files & PR diffs)
   * **Pull requests**: `Read and write` (to read metadata and post PR feedback comments)
   * **Issues**: `Read and write` (to list and upsert issue comments on PRs)
5. Subscribe to **Events**:
   * Check **Pull request**.
6. Create the app and note down the **App ID**.
7. Scroll down to **Private keys** and click **Generate a private key**. Download the `.pem` file and save it in a secure folder (e.g. `./secrets/github-app.private-key.pem`).
8. Click **Install App** in the left sidebar and install it on your target repository.

---

## Step 2: Set Up Local Environment & Dependencies

### Option A: Using `venv` and `pip`
```bash
# 1. Clone or navigate to the repository
cd readme-realist

# 2. Create and activate a virtual environment
python -m venv .venv
# On Windows (PowerShell):
.venv\Scripts\Activate.ps1
# On Linux/macOS:
source .venv/bin/activate

# 3. Upgrade pip and install package with dev dependencies
pip install --upgrade pip
pip install -e ".[dev]"
```

### Option B: Using `uv`
```bash
uv venv
# Activate virtual environment (.venv)
uv pip install -e ".[dev]"
```

---

## Step 3: Configure Environment Variables (`.env`)

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

```dotenv
# --- GitHub App Settings ---
GITHUB_APP_ID=123456
GITHUB_WEBHOOK_SECRET=your_generated_webhook_secret_here
GITHUB_PRIVATE_KEY_PATH=./secrets/github-app.private-key.pem

# --- LLM Provider Settings (Gemini by default) ---
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash

# (Optional: Anthropic)
# LLM_PROVIDER=anthropic
# ANTHROPIC_API_KEY=sk-ant-...
# ANTHROPIC_MODEL=claude-3-7-sonnet-20250219

# --- Documentation Scope & Pipeline ---
DOCS_GLOBS=README.md,docs/**/*.md
DRIFT_CHECK_CONCLUSION=neutral
POST_PR_COMMENT=true
PUBLISH_CHECK_RUN=true
SKIP_DRAFT_PULL_REQUESTS=true
```

---

## Step 4: Run the Application Locally

Start the FastAPI application with Uvicorn:

```bash
uvicorn app.main:app --reload --port 8000
```

### Verify Application Health:
Check that the server is up and healthy:
```bash
curl http://127.0.0.1:8000/healthz
```
*Expected Response:*
```json
{"status":"healthy","version":"0.1.0","drift_conclusion":"neutral"}
```

---

## Step 5: Test Webhooks Locally (Tunneling with Smee / ngrok)

GitHub needs a public URL to deliver webhook events to your local machine.

### Option A: Using Smee.io (Recommended for GitHub Apps)
1. Go to [smee.io](https://smee.io/) and click **Start a new channel**. Copy the channel URL.
2. In your GitHub App settings, set **Webhook URL** to your Smee URL.
3. Install and run the Smee client:
   ```bash
   npx smee-client --url https://smee.io/<your-channel-id> --target http://127.0.0.1:8000/webhooks/github
   ```

### Option B: Using ngrok
```bash
ngrok http 8000
```
Update your GitHub App's **Webhook URL** to `https://<your-ngrok-subdomain>.ngrok-free.app/webhooks/github`.

---

## Step 6: Run Tests & Linters

Run the automated test suite to ensure everything is configured properly:

```bash
# Run pytest with coverage
pytest

# Run code style & lint checks
ruff check app/ tests/

# Run static type checks
mypy app/
```

---

## Step 7: Run with Docker

### Build the Docker Image:
```bash
docker build -t readme-realist:latest .
```

### Run the Container:
Pass your `.env` file and mount your GitHub App private key:

```bash
docker run -d \
  --name readme-realist \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/secrets:/srv/secrets:ro \
  readme-realist:latest
```

Check container logs:
```bash
docker logs -f readme-realist
```

Check container health:
```bash
curl http://127.0.0.1:8000/healthz
```

---

## 🚀 Summary of the End-to-End Flow
1. Developer creates or updates a Pull Request.
2. GitHub sends a `pull_request` event to `/webhooks/github`.
3. ReadMe Realist verifies HMAC signature, downloads the PR diff, and scans for doc changes and code changes.
4. If substantive code changes exist, it queries the LLM against your repository's docs.
5. It posts a suggested doc fix comment to the PR and updates the GitHub Check Run.
