# n8n + Slack + A2A Ops flow

## Architecture (your model)

```text
Slack alert thread
    ↓
User @NOC Handover investigate
    ↓
n8n (Slack trigger → Webhook workflow)
    ↓
POST https://<your-host>/investigate
    ↓
kubernetes-agent (A2A) — GPT + K8s API
    ↓
JSON answer back to n8n
    ↓
n8n → Slack (post message in same thread)
```

Socket Mode on the Python bot is **not required** for this path.

---

## 1. Run the webhook API

**Local (with port-forward to agent):**

```bash
# Terminal 1
kubectl port-forward -n a2a-ops svc/kubernetes-agent 8082:8082

# Terminal 2
export KUBERNETES_AGENT_URL=http://127.0.0.1:8082
./scripts/run-webhook-api-local.sh
```

Listens on **http://0.0.0.0:8090**

**In cluster:**

```bash
./scripts/deploy-webhook-api-incluster.sh
```

Service: `http://a2a-webhook-api.a2a-ops.svc.cluster.local:8090`

---

## 2. n8n workflow (outline)

### Trigger

- **Slack Trigger** (or Slack node on `@mention` / message in channel)
- Filter: message contains `@NOC Handover` or your bot user ID

### Build context (Code / Set node)

Concatenate thread text so the API can parse the alert:

- Parent message text (NOC-Automator alert with `alertname:`)
- User mention text (`Investigate this`)

Example fields to send:

| Field | Source |
|-------|--------|
| `text` | Thread root message + reply text |
| `channel` | Slack channel ID |
| `thread_ts` | Thread ts (parent ts if reply) |
| `user_prompt` | Text after @mention |

### HTTP Request node

- **Method:** POST  
- **URL:** `http://a2a-webhook-api.a2a-ops.svc.cluster.local:8090/investigate`  
  (or ngrok URL if n8n is outside cluster)
- **Headers:** `Content-Type: application/json`  
  Optional: `X-API-Key: <WEBHOOK_API_KEY>`
- **Body (JSON):**

```json
{
  "text": "={{ $json.combined_thread_text }}",
  "channel": "={{ $json.channel }}",
  "thread_ts": "={{ $json.thread_ts }}",
  "user_prompt": "={{ $json.user_message }}",
  "post_to_slack": false
}
```

Use `post_to_slack: true` only if you want **this API** to post to Slack (needs `SLACK_BOT_TOKEN` on the webhook pod). Otherwise n8n posts the response.

### Slack reply node

- Post `{{ $json.slack_text }}` or `{{ $json.answer }}` to the same `channel` + `thread_ts`

---

## 3. Test without Slack

```bash
curl -s -X POST http://127.0.0.1:8090/investigate \
  -H "Content-Type: application/json" \
  -d @master/samples/investigate_request.json | python3 -m json.tool
```

---

## 4. Environment

| Variable | Purpose |
|----------|---------|
| `KUBERNETES_AGENT_URL` | A2A kubernetes-agent |
| `WEBHOOK_API_KEY` | Optional auth for n8n |
| `WEBHOOK_PORT` | Default 8090 |
| `SLACK_BOT_TOKEN` | Only if `post_to_slack: true` |
