# NOC handover bot — Slack setup checklist

## Recommended: n8n (not Socket Mode)

Production flow:

1. User **@NOC Handover** in the alert thread  
2. **n8n** Slack trigger / workflow receives the event  
3. n8n **HTTP POST** → `http://a2a-webhook-api.a2a-ops.svc.cluster.local:8090/investigate`  
4. Response `slack_text` → n8n posts back in the same thread  

See **[N8N_INTEGRATION.md](./N8N_INTEGRATION.md)**. Deploy API: `./scripts/deploy-webhook-api-incluster.sh`.

The sections below apply only if you run **`master/slack_bot.py`** (Socket Mode) instead of n8n.

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
