# A2A Ops Agent — NOC handover for Slack alerts

Operations assistant for **NOC-Automator / Trigmetry** Slack alerts. A **central master layer** parses the alert, routes to the right **A2A specialist agent**, and posts investigation results back in the thread.

Uses **Google's A2A protocol** (HTTP + JSON-RPC) between services. **Temporal is not used** in the current design — orchestration is direct HTTP from the master layer to agents.

---

## How it works (today)

```
Slack @NOC Handover (thread)
  │
  ▼
slack-bot (thin client — thread parse only)
  │  A2A tasks/send
  ▼
┌─────────────────────────────────────────────────────────┐
│  master-agent (:8095) — A2A server like specialists      │
│  • agent_card.json — published at /.well-known/agent.json│
│  • registry.py — discovers specialist Agent Cards        │
│  • router.py — L1 alertname → L2 keywords → L3 LLM*      │
│  • orchestrator.py — A2A tasks/send to specialist        │
└──────────────────────────┬──────────────────────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
  prometheus-agent   rds-agent      kubernetes-agent
     :8080              :8081            :8082

* LLM routing only when alertname + keywords are ambiguous (needs OPENAI_API_KEY)
  K8s agent uses LLM for intent parsing on complex natural-language queries.
```

**Cluster (production):** `./scripts/deploy-production-incluster.sh` — agents + master + slack-bot in `a2a-ops` (no local processes).

**Cluster (agents only):** `./scripts/deploy-all-agents-incluster.sh` then `./scripts/deploy-slack-bot-incluster.sh`

**Local dev only:** `./scripts/run-all-agents-local.sh` → `./scripts/run-master-agent-local.sh` → `./scripts/restart-slack-bot.sh`

### Entry points (pick one)

| Path | When to use |
|------|-------------|
| **Slack Socket Mode** — `master/slack_bot.py` | `@NOC Handover` in alert thread (current demo) |
| **n8n webhook** — `master/webhook_api.py` | Slack → n8n → `POST /investigate` (see [docs/N8N_INTEGRATION.md](docs/N8N_INTEGRATION.md)) |

Both call **master-agent** via **A2A** (`MASTER_AGENT_URL` → `tasks/send`) which routes to specialists.

---

## Architecture (no Temporal)

| Layer | Role |
|-------|------|
| **Slack / n8n** | Human trigger, thread context |
| **master-agent** | Agent Card registry, L1/L2/L3 routing, A2A dispatch |
| **A2A** | Standard task/artifact exchange between master and agents |
| **Agents** | Read-only domain experts (K8s API, PromQL, SQL, …) |

```
┌──────────────────────────────────────────────────────────────┐
│  Slack (private channel)  or  n8n workflow                   │
└────────────────────────────┬─────────────────────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  slack_bot.py / webhook_api.py  (thin clients)               │
│    alert_parser.py, slack_thread.py — parse NOC thread       │
└────────────────────────────┬─────────────────────────────────┘
                             │  A2A tasks/send (+ alert metadata)
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  master-agent (:8095) — A2A server (master/server.py)        │
│    agent_card.json — GET /.well-known/agent.json             │
│    registry.py  — discovers specialist Agent Cards (AGENT_URLS)│
│    router.py    — L1 alertname → L2 keywords → L3 LLM*       │
│    orchestrator.py — A2A tasks/send to specialist              │
└──────────────┬─────────────────┬─────────────────┬───────────┘
               ▼                 ▼                 ▼
     prometheus-agent    rds-agent      kubernetes-agent
          :8080             :8081             :8082

* LLM routing (Layer 3) only when alertname + keywords are ambiguous.
  K8s agent uses LLM for natural-language intent when metadata is incomplete.
```

**Routing:** `KubePodCrashLooping` and other `Kube*` alert names → **kubernetes-agent** (Layer 1). Prometheus/RDS agents are stub A2A servers today; master routes to them when alert context or keywords match.

**Future (optional):** Temporal could be added later for durable multi-turn sessions and retries. It is **out of scope** for the current build.

---

## NOC alert field mapping

| Slack / Trigmetry field | Used for |
|-------------------------|----------|
| `alertname` | Alert type (e.g. `KubePodCrashLooping`) → agent choice |
| `namespace` | Kubernetes namespace |
| `alert_sre_attributes` | **Pod name** (e.g. `test-crashloop`) |
| `cluster`, `region`, `severity`, … | Context in Slack reply |

NOC-Automator often formats fields as `` `alertname`:KubePodCrashLooping `` — the parser normalizes that.

---

## Repository layout

```
hand-call-main/
├── master/                    # Central orchestration (Slack + alerts)
│   ├── server.py              # master-agent A2A server (:8095)
│   ├── agent_card.json        # Master Agent Card (router/orchestrator)
│   ├── orchestrator.py        # A2A dispatch to specialists
│   ├── registry.py            # Specialist Agent Card discovery (AGENT_URLS)
│   ├── router.py              # L1 alertname / L2 keywords / L3 LLM
│   ├── master_client.py       # Slack → A2A tasks/send to master-agent
│   ├── alert_metadata.py      # AlertContext ↔ A2A task metadata
│   ├── slack_bot.py           # Socket Mode bot
│   ├── slack_thread.py        # Thread read, investigation flow
│   ├── alert_parser.py        # NOC-Automator parse + query build
│   ├── webhook_api.py         # n8n / HTTP investigate API
│   └── samples/               # Example alert bodies
│
├── agents/
│   ├── kubernetes/            # ✅ A2A K8s agent (:8082)
│   ├── prometheus/            # 🔜 stub A2A agent (:8080)
│   └── rds/                   # 🔜 stub A2A agent (:8081)
│
├── common/                    # Shared A2A + LLM
│   ├── a2a_server.py
│   ├── a2a_client.py
│   ├── models.py
│   └── llm.py
│
├── deploy/kubernetes/         # EKS: agent, RBAC, demo pod, webhook API
├── docs/                      # SLACK_SETUP.md, N8N_INTEGRATION.md
├── scripts/                   # deploy, port-forward, ask-agent, restart bot
└── requirements.txt
```

---

## Core concepts (A2A)

### Agent Card

Each agent (master + specialists) publishes capabilities at `GET /.well-known/agent.json`.

- Master: `master/agent_card.json` (skills: investigate, route to K8s/Prom/RDS)
- Kubernetes: `agents/kubernetes/agent_card.json` (skills: pod status, logs, deployments, events)

### Task and artifact

- **Task** — JSON-RPC `tasks/send` with a user message and optional **metadata** (`namespace`, `pod`, `alertname`, …).
- **Artifact** — Text result returned to the master layer and posted to Slack.

```text
slack_bot / webhook          master-agent              kubernetes-agent
     │  A2A tasks/send
     │  { message, metadata: { alertname, pod, namespace, … } }
     │────────────────────────────▶
     │                            │  A2A tasks/send
     │                            │  { message, metadata: { pod, namespace } }
     │                            │────────────────────────────▶
     │                            │◀────────────────────────────
     │◀────────────────────────────
     │  artifact text + routed_agent in task metadata
```

---

## Quick start

### 1. Kubernetes agent (cluster)

```bash
./scripts/deploy-k8s-agent-incluster.sh
./scripts/create-llm-secret.sh          # OPENAI_API_KEY for intent parsing
kubectl get pods -n a2a-ops
```

Demo crashloop pod: `test-crashloop` in namespace `a2a-ops` — see `deploy/kubernetes/test-crashloop-pod.yaml`.

### 2. CLI test (no Slack)

```bash
kubectl port-forward -n a2a-ops svc/kubernetes-agent 8082:8082
./scripts/ask-agent.sh "what is wrong with test-crashloop in namespace a2a-ops"
```

### 3. Slack bot (local)

```bash
cp .env.example .env.local    # SLACK_BOT_TOKEN, SLACK_APP_TOKEN, …
./scripts/restart-slack-bot.sh
```

Private channel: scopes `groups:history`, `groups:read`, events `app_mention` + `message.groups` — see [docs/SLACK_SETUP.md](docs/SLACK_SETUP.md).

In the **NOC-Automator alert thread**: `@NOC Handover investigate` or ask about crashloop.

---

## Configuration

| Variable | Purpose |
|----------|---------|
| `KUBERNETES_AGENT_URL` | A2A endpoint (default in-cluster `:8082`) |
| `OPENAI_API_KEY` | Optional LLM intent on kubernetes-agent |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | Socket Mode bot |
| `SLACK_ALERT_CHANNEL_IDS` | Optional channel allowlist |
| `WEBHOOK_API_KEY` | Optional auth for n8n → `webhook_api` |

Example multi-agent config (when Prom/RDS exist):

```bash
# Future master routing
AGENT_URLS=http://prometheus-agent:8080,http://rds-agent:8081,http://kubernetes-agent:8082
```

Today only `KUBERNETES_AGENT_URL` is wired in code.

---

## Request flow (KubePodCrashLooping)

1. NOC-Automator posts alert with `namespace:a2a-ops`, `alert_sre_attributes:test-crashloop`.
2. User **replies in that thread**: `@NOC Handover What is the reason for pod crashloopbackoff?`
3. Master reads thread, parses fields, builds query with pod + namespace.
4. Master sends A2A task with metadata `{ pod: test-crashloop, namespace: a2a-ops }`.
5. kubernetes-agent describes pod, fetches logs, returns artifact.
6. Master posts formatted reply in the thread.

---

## Roadmap

| Component | Status |
|-----------|--------|
| kubernetes-agent | ✅ Deployed, Slack path working |
| master alert parse + thread | ✅ |
| prometheus-agent | 🔜 A2A server + PromQL |
| rds-agent | 🔜 A2A server + read-only SQL |
| master multi-agent router | 🔜 Route by `alertname` / Agent Cards |
| Temporal orchestration | ⏸️ Skipped for now |

---

## Adding a new specialist agent

1. Add `agents/<name>/server.py` extending `common.a2a_server.A2AServer`.
2. Add `agents/<name>/agent_card.json`.
3. Deploy Service on a port (e.g. 8080 / 8081).
4. Extend master routing (alertname tags or Agent Card registry) to call the new URL via `A2AClient`.

No Temporal required for basic routing.

---

## Security

- Kubernetes agent: read-only RBAC (`deploy/kubernetes/rbac.yaml`).
- LLM and Slack tokens via Kubernetes Secrets / `.env.local` (gitignored).
- Webhook API optional `WEBHOOK_API_KEY` for n8n.

---

## Tech stack

| Area | Technology |
|------|------------|
| Language | Python 3.11+ |
| Agent protocol | A2A (FastAPI, JSON-RPC, SSE-capable server) |
| Slack | slack-bolt (Socket Mode) |
| Kubernetes | Official `kubernetes` client |
| LLM (optional) | OpenAI via `common/llm.py` |
| Orchestration | **Direct HTTP** (no Temporal in current design) |

---

## Docs

- [docs/SLACK_SETUP.md](docs/SLACK_SETUP.md) — OAuth scopes, private channels, Socket Mode
- [docs/N8N_INTEGRATION.md](docs/N8N_INTEGRATION.md) — n8n webhook flow
