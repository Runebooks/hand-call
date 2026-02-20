# A2A Ops Agent — Agent-to-Agent Communication Platform

An interactive operations assistant that uses **Google's A2A protocol** for agent communication,
**Temporal** for durable workflow orchestration, and **Slack** as the human-in-the-loop interface.

Ask questions in plain English via Slack — the system routes your query to the right agent,
fetches the data, and comes back with the answer. The conversation continues until you say stop.

---

## How It Works (In Simple Terms)

```
You (in Slack)
  │
  │  "Why is the payments service slow?"
  ▼
┌──────────────┐
│ Slack Bot    │  Receives your message
└──────┬───────┘
       ▼
┌──────────────┐
│ Temporal     │  Starts a durable workflow (survives crashes)
│ Workflow     │
└──────┬───────┘
       ▼
┌──────────────┐
│ Master Agent │  Figures out which agent can answer your question
│ (Router)     │  Uses Agent Cards to know what each agent can do
└──────┬───────┘
       │
       │  Routes to the right agent via A2A protocol
       ▼
┌──────────────────────────────────┐
│ Specialized Agents (A2A Servers) │
│                                  │
│  📊 Prometheus Agent             │  → Metrics, CPU, latency, error rates
│  🗄️  RDS Agent                   │  → Database queries, slow query logs
│  ☸️  K8s Agent                   │  → Pod status, deployments, cluster state
└──────────────┬───────────────────┘
               │
               │  Returns result (Artifact)
               ▼
┌──────────────┐
│ Slack Bot    │  Posts the answer back to your Slack thread
└──────┬───────┘
       │
       │  "Here's what I found: ..."
       │  [Ask Follow-up] [Switch Agent] [Stop]
       ▼
You (in Slack)
       │
       │  Ask another question or click Stop
       │
       └──── Loop continues until you stop ────┘
```

---

## Architecture

### Two Layers Working Together

| Layer | Technology | Responsibility |
|-------|-----------|---------------|
| **Communication** | A2A Protocol | How agents discover and talk to each other |
| **Orchestration** | Temporal | When agents talk, retry logic, the Slack loop, crash recovery |

They solve different problems — A2A handles the **what and how**, Temporal handles the **when and reliability**.

### Detailed Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         TEMPORAL WORKFLOW                                │
│                  (durable, crash-safe, stateful)                         │
│                                                                         │
│  ┌───────────┐     ┌────────────────┐     ┌──────────────────────────┐  │
│  │           │     │                │     │    A2A Protocol Layer     │  │
│  │  Slack    │────▶│  Master Agent  │────▶│                          │  │
│  │  Trigger  │     │  (Router/LLM)  │     │  Discovery:              │  │
│  │           │     │                │     │  GET /.well-known/        │  │
│  └───────────┘     └────────────────┘     │      agent.json          │  │
│       ▲                                   │                          │  │
│       │                                   │  Execute:                │  │
│       │            ┌────────────────┐     │  POST /tasks/send        │  │
│       │◀───────────│  Slack Output  │◀────│                          │  │
│       │            │  + Signal Wait │     │  Stream:                 │  │
│       │            └────────────────┘     │  POST /tasks/            │  │
│       │                                   │       sendSubscribe      │  │
│       │                                   └────┬────┬────┬───────────┘  │
│  ┌────┴──────┐                                 │    │    │              │
│  │  User     │                                 ▼    ▼    ▼              │
│  │  Signal   │                           ┌────┐┌───┐┌────┐             │
│  │  (Slack)  │                           │Prom││RDS││K8s │             │
│  │           │                           │    ││   ││    │             │
│  │ continue/ │                           └────┘└───┘└────┘             │
│  │ stop      │                           Each runs as an               │
│  └───────────┘                           A2A Server with               │
│                                          its own Agent Card            │
│  ◀── Loop runs until user sends stop ──▶                               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Core Concepts

### 1. Agent Cards (A2A Discovery)

Every agent publishes a **JSON file** describing what it can do. The master agent reads these
to understand which agents are available and what they handle.

Hosted at: `https://<agent-url>/.well-known/agent.json`

**Example — Prometheus Agent Card:**

```json
{
  "name": "prometheus-agent",
  "description": "Queries Prometheus metrics, alerts, and performance data",
  "url": "https://prom-agent.internal:8080",
  "version": "1.0.0",
  "capabilities": {
    "streaming": true,
    "pushNotifications": false
  },
  "skills": [
    {
      "id": "query_metrics",
      "name": "Query Metrics",
      "description": "Execute PromQL queries and return time-series data",
      "tags": ["prometheus", "metrics", "monitoring", "cpu", "memory", "latency", "error_rate"],
      "examples": [
        "What is the CPU usage of the payments service?",
        "Show me the p99 latency for the API gateway"
      ]
    },
    {
      "id": "check_alerts",
      "name": "Check Alerts",
      "description": "List active and pending Prometheus alerts",
      "tags": ["alerts", "firing", "pending", "alertmanager"]
    }
  ]
}
```

**Why this matters:** When you add a new agent, the master agent discovers it automatically
by reading its Agent Card. No code changes needed in the router.

### 2. Tasks & Artifacts (A2A Communication)

**Task** = a request sent to an agent ("What's the CPU usage?")
**Artifact** = the result the agent sends back (the actual metric data)

```
Master Agent                          Prometheus Agent
     │                                      │
     │  POST /tasks/send                    │
     │  { message: "CPU usage of           │
     │    payments service?" }              │
     │─────────────────────────────────────▶│
     │                                      │
     │  Response:                           │
     │  { status: "completed",             │
     │    artifacts: [{                     │
     │      parts: [{ text: "CPU: 72%,     │
     │        trending up over last 2h" }] │
     │    }]                                │
     │  }                                   │
     │◀─────────────────────────────────────│
```

### 3. Temporal Workflow (The Glue)

Temporal wraps the entire conversation in a **durable workflow**:

```
Start Workflow
     │
     ▼
┌─── Loop ──────────────────────────────────┐
│                                           │
│  1. Receive user message (from Slack)     │
│  2. Route to agent (keyword → semantic    │
│     → LLM → ask user)                    │
│  3. Call agent via A2A (Temporal Activity) │
│  4. Post result to Slack                  │
│  5. Wait for Slack signal                 │
│     ├── User sends follow-up → go to 1   │
│     ├── User clicks Stop → exit loop      │
│     └── Timeout (1hr idle) → exit loop    │
│                                           │
└───────────────────────────────────────────┘
     │
     ▼
Workflow Complete
```

**Why Temporal?**
- If the service crashes at step 3, it **resumes from step 3** when it comes back — no lost context
- Each A2A call is a Temporal Activity with **automatic retries** and timeouts
- The "wait for Slack signal" can last **hours or days** without holding resources
- Full audit trail of every step in the conversation

### 4. Routing Pipeline (How The Right Agent Is Picked)

Queries flow through layers — fast/cheap first, slow/expensive last:

```
User Query: "Is the database connection pool exhausted?"
     │
     ▼
Layer 1: Keyword Match ──▶ Matches "database" → RDS? "connection pool" → Prom?
         (< 1ms, $0)       AMBIGUOUS — fall through
     │
     ▼
Layer 2: Semantic Match ──▶ Compare embeddings against Agent Card examples
         (~ 5ms, $0)       Best match: Prometheus (connection pool = metric)
         LOCAL model        Confidence: 0.88 → ROUTE ✅
     │
     │  (if confidence was low, would continue to...)
     ▼
Layer 3: LLM Classify  ──▶ Few-shot prompted classification
         (~ 500ms, ~$0.01)  with Agent Card context
     │
     ▼
Layer 4: Ask User       ──▶ Slack buttons: [Prometheus] [RDS] [K8s]
         (human speed, $0)   User picks → 100% accurate
```

Most queries resolve at Layer 1 or 2 — the LLM is only called for genuinely ambiguous ones.

---

## Components

```
a2a/
├── master/                  # Master agent (router + orchestrator)
│   ├── workflow.py          # Temporal workflow — the interactive loop
│   ├── router.py            # Multi-layer routing pipeline
│   ├── registry.py          # A2A Agent Card discovery & registry
│   └── slack_handler.py     # Slack bot — messages, buttons, signals
│
├── agents/                  # Specialized A2A agent servers
│   ├── prometheus/
│   │   ├── server.py        # A2A server + /tasks endpoints
│   │   ├── agent_card.json  # Agent Card (capabilities)
│   │   └── promql.py        # PromQL query builder & executor
│   │
│   ├── rds/
│   │   ├── server.py        # A2A server + /tasks endpoints
│   │   ├── agent_card.json  # Agent Card (capabilities)
│   │   └── sql_executor.py  # SQL query builder & executor (read-only)
│   │
│   └── kubernetes/
│       ├── server.py        # A2A server + /tasks endpoints
│       ├── agent_card.json  # Agent Card (capabilities)
│       └── kube_client.py   # Kubernetes API client
│
├── common/
│   ├── a2a_client.py        # A2A protocol client (send tasks, read artifacts)
│   ├── a2a_server.py        # A2A protocol server base class
│   ├── models.py            # Shared data models (Task, Artifact, AgentCard)
│   └── config.py            # Configuration & secrets management
│
├── docker-compose.yml       # Local dev: Temporal + all agents
├── requirements.txt         # Python dependencies
└── README.md                # This file
```

---

## Configuration

Each agent receives its connection details via environment variables:

```yaml
# docker-compose.yml (example)
services:
  prometheus-agent:
    environment:
      PROMETHEUS_URL: "http://prometheus:9090"
      A2A_PORT: 8080

  rds-agent:
    environment:
      RDS_HOST: "mydb.cluster-xyz.us-east-1.rds.amazonaws.com"
      RDS_PORT: 5432
      RDS_DATABASE: "production"
      RDS_USER: "readonly_user"
      RDS_PASSWORD_SECRET: "aws:secretsmanager:rds-password"
      A2A_PORT: 8081

  kubernetes-agent:
    environment:
      KUBECONFIG: "/etc/kube/config"
      K8S_CONTEXT: "production-cluster"
      A2A_PORT: 8082

  master-agent:
    environment:
      AGENT_URLS: "http://prometheus-agent:8080,http://rds-agent:8081,http://kubernetes-agent:8082"
      SLACK_BOT_TOKEN: "xoxb-..."
      SLACK_SIGNING_SECRET: "..."
      TEMPORAL_HOST: "temporal:7233"
      TEMPORAL_NAMESPACE: "default"
      OPENAI_API_KEY: "sk-..."  # For LLM routing (Layer 3 only)
```

---

## Request Flow (Step by Step)

Here's exactly what happens when you type a question in Slack:

```
1.  You type in Slack:  "Why is the payments pod restarting?"

2.  Slack Bot receives the message via Slack Events API

3.  Slack Bot starts a Temporal Workflow (or signals an existing one)
      → workflow_id = "slack-conv-{channel}-{thread_ts}"

4.  Temporal Workflow runs the routing pipeline:
      → Layer 1 (keywords): "pod" + "restarting" → K8s agent (confidence: 0.95) ✅

5.  Temporal executes an Activity: send A2A Task to K8s agent
      → POST http://k8s-agent:8082/tasks/send
      → { message: "Why is the payments pod restarting?" }

6.  K8s Agent:
      a. Runs: kubectl get pods | grep payments
      b. Finds: payments-7d4b8c-x2k9f  0/1  CrashLoopBackOff  12  45m
      c. Runs: kubectl logs payments-7d4b8c-x2k9f --previous
      d. Finds: "OOMKilled — container exceeded 512Mi memory limit"
      e. Returns A2A Artifact with formatted summary

7.  Temporal Workflow receives the Artifact

8.  Slack Bot posts to your thread:
      ┌──────────────────────────────────────────────┐
      │ ☸️ Kubernetes Agent                           │
      │                                              │
      │ Pod `payments-7d4b8c-x2k9f` is in           │
      │ CrashLoopBackOff (12 restarts in 45m)        │
      │                                              │
      │ Root cause: OOMKilled                        │
      │ Container exceeded 512Mi memory limit         │
      │                                              │
      │ Suggestion: Increase memory limit or check   │
      │ for memory leaks in the payments service     │
      │                                              │
      │ [Ask Follow-up]  [Check Metrics]  [Stop]     │
      └──────────────────────────────────────────────┘

9.  Temporal Workflow waits for your Slack signal...

10. You click "Check Metrics" or type "Show me memory usage for payments"
      → Loop back to step 4

11. You click "Stop"
      → Workflow completes, conversation archived
```

---

## Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Language | Python 3.11+ | Temporal SDK, rich async ecosystem |
| Agent Communication | A2A Protocol (HTTP+JSON) | Standardized discovery & interop |
| Orchestration | Temporal (`temporalio`) | Durable workflows, signals, retries |
| Slack Integration | `slack-bolt` | Official Slack SDK with event handling |
| Prometheus Queries | `prometheus-api-client` | PromQL execution |
| Database Queries | `sqlalchemy` + `asyncpg` | Async SQL with connection pooling |
| Kubernetes Access | `kubernetes` (official client) | Cluster inspection |
| LLM Routing | OpenAI / Anthropic API | Ambiguous query classification |
| Semantic Routing | `sentence-transformers` | Local embedding-based matching |
| Config | Pydantic Settings | Typed configuration with validation |
| Secrets | AWS Secrets Manager / Vault | Secure credential storage |

---

## Security

- All agents run with **read-only** access by default
- RDS agent uses a read-only database user — no INSERT/UPDATE/DELETE
- K8s agent uses RBAC with get/list permissions only — no create/delete
- SQL queries are validated against an allowlist before execution
- Agent-to-agent communication uses **bearer token** authentication
- All connection credentials stored in secrets manager, never in code/config files

---

## Adding a New Agent

1. Create a new A2A server with an Agent Card:

```json
{
  "name": "my-new-agent",
  "description": "Does something useful",
  "url": "https://my-agent.internal:8083",
  "skills": [
    {
      "id": "my_skill",
      "name": "My Skill",
      "description": "What this agent can do",
      "tags": ["relevant", "keywords"],
      "examples": ["Example question 1", "Example question 2"]
    }
  ]
}
```

2. Deploy it and add the URL to `AGENT_URLS`
3. The master agent discovers it on next restart — **no router code changes needed**

The routing pipeline automatically incorporates the new agent's tags and examples
into keyword matching, semantic matching, and LLM classification.

