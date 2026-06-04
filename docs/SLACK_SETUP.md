# NOC handover bot — Slack setup checklist

## Production (recommended): everything in cluster

No local `run-slack-bot-local.sh` or port-forward needed. Slack Socket Mode connects **outbound** from the `slack-bot` pod to Slack — **Ingress is not required** for @mentions.

```bash
# 1. Secrets from .env.local (once)
./scripts/create-llm-secret.sh
./scripts/create-slack-secret.sh

# 2. Deploy full stack into a2a-ops
./scripts/deploy-production-incluster.sh

# 3. Stop local duplicates on your laptop (important!)
pkill -f 'master.slack_bot|master.server|agents.kubernetes.server' || true

# 4. Verify
kubectl get pods -n a2a-ops
kubectl logs -n a2a-ops -l app.kubernetes.io/name=slack-bot --tail=30
```

After `@NOC Handover` in Slack, master logs should show `POST /` and `Master routing → kubernetes-agent` (not only `GET /health`).

**Optional Ingress** — only if n8n or external systems call the webhook API from outside the cluster:

```bash
# Edit host/TLS in deploy/kubernetes/ingress-webhook.yaml first
./scripts/deploy-webhook-api-incluster.sh
./scripts/deploy-production-incluster.sh --with-ingress
```

---

## Recommended: n8n (alternative to Socket Mode)

Production flow without Socket Mode:

1. User **@NOC Handover** in the alert thread  
2. **n8n** Slack trigger / workflow receives the event  
3. n8n **HTTP POST** → webhook API (ClusterIP or Ingress)  
4. Response `slack_text` → n8n posts back in the same thread  

See **[N8N_INTEGRATION.md](./N8N_INTEGRATION.md)**. Deploy API: `./scripts/deploy-webhook-api-incluster.sh`.

The sections below apply if you run **`master/slack_bot.py` locally** (dev only) or need Slack app configuration details.

---

## 1. Install app on workspace

**Settings → Install App** → Install / Reinstall to Workspace (admin approval if required).

## 2. OAuth scopes (Bot Token Scopes)

### Private channel only (your case)

| Scope | Why |
|-------|-----|
| `app_mentions:read` | Receive `@NOC Handover` |
| `chat:write` | Reply in thread |
| `groups:history` | **Read** NOC-Automator alert + thread messages |
| `groups:read` | **Channel info** / membership |

`channels:history` does **not** apply to private channels — without `groups:history` the bot sees your @mention but gets **zero** thread text (empty `alertname:`).

### Public channel (optional)

Also add: `channels:history`, `channels:read`

## 3. Event Subscriptions (required even with Socket Mode)

1. **Event Subscriptions** → turn **ON**
2. Under **Subscribe to bot events**:

**Private channel:**

- `app_mention`
- `message.groups` ← required (not `message.channels`)

**Public channel:**

- `app_mention`
- `message.channels` (fallback for thread @mentions)

3. Save changes → **Reinstall app** if prompted

`message.channels` does **not** fire in private channels.

## 4. Socket Mode

1. **Socket Mode** → **ON**
2. Generate App-Level Token (`connections:write`) → `SLACK_APP_TOKEN`

## 5. Invite bot to channel

In the **private** alerts channel: `/invite @NOC Handover`

## 5b. Verify access (run locally)

```bash
# .env.local: SLACK_BOT_TOKEN, SLACK_ALERT_CHANNEL_IDS=C0B7R75EQ8K
./scripts/check-slack-channel-access.py
```

Expect `OK conversations.history` and `is_member=True`. If you see `missing_scope` → add `groups:history` + reinstall.

## 6. Channel ID filter (optional)

If `SLACK_ALERT_CHANNEL_IDS` is set in `.env.local`, the bot only responds in those channels.

To find channel ID: open channel in browser → URL contains `C...`

Remove `SLACK_ALERT_CHANNEL_IDS` to allow all channels (local dev).

## 7a. Option A — Socket Mode (no public URL)

See `scripts/run-slack-bot-local.sh`

## 7b. Option B — HTTP webhook (ngrok)

Slack sends events to your laptop:

```bash
# Terminal 1
kubectl port-forward -n a2a-ops svc/kubernetes-agent 8082:8082

# Terminal 2
./scripts/run-slack-bot-http.sh

# Terminal 3
ngrok http 3000
```

Set **Event Subscriptions → Request URL** to:

`https://<ngrok-host>/slack/events`

Subscribe to: `app_mention`, `message.channels`

## 7c. Option C — Incoming webhook only (quick test, no @mention)

Incoming webhooks **only post messages** — they cannot receive `@NOC Handover`.

```bash
# In .env.local:
# SLACK_INCOMING_WEBHOOK_URL=https://hooks.slack.com/services/...

./scripts/test-webhook-flow.sh
```

This posts a sample alert + investigation result to the channel. For real thread replies, use Option A or B.

## 7. Run locally (Socket Mode)

```bash
# Terminal 1 — keep running
kubectl port-forward -n a2a-ops svc/kubernetes-agent 8082:8082

# Terminal 2
./scripts/run-slack-bot-local.sh
```

When you @mention the bot, logs should show:

```text
app_mention channel=C... thread=...
```

or

```text
message_mention channel=C... thread=...
```
