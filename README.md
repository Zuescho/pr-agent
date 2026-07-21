# pr-agent — self-hosted AI PR reviewer on Unraid (Caddy + Porkbun + GitHub App)

A self-hosted fork of [`the-pr-agent/pr-agent`](https://github.com/the-pr-agent/pr-agent) that reviews pull requests on your GitHub repos and posts comments back as a **distinct bot identity** (`<your-app>[bot]`), powered by **any OpenAI-compatible LLM** (Ollama Cloud, Neuralwatt, OpenRouter, local Ollama, …), running in Docker on Unraid.

This README is the single source of truth: what it is, how to deploy it, and every setting you can configure.

---

## What you get

- **Distinct bot identity** — reviews post as `<your-app-name>[bot]`, not `github-actions[bot]`. One GitHub App install covers every repo you add it to.
- **No GitHub Actions runner spin-up, no GitHub-side review timeout** — the webhook receiver acks GitHub in milliseconds and runs the LLM call on a background task. A slow model taking 2-3 minutes per PR is a non-issue.
- **Pluggable LLM backend** — pick any current model via one env var. Defaults to Ollama Cloud; switch to Neuralwatt or any OpenAI-compatible endpoint by changing the model prefix (`ollama/...` → `openai/...`) and the matching secrets section. No code changes, no patches.
- **Two importable Unraid templates** — one private PR-agent container plus one public Caddy edge with automatic Porkbun DNS and TLS.
- **Hands-off public ingress** — Caddy follows a changing public IPv4, renews its own certificate with DNS-01, and exposes only the intended webhook service.
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

```text
GitHub PR event
      │ HTTPS POST /api/v1/github_webhooks
      ▼
pr-agent.<PORKBUN_ZONE> ──▶ router TCP 443 ──▶ Unraid :18443 ──▶ Caddy
      ▲                                                           │
      │ Porkbun A record (Caddy DDNS, IPv4 only)                  │ pr-agent-net
      │ Let's Encrypt certificate (Porkbun DNS-01)                ▼
      └────────────────────────────────────────────────── pr-agent:3000
                                                                  │ verify HMAC
                                                                  │ ack 200
                                                                  ▼
                                                        background review tools
                                                                  │
                                                                  ▼
                                                        configured LLM provider
                                                                  │
                                                                  ▼
                                                        <your-app>[bot] comment
```

Only Caddy publishes host ports. PR-agent has no host port and is reachable from Caddy only through the private
`pr-agent-net` Docker network. Public TCP 80 is forwarded to Caddy solely for its automatic HTTP-to-HTTPS redirect;
certificate issuance and renewal use Porkbun DNS-01.

---

## Files in this fork (added on top of upstream pr-agent)

| File | Purpose |
|---|---|
| `README.md` | Complete deployment and settings reference |
| `secrets.example.toml` | Template for LLM and GitHub App credentials |
| `templates/pr-agent.xml` | Private PR-agent Unraid template |
| `templates/caddy.xml` | Public Caddy Unraid template with masked Porkbun credential fields |
| `deploy/docker-compose.yml` | Non-GUI mirror of both containers on `pr-agent-net` |
| `deploy/Caddyfile` | Dynamic A record, DNS-01 TLS, access log, and reverse-proxy contract |
| `deploy/Dockerfile.caddy` | Pinned Caddy build with Porkbun DNS and Dynamic DNS modules |
| `deploy/Dockerfile.webhook` | Webhook-only PR-agent child image with the mandatory-secret boot guard |
| `.github/workflows/docker-publish.yml` | Publishes matching PR-agent and Caddy tags to GHCR |
| `pr_agent/webhook_app.py` | Entrypoint shim that enforces the webhook-secret guard |
| `deploy/Dockerfile.status`, `pr_agent/status_page.py`, `pr_agent/status_app.py` | Optional, unguarded status image; not built or deployed by this guide |

The application customization remains isolated from upstream PR-agent. Deployment behavior lives in the child image,
environment variables, mounted secrets, Caddy configuration, and Unraid templates.

---

## Deployment guide

`PORKBUN_ZONE` always means the purchased registrable domain only, for example `example.net`. The public hostname is
always `pr-agent.<PORKBUN_ZONE>`; do not enter `pr-agent.` in the zone field.

### 1. Confirm a directly reachable public IPv4

Reserve the Unraid server's LAN IPv4 in DHCP. On the router/firewall, note the WAN IPv4. Then run this on Unraid:

```bash
curl -4 https://icanhazip.com
```

The two addresses must match. Stop and ask the ISP for a public IPv4 if the router shows a different address, an
RFC1918 address (`10/8`, `172.16/12`, or `192.168/16`), or an address in `100.64.0.0/10`. CGNAT and DS-Lite without
inbound IPv4 cannot receive direct GitHub webhooks. Also confirm that public TCP 80 and 443 are not already forwarded
to another edge proxy; two proxies cannot share one public `IP:port`.

### 2. Purchase and prepare the Porkbun DNS zone

1. Buy a dedicated domain at Porkbun, keep Porkbun's authoritative nameservers, and enable domain auto-renewal.
   Existing email domains and `zuescho.de` remain outside this deployment.
2. In **Domain Management → Details**, enable API Access for this domain as described in Porkbun's
   [API setup guide](https://kb.porkbun.com/article/190-getting-started-with-the-porkbun-api).
3. Create a dedicated API key named `pr-agent-caddy` and save its one-time secret. Apply Porkbun's
   [per-key domain restriction](https://porkbun.com/api/json/v3/documentation#api-key-scoping-ip--domain-restrictions)
   so the key can modify only this purchased domain.
4. Do **not** apply a source-IP restriction. Caddy must still authenticate after the WAN IPv4 changes. If the account
   UI cannot restrict a key by target domain, place this one domain in a separate Porkbun account; do not deploy an
   account-wide key that can modify unrelated zones.
5. In the DNS editor, remove only conflicting `A`, `AAAA`, `CNAME`, or `ALIAS` records named `pr-agent`. Do not create
   a replacement. Caddy creates and owns the IPv4 `A` record through Porkbun's
   [DNS API](https://porkbun.com/llms/dns).

Do not download or copy Porkbun certificates. Caddy owns ACME issuance, renewal, and private-key storage.

### 3. Create the GitHub App

Go to <https://github.com/settings/apps> → **New GitHub App** (or create it under an organization).

| Field | Value |
|---|---|
| GitHub App name | `pr-agent` or the desired bot name |
| Homepage URL | The repository URL or another valid homepage |
| Webhook → Active | Enabled |
| Webhook URL | `https://pr-agent.<PORKBUN_ZONE>/api/v1/github_webhooks` |
| Webhook content type | `application/json` |
| Webhook secret | A high-entropy value generated once and saved |
| Repository permissions | **Pull requests: Read & write**, **Issue comment: Read & write**, **Contents: Read-only**, **Metadata: Read-only** |
| Subscribe to events | **Pull request**, **Issue comment** |

Generate a secret locally:

```bash
python -c "import secrets; print(secrets.token_hex(20))"
```

After creating the app, generate and download its private key and record the numeric App ID. Keep the exact same
webhook secret in GitHub and `/mnt/user/appdata/pr-agent/.secrets.toml`. The PR-agent image refuses to start when this
secret is missing, blank, or still a placeholder.

### 4. Get both deployment images

Every push to `main` and every `vX.Y.Z` tag publishes matching tag sets:

```text
ghcr.io/zuescho/pr-agent:latest
ghcr.io/zuescho/pr-agent-caddy:latest
ghcr.io/zuescho/pr-agent:sha-<short>
ghcr.io/zuescho/pr-agent-caddy:sha-<short>
ghcr.io/zuescho/pr-agent:<version>
ghcr.io/zuescho/pr-agent-caddy:<version>
```

After the first successful workflow run, set **both** GHCR packages to public visibility in their package settings so
Unraid can pull anonymously. Then pull them:

```bash
docker pull ghcr.io/zuescho/pr-agent:latest
docker pull ghcr.io/zuescho/pr-agent-caddy:latest
```

For a local build instead:

```bash
docker build --target github_app -t local/pr-agent:github_app -f docker/Dockerfile .
docker build --build-arg BASE_IMAGE=local/pr-agent:github_app -t local/pr-agent:github_app-webhook -f deploy/Dockerfile.webhook .
docker build --platform linux/amd64 -t local/pr-agent-caddy -f deploy/Dockerfile.caddy .
```

The Caddy image pins Caddy 2.11.4 and exact commits of both required modules; do not replace those pins with `latest`
or module branches.

### 5. Create the PR-agent secrets file

```bash
mkdir -p /mnt/user/appdata/pr-agent
```

Copy `secrets.example.toml` to `/mnt/user/appdata/pr-agent/.secrets.toml`. Fill in exactly one LLM provider section
plus `[github]`.

**Ollama Cloud (default):**

```toml
[ollama]
api_base = "https://ollama.com"
api_key = "<your Ollama Cloud key from https://ollama.com/settings/keys>"

[github]
deployment_type = "app"
app_id = <your App ID, numeric>
webhook_secret = "<the webhook secret configured in GitHub>"
private_key = """\
-----BEGIN RSA PRIVATE KEY-----
<paste the entire downloaded PEM>
-----END RSA PRIVATE KEY-----
"""
```

**Neuralwatt or another OpenAI-compatible endpoint:**

```toml
[openai]
api_base = "https://api.neuralwatt.com/v1"
api_key = "<your provider key>"

[github]
deployment_type = "app"
app_id = <your App ID, numeric>
webhook_secret = "<the webhook secret configured in GitHub>"
private_key = """\
-----BEGIN RSA PRIVATE KEY-----
<paste the entire downloaded PEM>
-----END RSA PRIVATE KEY-----
"""
```

Set `CONFIG__MODEL` and `CONFIG__FALLBACK_MODELS` to the matching `ollama/` or `openai/` prefix. Configuration tuning
stays in environment variables; `.secrets.toml` contains credentials only.

### 6. Prepare Unraid

In **Settings → Docker**, enable preservation of user-defined networks. Inspect the required network first:

```bash
docker network inspect pr-agent-net
```

If Docker reports that it does not exist, create it once:

```bash
docker network create pr-agent-net
```

Confirm its subnet:

```bash
docker network inspect pr-agent-net --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
```

It must be inside `10/8`, `172.16/12`, or `192.168/16`, matching the PR-agent image's trusted proxy CIDRs.

From a checkout of this repository on Unraid, install the templates and Caddyfile:

```bash
mkdir -p /boot/config/plugins/dockerMan/templates-user
mkdir -p /mnt/user/appdata/caddy/data /mnt/user/appdata/caddy/config
cp templates/pr-agent.xml /boot/config/plugins/dockerMan/templates-user/pr-agent.xml
cp templates/caddy.xml /boot/config/plugins/dockerMan/templates-user/caddy.xml
cp deploy/Caddyfile /mnt/user/appdata/caddy/Caddyfile
```

The Caddyfile is mounted read-only. `/mnt/user/appdata/caddy/data` persists ACME accounts, certificates, and private
keys; `/mnt/user/appdata/caddy/config` persists Caddy state.

#### Import `pr-agent`

In **Docker → Add Container**, select `pr-agent`. The template has no WebUI shortcut and publishes no host port.

| Field | Default / required value |
|---|---|
| LLM model | `ollama/kimi-k2.7-code`; change prefix when using another provider |
| Fallback LLM model(s) | `["ollama/deepseek-v4-flash"]`; use the same provider prefix |
| Secrets file | `/mnt/user/appdata/pr-agent/.secrets.toml` |
| Model max input tokens | `128000` |
| AI call timeout (seconds) | `300` |
| Max model tokens (hard cap) | `128000` |
| Large patch policy | `clip` |
| Response language | `en-US` |
| Repo context files | `[]` |
| Gunicorn workers | `1` |
| Log level | `INFO` |
| Auto-review on PR actions | `["opened","reopened","ready_for_review","synchronize"]` |
| Auto-run tools | `["/describe --pr_description.final_update_message=false","/review","/improve"]` |
| Re-review on new commits | `true` |
| Re-review tools | `["/describe --pr_description.final_update_message=false","/review"]` |
| Review: require tests review | `false` |
| Review: require security review | `true` |
| Review: max findings | `5` |
| Improve: score threshold | `7` |
| Improve: suggestions per chunk | `3` |

Apply it and verify that the `pr-agent` container becomes healthy.

#### Import `caddy`

Select `caddy` from **Add Container** and fill every field:

| Field | Value |
|---|---|
| Porkbun DNS zone | Required; purchased registrable domain only |
| Porkbun API key | Required, masked; dedicated domain-scoped key |
| Porkbun API secret key | Required, masked; matching one-time secret |
| ACME account email | `github@zuescho.de` |
| Caddyfile | `/mnt/user/appdata/caddy/Caddyfile` (read-only) |
| Caddy data | `/mnt/user/appdata/caddy/data` |
| Caddy config state | `/mnt/user/appdata/caddy/config` |
| HTTP host port | `18080` mapped to container port 80 |
| HTTPS host port | `18443` mapped to container port 443 |

Never place either Porkbun credential in the Caddyfile, repository, image, or logs. Apply the template. Enable Unraid
AutoStart for both containers and order `pr-agent` before `caddy`. If Caddy starts first, a temporary `502` is expected
and clears automatically when Docker DNS can reach `pr-agent:3000`.

**Compose alternative:** `deploy/docker-compose.yml` is the exact non-GUI topology. Supply the three required Porkbun
variables through Compose Manager or an ignored `.env`, keep the same external `pr-agent-net`, and never commit that
file. PR-agent still has no host `ports` entry.

### 7. Configure the router and firewall

Create only these inbound forwards to the reserved Unraid LAN IPv4:

| WAN | LAN target |
|---|---|
| TCP 80 | `<reserved-Unraid-IP>:18080` |
| TCP 443 | `<reserved-Unraid-IP>:18443` |

Allow both through the firewall. Never forward PR-agent port 3000 or Caddy admin port 2019. DNS-01 does not require an
inbound port, but TCP 80 remains open for Caddy's HTTP-to-HTTPS redirect. If the router cannot translate ports, first
move and verify Unraid's own management ports, then deliberately change both Caddy mappings to `80:80` and `443:443`.
Do not overwrite an existing public service's forwards.

### 8. Verify the public edge

Watch startup:

```bash
docker logs -f caddy
```

With no pre-created `pr-agent` DNS record, Caddy must log `updating DNS record`, `finished updating DNS`, and successful
certificate issuance without Porkbun authentication, propagation, ACME, or storage errors.

From a Windows system on cellular or another external network:

```powershell
$zone = "<purchased-domain>"
$hostName = "pr-agent.$zone"
Resolve-DnsName $hostName -Type A -Server 1.1.1.1
curl.exe -I "http://$hostName/"
curl.exe -i "https://$hostName/"
curl.exe -i -X POST -H "Content-Type: application/json" --data "{}" "https://$hostName/api/v1/github_webhooks"
openssl s_client -connect "${hostName}:443" -servername $hostName -showcerts | openssl x509 -noout -subject -issuer -dates -ext subjectAltName
```

Required results:

- The public `A` record equals the router's current WAN IPv4.
- Porkbun shows exactly one `pr-agent` `A` record with TTL 600 and no `AAAA`, `CNAME`, or `ALIAS` record of that name.
- HTTP redirects to HTTPS.
- HTTPS `/` returns `200` with `{"status":"ok"}`.
- An unsigned JSON webhook traverses DNS and Caddy but returns `403` from PR-agent.
- The certificate is publicly trusted and its SAN contains exactly `DNS:pr-agent.<PORKBUN_ZONE>`.
- `docker port pr-agent` prints nothing; `docker port caddy` shows only `80/tcp -> 18080` and `443/tcp -> 18443`.

Test externally; lack of NAT reflection must not be mistaken for a broken deployment.

### 9. Install the App and exercise a real delivery

Install the GitHub App on the selected repositories. Open a PR, or use **Recent Deliveries** to manually redeliver a
`pull_request` event. GitHub does **not** automatically retry failed deliveries; Recent Deliveries can
[manually redeliver events from the previous three days](https://docs.github.com/en/webhooks/testing-and-troubleshooting-webhooks/redelivering-webhooks).

The delivery must return `200` within GitHub's
[ten-second response limit](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks#respond-to-webhook-deliveries-within-10-seconds),
then the configured bot must add its PR comments. Match the GitHub delivery with both `docker logs pr-agent` and
`docker logs caddy`.

Finally, restart both containers and repeat the HTTPS health and unsigned-webhook checks. Confirm certificate state
remains under `/mnt/user/appdata/caddy/data`. At the next ISP address change, Caddy polls within five minutes; Porkbun's
ten-minute minimum TTL means public resolvers can take about fifteen minutes total to converge without operator or
certificate intervention.

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
| `GUNICORN_WORKERS` | `1` | Number of gunicorn worker processes. Keep at 1 — the webhook acks in milliseconds and reviews run as background tasks, so one worker handles concurrent reviews fine for a personal bot. Raise only if you review many PRs simultaneously. |

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
## Endpoints

Caddy publishes the application's two routes at `https://pr-agent.<PORKBUN_ZONE>`:

| Endpoint | Returns |
|---|---|
| `POST /api/v1/github_webhooks` | GitHub webhook receiver. Requires a valid `X-Hub-Signature-256`; missing or forged signatures return 403. |
| `GET /` | `{"status":"ok"}` for health and edge verification. |

PR-agent still listens on container port 3000, but that port is not published on the Unraid host. Only Caddy can reach
it over `pr-agent-net`. The deployed image has no `/status` dashboard. The optional status image is unauthenticated and
is intentionally outside this public deployment.

---

## Security

- **HMAC is the public endpoint's authentication boundary.** The image refuses to boot without a real
  `github.webhook_secret`; `verify_signature()` uses `hmac.compare_digest` and rejects missing or mismatched
  `X-Hub-Signature-256` values with 403.
- **Do not add Basic Auth.** GitHub cannot answer an interactive authentication challenge, so deliveries would fail.
- **Do not maintain a static GitHub source-IP allowlist.** GitHub can change its ranges; signature verification is the
  durable request-authentication control.
- **PR-agent is not host-published.** Caddy is the only ingress, and Caddy's admin API on port 2019 is never published.
- **Trusted proxy headers are private-only.** The webhook image trusts forwarded headers only from `10/8`,
  `172.16/12`, `192.168/16`, and loopback. Verify `pr-agent-net` uses one of those private ranges.
- **Porkbun access is least privilege.** Use a dedicated domain-scoped key without source-IP restriction. Store it only
  in masked Unraid fields or an ignored Compose `.env`; rotate it immediately if exposed.
- **Certificate state is sensitive.** Protect and back up `/mnt/user/appdata/caddy/data`, which contains ACME accounts,
  certificates, and private keys.

---

## Operating notes

- **Unraid version:** run 7.3.2 or later and preserve user-defined Docker networks across service restarts.
- **DNS convergence:** Caddy checks every five minutes; Porkbun enforces a 600-second minimum TTL. A WAN IPv4 change can
  therefore take about fifteen minutes to reach every resolver.
- **Domain lifecycle:** keep the dedicated domain renewed, on Porkbun nameservers, and API Access enabled. No manual
  certificate replacement is part of normal operation.
- **Port 80:** leave the forward active for HTTP-to-HTTPS redirects. DNS-01 certificate renewal itself uses outbound
  Porkbun API traffic, not inbound HTTP.
- **Restart policy:** both services use `unless-stopped`. Persistent Caddy state prevents account and certificate loss
  across container recreation.
- **Updates:** the workflow publishes matching PR-agent and Caddy `latest`, `sha-*`, and release tags. Pull and restart
  both images together; use matching immutable tags when pinning a deployment.
- **Failed GitHub deliveries:** they are not retried automatically. Manually redeliver eligible events from Recent
  Deliveries within three days after the edge is healthy.
- **Multiple repositories and forked PRs:** one App installation can cover every selected repository, including PRs
  whose head branch comes from a fork.
- **LLM cost and model lifecycle:** monitor the chosen provider and update model IDs when the provider retires them.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Router WAN IPv4 differs from `curl -4 https://icanhazip.com`, or is private/`100.64/10` | CGNAT or DS-Lite blocks direct inbound IPv4. Stop and request a public IPv4 from the ISP. |
| `pr-agent.<PORKBUN_ZONE>` does not resolve | Confirm the domain is not expired, still uses Porkbun nameservers, and has API Access enabled. Inspect `docker logs caddy` for DDNS errors. |
| Caddy logs Porkbun authentication errors | The key/secret pair is wrong, revoked, or API Access is disabled. Reissue the dedicated key and keep it restricted to only the deployment domain, with no source-IP rule. |
| The Porkbun key can modify unrelated domains | The key is over-scoped. Restrict it to the purchased domain before deployment; if the UI cannot do that, isolate the domain in a separate Porkbun account. |
| Caddy logs DNS update errors | Remove conflicting `pr-agent` `AAAA`, `CNAME`, or `ALIAS` records, confirm the zone value is the registrable domain only, and verify Porkbun remains authoritative. |
| Caddy logs ACME propagation or challenge errors | Verify the Porkbun module can create `_acme-challenge` records, wait for DNS propagation, check outbound DNS/HTTPS, and keep `/data` writable. |
| HTTPS returns `502` | Caddy cannot resolve or connect to `pr-agent:3000`. Confirm both containers are running on `pr-agent-net`, PR-agent is healthy, and the network subnet is private. |
| Router cannot forward public 80/443, or another service already owns them | Do not overwrite the existing rules. Make one Caddy instance the shared edge, or redesign the edge before deployment. |
| `docker port pr-agent` prints a host mapping | The old template is still active. Remove the mapping and re-import the current private-network template; never expose port 3000. |
| GitHub Recent Deliveries returns 403 | The request reached PR-agent but the webhook secrets differ. Put the exact same high-entropy secret in GitHub and `.secrets.toml`. |
| GitHub delivery returns 200 but no review appears | Check `docker logs pr-agent` for App installation, provider authentication, model ID, or mixed-provider configuration errors. |
| A failed GitHub delivery never retries | Expected. Use Recent Deliveries to manually redeliver an event from the previous three days. |
| Either GHCR image returns `manifest unknown` | The first workflow run has not published that package/tag. Wait for the Docker publish workflow to succeed. |
| Pulling either GHCR image returns `denied` | Make both packages public-readable, or authenticate Unraid to GHCR with `read:packages`. |

---

## Architecture decision notes

**Why self-hosted instead of GitHub Actions?** A GitHub App provides a distinct bot identity and one installation across
multiple repositories. The webhook is acknowledged immediately while slow review work runs in the background.

**Why a dedicated Porkbun domain?** It isolates webhook DNS and API permissions from existing web and mail domains.
Changing the deployment requires only `PORKBUN_ZONE` and the GitHub Webhook URL.

**Why Caddy owns DNS, TLS, and ingress?** One process observes the WAN IPv4, updates the direct `A` record, completes
DNS-01, persists certificate state, redirects HTTP, and proxies to a private Docker service. There is no certificate
copy job and no direct PR-agent host port.

**Why HMAC instead of Basic Auth or an IP allowlist?** GitHub signs every delivery with the shared webhook secret.
Basic Auth would break deliveries, while provider IP ranges can change. Constant-time HMAC validation authenticates
the payload without ongoing network-list maintenance.

**Why a child PR-agent image instead of editing upstream?** The boot guard and entrypoint stay isolated, minimizing
merge debt when upstream changes.

**Why environment variables instead of a mounted `configuration.toml`?** PR-agent loads
`settings_prod/.secrets.toml`, but not `settings_prod/configuration.toml`. Dynaconf environment variables are the
supported override path and take precedence over baked-in defaults.

---

## Credits

This fork builds on [`the-pr-agent/pr-agent`](https://github.com/the-pr-agent/pr-agent) (formerly `Codium-ai/pr-agent`), the open-source AI PR reviewer donated to the community by Qodo. All credit for the review/improve/describe tools, PR compression, and multi-provider LLM support belongs to that project. This fork adds only the Unraid deployment scaffolding, the webhook-only image with its boot guard, and the CI build pipeline.
