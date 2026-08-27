# Hosting ReadMe Realist

This is the **App path**: one permanently hosted server that many repositories
install once and forget. If you only want drift checks on your own repo, the
[GitHub Action](README.md#option-a--github-action-recommended) needs no hosting
at all — use that instead.

The server is a plain stateless ASGI container. It holds no database and no
disk state, so any runtime that runs a container and gives it a stable HTTPS
URL works. Three are written up below; all use the repo's `Dockerfile`
unchanged.

---

## 1. What the container needs

| Variable | Required | Notes |
| :--- | :--- | :--- |
| `GITHUB_APP_ID` | yes | From `https://github.com/settings/apps/<app>` |
| `GITHUB_WEBHOOK_SECRET` | yes | The secret set on the App; ≥ 16 chars |
| `GITHUB_PRIVATE_KEY` | yes\* | The App PEM, newlines as literal `\n` |
| `GITHUB_PRIVATE_KEY_PATH` | yes\* | Alternative to the above: a mounted file |
| `GEMINI_API_KEY` | yes\*\* | When `LLM_PROVIDER=gemini` (the default) |
| `ANTHROPIC_API_KEY` | yes\*\* | When `LLM_PROVIDER=anthropic` |
| `LOG_FORMAT` | no | `json` (default) or `text` |
| `PORT` | no | Overrides the listen port; default `8000` |
| `FORWARDED_ALLOW_IPS` | no | Default `*` — trust the platform proxy |

\* Supply **exactly one** of `GITHUB_PRIVATE_KEY` / `GITHUB_PRIVATE_KEY_PATH`.
\*\* Only the selected provider's key. Full list in [`.env.example`](.env.example).

The process validates every value at startup and refuses to boot on a
malformed one, so a bad secret fails the deploy immediately rather than the
first webhook at 3am.

---

## 2. Verify the image locally first

```bash
docker build -t readme-realist .

docker run --rm -p 8000:8000 \
  -e GITHUB_APP_ID=000000 \
  -e GITHUB_WEBHOOK_SECRET=0123456789abcdef0123456789abcdef \
  -e GITHUB_PRIVATE_KEY_PATH=/srv/secrets/key.pem \
  -e GEMINI_API_KEY=dummy \
  -v "$PWD/secrets/github-app.private-key.pem:/srv/secrets/key.pem:ro" \
  readme-realist
```

In another shell:

```bash
curl -fsS http://127.0.0.1:8000/healthz   # {"status":"ok","version":"..."}
curl -fsS http://127.0.0.1:8000/readyz    # {"status":"ready",...}
```

`docker inspect --format '{{.State.Health.Status}}' <container>` should read
`healthy` within ~15s — the image ships a `HEALTHCHECK` against `/healthz`.

---

## 3a. Fly.io

A [`fly.toml`](fly.toml) is committed. It keeps one machine warm (a suspended
machine can miss GitHub's webhook delivery window) and health-checks
`/healthz`.

```bash
fly launch --no-deploy --copy-config --name readme-realist

fly secrets set \
  GITHUB_APP_ID=000000 \
  GITHUB_WEBHOOK_SECRET=... \
  GITHUB_PRIVATE_KEY="$(cat secrets/github-app.private-key.pem)" \
  GEMINI_API_KEY=...

fly deploy
```

URL: `https://readme-realist.fly.dev` → webhook `.../webhooks/github`.
`fly secrets set` accepts a real multi-line value, so the PEM needs no `\n`
escaping here.

## 3b. Google Cloud Run

```bash
gcloud run deploy readme-realist \
  --source . \
  --region us-central1 \
  --port 8000 \
  --allow-unauthenticated \
  --min-instances 1 \
  --set-env-vars LLM_PROVIDER=gemini,LOG_FORMAT=json \
  --set-secrets \
GITHUB_WEBHOOK_SECRET=readme-realist-webhook:latest,\
GITHUB_PRIVATE_KEY=readme-realist-key:latest,\
GEMINI_API_KEY=readme-realist-gemini:latest \
  --set-env-vars GITHUB_APP_ID=000000
```

Store the secrets first with `gcloud secrets create ... --data-file=-`. Cloud
Run injects `PORT`; the image already honours it. `--min-instances 1` avoids a
cold start eating the webhook timeout.

## 3c. Railway

`railway up` from the repo root picks up the `Dockerfile`. Add the variables
from the table in the project's **Variables** tab (paste the PEM with literal
`\n`), and set the service to **always on**. Railway assigns
`https://<service>.up.railway.app`.

---

## 4. Point the GitHub App at the new URL

Once the container answers `/healthz` on its permanent URL:

1. **App settings → Webhook → URL:** `https://<permanent-host>/webhooks/github`
2. Save. GitHub sends a `ping`; the server logs `event=ping` and returns 202.
3. **App settings → Advanced → Recent Deliveries:** redeliver the `ping` and
   confirm a green 202. This retires the ephemeral `cloudflared` tunnel for
   good.

---

## 5. Finish the Milestone 6 live checks

With a stable URL these three can finally be closed (see
[`PROGRESS.md`](PROGRESS.md) §Milestone 6):

- [ ] Open a PR that adds an undocumented env var → expect a `NEEDS_UPDATE`
      comment with a concrete README patch.
- [ ] Push a commit to that PR that documents the var → expect the **same**
      comment rewritten to up-to-date and the Check Run flipping green.
- [ ] Push a whitespace-only commit → expect the noise-skip path: a 202, no
      LLM call, no comment churn (visible in logs as the skip reason).

---

## 6. Go public

- **App settings → Advanced → Make public.** Until this flip only your own
  org can install it.
- Publish the install URL (`https://github.com/apps/<app-slug>/installations/new`)
  on the website, replacing the `#setup` anchor.
