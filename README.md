# pr-agent — self-hosted AI PR reviewer on Unraid (LLM backend + GitHub App)

A self-hosted fork of [`the-pr-agent/pr-agent`](https://github.com/the-pr-agent/pr-agent) that reviews pull requests on your GitHub repos and posts comments back as a **distinct bot identity** (`<your-app>[bot]`), powered by **any OpenAI-compatible LLM** (Ollama Cloud, Neuralwatt, OpenRouter, local Ollama, …), running in Docker on Unraid.

This README is the single source of truth: what it is, how to deploy it, and every setting you can configure.

---

## What you get

- **Distinct bot identity** — reviews post as `<your-app-name>[bot]`, not `github-actions[bot]`. One GitHub App install covers every repo you add it to.
- **No GitHub Actions runner spin-up, no GitHub-side review timeout** — the webhook receiver acks GitHub in milliseconds and runs the LLM call on a background task. A slow model taking 2-3 minutes per PR is a non-issue.
- **Pluggable LLM backend** — pick any current model via one env var. Defaults to Ollama Cloud; switch to Neuralwatt or any OpenAI-compatible endpoint by changing the model prefix (`ollama/...` → `openai/...`) and the matching secrets section. No code changes, no patches.
- **Status page** — `/status` (HTML, auto-refreshing) and `/status.json` showing bot identity, configured model, in-flight reviews, and a live log tail.
- **Unraid container template** — install via the Unraid "Add Container" GUI with a labeled form for every setting (LLM picker, tool toggles, review tuning). No YAML editing required.
- **Tools** — auto-runs `/describe` (AI PR description), `/review` (inline code review), `/improve` (committable code suggestions) on every PR; re-runs `/review` on new commits. Also available on-demand via PR comments: `/review`, `/improve`, `/describe`, `/ask <question>`, `/update_changelog`, `/add_docs`, `/analyze`.


---

## Choosing an LLM provider

pr-agent routes LLM calls by the **prefix on `CONFIG__MODEL`**, not by which secrets section is filled in. The prefix determines which litellm provider handles the call, and each provider reads its own `[section]` in `.secrets.toml`:

| `CONFIG__MODEL` prefix | litellm provider | Reads from `.secrets.toml` | Typical base URL |
|---|---|---|---|
| `ollama/<model>` | Ollama | `[ollama] api_base` + `api_key` | `https://ollama.com` (Cloud) or `http://host.docker.internal:11434` (local) |
| `openai/<model>` | OpenAI-compatible | `[openai] api_base` + `api_key` | `https://api.openai.com/v1`, `https://api.neuralwatt.com/v1`, OpenRouter, Together, Groq, local vLLM/LM Studio … |

**Switching provider = two steps**: (1) change the prefix on `CONFIG__MODEL` (and `CONFIG__FALLBACK_MODELS`); (2) fill in the matching `[section]` in `.secrets.toml`. No rebuild — it's an env-var + secrets swap.

> ⚠️ **Fill in ONLY ONE provider section.** If both `[ollama]` and `[openai]` set an `api_base`, **Ollama silently wins**: pr-agent's litellm handler sets `litellm.api_base` from `[openai]` first, then `[ollama]` overwrites it a few lines later (`pr_agent/algo/ai_handlers/litellm_ai_handler.py` L150 vs L170). A `openai/<model>` call would then hit the Ollama host and 404. Pick one provider, comment out the other section.

### Built-in providers

- **Ollama Cloud** (default) — <https://ollama.com/settings/keys>. Pay-per-token, no GPU needed. Models at <https://ollama.com/search?c=cloud>.
- **Neuralwatt** — <https://portal.neuralwatt.com>. OpenAI-compatible (`https://api.neuralwatt.com/v1`), energy-transparent pricing, serves the same open models (GLM, Kimi, Qwen, Gemma). Model list at <https://api.neuralwatt.com/v1/models> (no auth required). Use the `openai/` prefix.
- **Any other OpenAI-compatible endpoint** — OpenAI itself, OpenRouter, Together, DeepSeek, Groq, local vLLM/LM Studio, etc. Set `[openai] api_base` to the provider's `/v1` URL, use the `openai/` prefix on the model.
- **Local Ollama daemon** — set `[ollama] api_base = "http://host.docker.internal:11434"` and leave `api_key` empty. The container reaches the host's Ollama via Docker's host bridge.

---
## How it works

```
GitHub PR event ──webhook──▶  your tunnel (Cloudflare/Tailscale)  ──▶  Docker container (Unraid)
                                         POST /api/v1/github_webhooks
                                              │  verify HMAC signature
                                              │  ack 200 immediately
                                              ▼
                                    background task per tool:
                                      /describe, /review, /improve
                                              │
                                              ▼
                                    your LLM (Ollama Cloud / Neuralwatt / local)
                                              │
                                              ▼
                                    post review as <your-app>[bot]
```

---

## Files in this fork (added on top of upstream pr-agent)

| File | Purpose |
|---|---|
| `README.md` | This file — complete deployment + settings reference |
| `secrets.example.toml` | Template for `.secrets.toml` (Ollama key + GitHub App credentials) |
| `templates/pr-agent.xml` | Unraid container template — GUI form for every setting |
| `deploy/docker-compose.yml` | Docker Compose file (env-var config, secrets mount, healthcheck) |
| `deploy/Dockerfile.status` | Thin child image that layers the `/status` page on the base |
| `pr_agent/status_page.py` | Status page router + loguru in-memory sink + HTML/JSON endpoints |
| `pr_agent/status_app.py` | Entrypoint shim: imports upstream app, mounts status router, **enforces webhook-secret guard at boot** |

No upstream files are modified — all overrides are via env vars + a child Docker image. This keeps pr-agent updates painless: `git pull` + rebuild.

---

## Deployment guide

### 1. Create the GitHub App

Go to <https://github.com/settings/apps> → **New GitHub App** (or under an org).

| Field | Value |
|---|---|
| GitHub App name | `pr-agent` (or whatever you want the bot to be called) |
| Homepage URL | anything (your repo URL is fine) |
| Webhook → Active | ✅ checked |
| Webhook URL | `https://<YOUR-TUNNEL-HOST>/api/v1/github_webhooks` (set up in step 4) |
| Webhook secret | a random hex string — generate with `python -c "import secrets; print(secrets.token_hex(20))"` and **save it** |
| Repository permissions | **Pull requests: Read & write**, **Issue comment: Read & write**, **Contents: Read-only**, **Metadata: Read-only** |
| Subscribe to events | **Pull request**, **Issue comment** |

Create the app, then:
1. On the app's settings page, click **Generate a private key** → a `.pem` file downloads.
2. Note the **App ID** (numeric, near the top of the settings page).

> ⚠️ **The webhook secret is mandatory.** The container refuses to start if it's missing or still set to the placeholder — this prevents the webhook endpoint from silently accepting unauthenticated requests (see Security below).

### 2. Build the Docker images on Unraid

On the Unraid host (web terminal or SSH), from this repo's root:

```bash
# 1. Base image (the github_app target from the upstream Dockerfile):
docker build --target github_app -t local/pr-agent:github_app -f docker/Dockerfile .

# 2. Status-page layer (thin child image, no upstream files edited):
docker build -t local/pr-agent:github_app-status -f deploy/Dockerfile.status .
```

The first builds the `github_app` target (Python 3.12 slim + pr-agent + gunicorn/uvicorn). The second layers the `/status` web page on top. The compose file and Unraid template default to `local/pr-agent:github_app-status`. If you don't want the status page, swap the image tag to `local/pr-agent:github_app`.

### 3. Create the secrets file on Unraid

```bash
mkdir -p /mnt/user/appdata/pr-agent
```

Create `/mnt/user/appdata/pr-agent/.secrets.toml` by copying `secrets.example.toml` from this repo. Fill in **exactly one** LLM provider section plus the `[github]` section:

**Option A — Ollama Cloud (default):**

```toml
[ollama]
api_base = "https://ollama.com"
api_key = "<your Ollama Cloud key from https://ollama.com/settings/keys>"

[github]
deployment_type = "app"
app_id = <your App ID, numeric>
webhook_secret = "<the webhook secret you generated in step 1>"
private_key = """\
-----BEGIN RSA PRIVATE KEY-----
<paste the entire contents of the .pem file you downloaded>
-----END RSA PRIVATE KEY-----
"""
```

**Option B — Neuralwatt (or any OpenAI-compatible endpoint):**

```toml
[openai]
api_base = "https://api.neuralwatt.com/v1"
api_key = "<your Neuralwatt key from https://portal.neuralwatt.com>"
# For other OpenAI-compatible providers, swap api_base for their /v1 URL:
#   OpenAI:        https://api.openai.com/v1
#   OpenRouter:    https://openrouter.ai/api/v1
#   Together:      https://api.together.xyz/v1
#   Groq:          https://api.groq.com/openai/v1
#   local vLLM:    http://host.docker.internal:8000/v1

[github]
deployment_type = "app"
app_id = <your App ID, numeric>
webhook_secret = "<the webhook secret you generated in step 1>"
private_key = """\
-----BEGIN RSA PRIVATE KEY-----
<paste the entire contents of the .pem file you downloaded>
-----END RSA PRIVATE KEY-----
"""
```

Then set `CONFIG__MODEL` to a model with the **matching prefix**: `ollama/kimi-k2.7-code` for Option A, `openai/kimi-k2.7-code` for Option B. See the LLM model picker below for the full list per provider.

**Config overrides (model, tools, tuning) are NOT in this file** — they're environment variables in the compose file / Unraid template. See the Settings reference below. The `.secrets.toml` holds only credentials.

### 4. Expose the webhook endpoint

The container listens on port 3000 for `POST /api/v1/github_webhooks`. Unraid is behind your router's NAT, so pick one:

- **Cloudflare Tunnel** (recommended, free): `cloudflared tunnel` maps a public hostname to `http://<unraid-ip>:3000`. No port forwarding, no cert management.
- **Tailscale Funnel**: if you already run Tailscale on Unraid, `tailscale funnel` exposes a port publicly.
- **Port forward + reverse proxy**: forward 443 to Unraid, run Caddy/Traefik with Let's Encrypt in front of the container.

The public URL goes into the GitHub App's **Webhook URL** field (step 1). The path must be `/api/v1/github_webhooks`.

The same host also serves the status page at `/status` (HTML, auto-refreshing) and `/status.json` (machine-readable). **Guard it** — see Security below.

### 5. Start the container

#### Option A — Unraid container template (recommended)

`templates/pr-agent.xml` turns every config knob into a labeled GUI field.

1. Copy `templates/pr-agent.xml` to your Unraid host at `/boot/config/plugins/dockerMan/templates-user/pr-agent.xml` (create the `templates-user` directory if it doesn't exist).
2. In the Unraid web UI → **Docker** tab → **Add Container** → pick **pr-agent** from the template dropdown.
3. Fill in the visible fields (see Settings reference for each):
   - **LLM model** — the model that reviews your PRs. The field description lists every current Ollama Cloud model with a one-line characterization and a link to the live catalog. Default: `ollama/kimi-k2.7-code`.
   - **Fallback LLM model(s)** — tried if the primary errors.
   - **Secrets file** — path to your filled-in `.secrets.toml` (default `/mnt/user/appdata/pr-agent/.secrets.toml`).
   - **Webhook port** — `3000` (behind your tunnel).
4. Click **Advanced View** for the tuning knobs (model max tokens, AI timeout, large-patch policy, response language, auto-review triggers, which tools run, review strictness, `/improve` score threshold, gunicorn workers, log level). Every field has a description.
5. **Apply**. The container starts within ~15s.

To change the LLM later: edit the container in the Unraid Docker UI, change the **LLM model** field, apply. No YAML editing, no rebuild — it's an env-var swap.

#### Option B — Docker Compose Manager plugin

Install the **Docker Compose Manager** plugin in Unraid, then add `deploy/docker-compose.yml` as a new compose project. Same settings as the template, just in YAML. Adjust the volume path if your appdata lives elsewhere. Start it.

### 6. Verify the webhook is reachable

From outside your network:

```bash
curl -s https://<YOUR-TUNNEL-HOST>/
# → {"status":"ok"}

# Status page (in-flight reviews + recent log, in-browser):
# open https://<YOUR-TUNNEL-HOST>/status

# Or machine-readable:
curl -s https://<YOUR-TUNNEL-HOST>/status.json | python -m json.tool
```

Then in your GitHub App settings, scroll to **Recent Deliveries**. After step 7 you'll see webhook events with their HTTP responses. GitHub retries failed deliveries for up to 3 days, so a transient tunnel blip won't lose reviews.

### 7. Install the App on your repos

Back in the GitHub App settings → **Install App** → choose the repos you want reviewed. From now on, opening a PR on any of them triggers:

1. GitHub POSTs the `pull_request` event to your tunnel URL.
2. The container validates the `X-Hub-Signature-256` HMAC with your webhook secret.
3. It acks `200` immediately and queues `/describe`, `/review`, `/improve` on a background task.
4. Each tool calls your configured LLM provider (Ollama Cloud / Neuralwatt / other), posts its comment as `<your-app>[bot]`.

Comment `/review`, `/improve`, `/describe`, or `/ask <question>` on any PR to re-trigger a tool on demand.

### 8. Sanity-check commands

```bash
# Container logs (live)
docker logs -f pr-agent

# Status page (in-flight reviews + recent log, in-browser):
# open https://<YOUR-TUNNEL-HOST>/status

# Confirm the App authenticates (from inside the container)
docker exec -it pr-agent python -c "from pr_agent.config_loader import get_settings; print('app_id:', get_settings().github.app_id, 'model:', get_settings().config.model)"

# Local CLI smoke test against a real PR (uses your mounted .secrets.toml)
docker exec -it pr-agent python pr_agent/cli.py --pr_url https://github.com/<you>/<repo>/pull/<N> review
```

---

## Settings reference

All non-secret config is via **environment variables** (double-underscore separator: `CONFIG__MODEL` == `config.model`). Env vars win over the upstream defaults baked into `pr_agent/settings/configuration.toml`. Set them in the Unraid template GUI or the compose `environment:` block.

### LLM backend

| Env var | Default | Description |
|---|---|---|
| `CONFIG__MODEL` | `ollama/kimi-k2.7-code` | The model that reviews PRs. The **prefix chooses the provider**: `ollama/<model>` → Ollama Cloud (uses `[ollama]` in `.secrets.toml`); `openai/<model>` → Neuralwatt or any OpenAI-compatible endpoint (uses `[openai]`). See "Choosing an LLM provider" above and "LLM model picker" below. |
| `CONFIG__FALLBACK_MODELS` | `["ollama/deepseek-v4-flash"]` | JSON array of models tried if the primary errors or rate-limits. Use the same provider prefix as `CONFIG__MODEL` (mixing prefixes needs both secrets sections — not recommended). |
| `CONFIG__CUSTOM_MODEL_MAX_TOKENS` | `128000` | Max input tokens the model accepts. Lower if you pick a smaller-context model. Lets pr-agent's PR-compression fit large PRs in one call. |
| `CONFIG__AI_TIMEOUT` | `300` | Per-LLM-call timeout in seconds. The webhook acks GitHub in milliseconds and runs the review on a background task, so this can be generous without hitting GitHub's ~10s webhook timeout. |
| `CONFIG__MAX_MODEL_TOKENS` | `128000` | Hard cap on tokens usable by any model. Keep aligned with `CUSTOM_MODEL_MAX_TOKENS`. |
| `CONFIG__LARGE_PATCH_POLICY` | `clip` | What to do when a PR diff exceeds the token budget. `clip` = review the first portion (always produces some review); `skip` = skip the PR entirely. |
| `CONFIG__RESPONSE_LANGUAGE` | `en-US` | ISO 3166 + ISO 639 locale for review comments. `en-US`, `de-DE`, `fr-FR`, `zh-CN`, etc. Change to `de-DE` for German reviews. |
| `CONFIG__REPO_CONTEXT_FILES` | `[]` | JSON array of repo-relative files (e.g. `AGENTS.md`, `CLAUDE.md`) to inject into review prompts. `[]` = none — keeps reviews focused on the diff. |

### Auto-run tools on each PR

| Env var | Default | Description |
|---|---|---|
| `GITHUB_APP__HANDLE_PR_ACTIONS` | `["opened","reopened","ready_for_review","synchronize"]` | JSON array of `pull_request` actions that trigger auto-review. Remove `synchronize` if you don't want re-reviews on every push. |
| `GITHUB_APP__PR_COMMANDS` | `["/describe --pr_description.final_update_message=false","/review","/improve"]` | JSON array of pr-agent commands run automatically on each new PR. Remove any you don't want. Available: `/describe`, `/review`, `/improve`, `/ask`, `/update_changelog`, `/add_docs`, `/analyze`. |
| `GITHUB_APP__HANDLE_PUSH_TRIGGER` | `true` | When `true`, re-run commands on new commits to an open PR (`synchronize` action). |
| `GITHUB_APP__PUSH_COMMANDS` | `["/describe --pr_description.final_update_message=false","/review"]` | JSON array of commands run on `synchronize`. Defaults to `/describe` + `/review` (skips `/improve` to save cost on every push). |

### Review tuning

| Env var | Default | Description |
|---|---|---|
| `PR_REVIEWER__REQUIRE_TESTS_REVIEW` | `false` | When `true`, the reviewer flags missing tests for new behavior. Noisy on pure source changes, so off by default. Toggle on for repos where test coverage matters. |
| `PR_REVIEWER__REQUIRE_SECURITY_REVIEW` | `true` | Run the security sub-review (injection, auth gaps, secret leakage, etc.). Recommended on. |
| `PR_REVIEWER__NUM_MAX_FINDINGS` | `5` | Cap on findings reported per review. Lower = less noisy, higher = more thorough. |
| `PR_CODE_SUGGESTIONS__SUGGESTIONS_SCORE_THRESHOLD` | `7` | 0-10. `/improve` only surfaces suggestions scoring at least this high. `7` = only real problems. Lower (4-5) for more suggestions, raise (8-9) for stricter. |
| `PR_CODE_SUGGESTIONS__NUM_CODE_SUGGESTIONS_PER_CHUNK` | `3` | Number of code suggestions per diff chunk in `/improve`. Lower = faster, less noisy. |

### Server

| Env var | Default | Description |
|---|---|---|
| `GUNICORN_WORKERS` | `1` | Number of gunicorn worker processes. **Keep at 1** so the `/status` page's in-memory log buffer sees ALL webhook activity (each worker has its own buffer). The webhook acks in milliseconds and reviews run as background tasks, so one worker handles concurrent reviews fine for a personal bot. Raise only if you review many PRs simultaneously (and accept that `/status` will show only one worker's logs). |
| `CONFIG__LOG_LEVEL` | `INFO` | Log verbosity: `INFO` (normal) or `DEBUG` (verbose, for troubleshooting). |

### Secrets (in `.secrets.toml`, NOT env vars)

The GitHub App private key is multi-line PEM — painful in a single-line env field — so credentials live in the mounted `.secrets.toml`:

| Key | Description |
|---|---|
| `[ollama] api_base` | `https://ollama.com` for Ollama Cloud, or `http://host.docker.internal:11434` for a local Ollama daemon. **Only when using the `ollama/` prefix.** |
| `[ollama] api_key` | Your Ollama Cloud key from <https://ollama.com/settings/keys>. Leave empty for a local daemon. |
| `[openai] api_base` | The provider's `/v1` URL. `https://api.neuralwatt.com/v1` for Neuralwatt, or OpenAI / OpenRouter / Together / Groq / local vLLM. **Only when using the `openai/` prefix.** |
| `[openai] api_key` | Your key for the OpenAI-compatible provider. Neuralwatt keys come from <https://portal.neuralwatt.com>. |
| `[github] deployment_type` | `app` (this deployment mode). |
| `[github] app_id` | Numeric App ID from the GitHub App settings page. |
| `[github] webhook_secret` | The secret you generated in step 1. **Mandatory** — container refuses to start without it. |
| `[github] private_key` | The full PEM contents of the `.pem` file you downloaded. |

You can alternatively pass these as env vars (`OLLAMA__API_KEY`, `OPENAI__API_KEY`, `OPENAI__API_BASE`, `GITHUB__APP_ID`, `GITHUB__WEBHOOK_SECRET`, `GITHUB__PRIVATE_KEY`) — env wins over the file.

---

## LLM model picker

The `CONFIG__MODEL` field's description in the Unraid template lists every current model with a one-line characterization, per provider. The **prefix chooses the provider**; the model id after the slash must match what that provider's `/v1/models` (or catalog page) returns.

### Ollama Cloud (`ollama/` prefix)

Verified catalog (2026-07-20):

| Model | Characterization |
|---|---|
| `ollama/kimi-k2.7-code` | **Coding-focused agentic, lower thinking-token cost (DEFAULT — best for code review)** |
| `ollama/deepseek-v4-flash` | Fast 1M-context MoE, good cheap fallback |
| `ollama/deepseek-v4-pro` | Stronger DeepSeek, three reasoning modes |
| `ollama/glm-5.2` | Z.ai flagship for long-horizon tasks |
| `ollama/glm-5.1` | Strong coding, prior flagship |
| `ollama/kimi-k2.6` | General multimodal agentic |
| `ollama/kimi-k2.5` | Earlier Kimi, still solid |
| `ollama/minimax-m3` | 1M context, native multimodality |
| `ollama/minimax-m2.7` | Coding + agentic workflows |
| `ollama/qwen3.5:122b` | Largest Qwen 3.5 tag, multimodal |
| `ollama/qwen3.5:27b` | Mid-size Qwen, faster |
| `ollama/gemma4:31b` | Frontier at size, reasoning + coding |
| `ollama/mistral-large-3` | Enterprise general-purpose MoE |
| `ollama/gpt-oss:120b` | OpenAI open-weight reasoning |
| `ollama/nemotron-3-ultra` | NVIDIA, long-running agents |
| `ollama/nemotron-3-super` | NVIDIA 120B MoE, 12B active |

Verify the live list anytime: <https://ollama.com/search?c=cloud>. Ollama retires older cloud models on a published schedule — if a model 404s, pick another from that page.

**To use a local Ollama daemon instead of Cloud**: set `CONFIG__MODEL` to e.g. `ollama/qwen2.5-coder:32b` and set `[ollama] api_base = "http://host.docker.internal:11434"` (and leave `api_key` empty) in your `.secrets.toml`. The container reaches the host's Ollama via Docker's host bridge.

### Neuralwatt (`openai/` prefix)

Neuralwatt (<https://portal.neuralwatt.com>) is an OpenAI-compatible inference provider with energy-transparent pricing. Use the `openai/` prefix and set `[openai] api_base = "https://api.neuralwatt.com/v1"` + your `sk-` key in `.secrets.toml`. Verified live from `GET https://api.neuralwatt.com/v1/models` (2026-07-20):

| Model | Context | Price (in/out per M tokens) | Notes |
|---|---|---|---|
| `openai/kimi-k2.7-code` | 262K | $0.95 / $4.00 | **Coding-focused agentic (DEFAULT for Neuralwatt — same model as the Ollama default)** |
| `openai/glm-5.2` | 1048K | $1.45 / $4.50 | ZhipuAI flagship, reasoning, 1M context |
| `openai/glm-5.2-fast` | 1048K | $1.45 / $4.50 | Non-reasoning variant of glm-5.2, same price |
| `openai/glm-5.2-short` | 200K | $1.45 / $4.50 | Reasoning, shorter context (cheaper fit for small PRs) |
| `openai/glm-5.2-short-fast` | 200K | $1.45 / $4.50 | Non-reasoning short-context variant |
| `openai/qwen3.5-397b` | 262K | $0.69 / $4.14 | Largest Qwen 3.5, reasoning |
| `openai/qwen3.5-397b-fast` | 262K | $0.69 / $4.14 | Non-reasoning variant, same price |
| `openai/qwen3.6-35b` | 131K | $0.29 / $1.15 | Mid-size Qwen, reasoning, cheapest reasoning option |
| `openai/qwen3.6-35b-fast` | 131K | $0.29 / $1.15 | Non-reasoning variant |
| `openai/kimi-k2.6` | 262K | $0.69 / $3.22 | Moonshot multimodal agentic, reasoning |
| `openai/kimi-k2.6-fast` | 262K | $0.69 / $3.22 | Non-reasoning variant |
| `openai/gemma-4-31b` | 262K | $0.14 / $0.42 | Google multimodal, cheapest option, vision-capable |

Always-current list: `curl https://api.neuralwatt.com/v1/models` (no auth needed). Get a key at <https://portal.neuralwatt.com> (Dashboard → API Keys).

> **Note on `-fast` variants:** the `-fast` suffix marks the non-reasoning variant of a model (verified against `/v1/models`: `reasoning=False` vs `True` for the base model). They skip the internal chain-of-thought trace and are cheaper per token, at the cost of less thorough analysis — fine for a cheap fallback, less ideal as the primary reviewer. All Neuralwatt models keep `tools=True` regardless, so tool-calling features still work. For the primary reviewer, prefer the reasoning variants (`kimi-k2.7-code`, `glm-5.2`, `qwen3.5-397b`); use a `-fast` model as the `CONFIG__FALLBACK_MODELS` entry when cost matters more than depth.

### Other OpenAI-compatible providers (`openai/` prefix)

The same `openai/` prefix works for any OpenAI-compatible endpoint. Set `[openai] api_base` to the provider's `/v1` URL and use its model id after the slash. Examples:

| Provider | `api_base` | Example `CONFIG__MODEL` |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `openai/gpt-4o`, `openai/o3-mini` |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/anthropic/claude-4-opus` (use OpenRouter's `model` string) |
| Together | `https://api.together.xyz/v1` | `openai/meta-llama/Llama-3.3-70B-Instruct-Turbo` |
| Groq | `https://api.groq.com/openai/v1` | `openai/llama-3.3-70b-versatile` |
| DeepSeek | `https://api.deepseek.com/v1` | `openai/deepseek-chat` |
| local vLLM | `http://host.docker.internal:8000/v1` | `openai/<your-loaded-model>` |
| local LM Studio | `http://host.docker.internal:1234/v1` | `openai/<your-loaded-model>` |

---

## The `/status` page

| Endpoint | Returns |
|---|---|
| `GET /` | `{"status":"ok"}` — healthcheck (used by Docker healthcheck) |
| `GET /status` | HTML page (auto-refreshing every 5s) |
| `GET /status.json` | Machine-readable JSON |

Shows: bot identity (App ID), configured model + fallbacks, **LLM endpoint** (provider from the model prefix + the base URL litellm actually uses — Ollama wins if both `[openai]` and `[ollama]` are set, surfaced as a `⚠ provider_mismatch` warning), AI timeout, webhook path, deployment type, uptime, start time, **in-flight reviews** (PR URL + command + age), and the last 80 log lines.

**In-flight detection** is heuristic: it parses recent log lines for pr-agent's `Performing auto command '<cmd>', for api_url='...'` and `Processing comment on PR api_url='...'` markers, and drops entries older than 6 minutes (the LLM call is bounded by `CONFIG__AI_TIMEOUT`, default 300s). It's an operational dashboard, not an authoritative registry.

**What it does NOT show**: diff content, secrets, API keys, the GitHub private key, or the webhook secret. Only repo/PR URLs, model names, and log lines (which pr-agent does not log secrets in).

---

## Security

- **Webhook authentication is enforced at boot.** The container refuses to start if `github.webhook_secret` is missing, empty, or still the placeholder. This closes a real upstream gap: `get_body()` in `pr_agent/servers/github_app.py` only calls `verify_signature()` inside `if webhook_secret:`, so a misconfigured secret would silently leave the endpoint unauthenticated. Fail-fast > silent vuln.
- **Guard the `/status` page.** It shows repo/PR URLs and the model name — not secrets, but not something you want public. Gate it at the tunnel/reverse-proxy layer: Cloudflare Access, Tailscale ACL, or basic auth on Caddy/Traefik. The page deliberately does no auth of its own (the webhook endpoint must be public, so layering auth only on `/status` would be security theater).
- **`--forwarded-allow-ips` is scoped to private CIDRs** (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.1) in the status image's CMD, not the upstream `*` wildcard. Your tunnel/reverse proxy sits in those ranges; clients can't spoof `X-Forwarded-*` headers.
- **HMAC signature validation** (`pr_agent/servers/utils.py:verify_signature`) uses `hmac.compare_digest` (constant-time) and raises 403 on missing/mismatched signatures.

---

## Operating notes

- **Unraid version**: run **7.3.2 or later**. It fixes CVE-2026-3838 (a path-traversal command-execution vulnerability in the WebGUI's `update.php`) and ships security fixes across openssl, nginx, curl, samba, php, openssh, and more. The 7.3.0 release added an optional fixed-MAC field to Docker templates — our template doesn't use it (the bot reaches out to GitHub/Ollama; nothing needs to reach it by a stable MAC), and 7.3.x leaves existing templates unchanged when networking is owned by the template (as ours is). Our template uses only the standard `Container version="2"` schema with `Variable`/`Path`/`Port` config types on a `bridge` network — fully compatible with 7.3.2, no template changes required.
- **Cost**: LLM providers bill per token. `kimi-k2.7-code` is optimized for lower thinking-token usage; a typical PR review is well under $0.05. Monitor at your provider's dashboard: Ollama Cloud &lt;https://ollama.com/settings/keys&gt;, Neuralwatt &lt;https://portal.neuralwatt.com&gt;.
- **Model retirements**: providers periodically retire models. Check Ollama &lt;https://ollama.com/search?c=cloud&gt; or Neuralwatt `curl https://api.neuralwatt.com/v1/models` and update `CONFIG__MODEL` / `CONFIG__FALLBACK_MODELS` when needed.
- **Restart policy**: the compose file sets `unless-stopped`, so reviews survive Unraid reboots once the Docker service comes back.
- **Updates**: to pick up upstream pr-agent fixes, `git pull` in this directory and re-run both `docker build` commands from step 2, then restart the container. No patches to reapply — all your customizations are env vars + the child image.
- **Multiple repos**: one App install covers every repo you add in the Install App tab. No per-repo config needed — each reviewed repo can optionally drop a `.pr_agent.toml` at its root to override tools per-repo.
- **Forked PRs**: if you accept external contributor PRs, the webhook handler works regardless of PR source (it uses the GitHub API to fetch PR data, not a local checkout).

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| Container exits at boot with `FATAL: github.webhook_secret is not configured (or is blank/placeholder)` | You didn't create/fill `/mnt/user/appdata/pr-agent/.secrets.toml`, or `webhook_secret` is empty/whitespace/still the placeholder. The guard refuses to boot rather than run an unauthenticated webhook. Create the file from `secrets.example.toml` and fill in the secret from step 1. |
| `docker build` step 2 fails with `FROM local/pr-agent:github_app` not found | You skipped step 1 (the base image). Build the base first. |
| GitHub App **Recent Deliveries** shows 403 | Wrong webhook secret, or the tunnel isn't forwarding. Re-check the secret matches between the App settings and `.secrets.toml`. |
| GitHub App shows redelivery loops (200 but no review appears) | Check `docker logs pr-agent` — likely an LLM auth error (wrong `api_key` for your provider) or a retired/wrong model (404). Update the key in `.secrets.toml` or `CONFIG__MODEL`. Also check the `/status` page's **LLM endpoint** row: the bracket shows the provider derived from your model prefix, the URL is the endpoint litellm actually uses (Ollama wins if both sections are set). If you see a `⚠ BOTH [openai] and [ollama] api_base are set` warning, both provider sections are filled in `.secrets.toml` — comment out the one for the provider you're NOT using. |
| `/status` shows no in-flight reviews even when a review is running | The review may have emitted >80 log lines (large PR) pushing the start marker out of the recent window, OR you're running `GUNICORN_WORKERS>1` and the review is on a different worker. Keep workers at 1. |
| Reviews post as `github-actions[bot]` not `<your-app>[bot]` | `deployment_type` isn't `app`, or the App isn't installed on the repo. Check `.secrets.toml` has `deployment_type = "app"` and the App is installed via the Install App tab. |
| `Model not found` / 404 errors in logs | The model id is wrong, retired, or the prefix doesn't match your provider. Verify: Ollama at <https://ollama.com/search?c=cloud>, Neuralwatt at `curl https://api.neuralwatt.com/v1/models`, or your provider's model list. Update `CONFIG__MODEL`. Remember the prefix must match the filled-in secrets section (`ollama/` ↔ `[ollama]`, `openai/` ↔ `[openai]`), and only ONE section should be filled in. |

---

## Architecture decision notes

**Why self-hosted instead of GitHub Actions?** Three reasons, in order: (1) distinct bot identity requires a GitHub App, which requires a webhook receiver you host; (2) no GitHub-side review timeout — the webhook acks in milliseconds, the LLM call runs on a background task, so slow models are fine; (3) one App install covers all repos, no per-repo workflow + secret.

**Why Ollama Cloud as the default instead of local Ollama / another provider?** No GPU box required, any model in the catalog, pay-per-token. To switch to local Ollama later, change `api_base` in `.secrets.toml` and `CONFIG__MODEL` — no code changes. To switch to Neuralwatt or any other OpenAI-compatible provider, change the model prefix to `openai/...`, fill in `[openai]`, and comment out `[ollama]` — see "Choosing an LLM provider" above. The provider layer is config-only by design: pr-agent already routes by model prefix through litellm, so adding a provider is never a code change in this fork.

**Why a child Docker image for the status page instead of editing upstream?** Zero merge debt. `git pull` + rebuild picks up upstream fixes; the status layer is two files copied on top. No patches to reapply.

**Why env vars for config instead of a mounted `configuration.toml`?** pr-agent's `config_loader.py` loads `settings_prod/.secrets.toml` but NOT `settings_prod/configuration.toml` — a config file mounted there is silently ignored. Env vars (dynaconf env_loader) are the supported override path and win over upstream defaults. JSON-array and boolean env vars parse correctly to lists/bools even with `AUTO_CAST_FOR_DYNACONF=false` (verified — dynaconf's `parse_with_toml` handles it).

---

## Credits

This fork builds on [`the-pr-agent/pr-agent`](https://github.com/the-pr-agent/pr-agent) (formerly `Codium-ai/pr-agent`), the open-source AI PR reviewer donated to the community by Qodo. All credit for the review/improve/describe tools, PR compression, and multi-provider LLM support belongs to that project. This fork adds only the Unraid deployment scaffolding, the status page, and the webhook-secret boot guard.