"""Multi-layer agent routing (README) — no Temporal."""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from common.llm import LLMClient
from master.alert_parser import AlertContext, K8S_ALERT_HANDLERS, is_metrics_alert
from master.registry import AgentRegistry, RegisteredAgent

logger = logging.getLogger(__name__)

# Layer 0 — user question intent (beats alertname for follow-ups)
_USER_K8S = re.compile(
    r"\b(pod|pods|crash|crashloop|crashloopbackoff|restarting|log|logs|deployment|"
    r"replica|restart|scale|delete|namespace|kube|k8s|container)\b",
    re.I,
)
_USER_PROM = re.compile(
    r"\b(rpm|metric|metrics|prometheus|promql|current.?value|threshold|latency|"
    r"cpu|memory|haystack|dashboard|spike|rate|trigmetry|fetch|current|"
    r"targets?|scrape|scraped|exporter|series|alertmanager|firing)\b"
    r"|up\s*==|up\s*\{|\bup\s*=\s*0\b",
    re.I,
)
_EXPLICIT_PROMQL = re.compile(r"(?:promql|query)\s*[:=]", re.I)

# Layer 0 — Freshservice / incident intent (highest priority, beats K8s/Prometheus)
# Matches explicit MIM ticket references and strong NOC incident signals.
_USER_FS = re.compile(
    r"\bMI-\d+\b"
    r"|\b(incident|incidents|mim|outage|outages|freshservice|freshstatus|"
    r"handover|status\s*page|zoom\s*link|mttr|mttd|briefing|escalat|"
    r"ticket|tickets|mim\s*ticket|incident\s*report|noc\s*report)\b",
    re.I,
)

# Alertname → preferred agent (Layer 1)
_ALERTNAME_AGENT: dict[str, str] = {
    name: "kubernetes-agent" for name in K8S_ALERT_HANDLERS
}
_ALERTNAME_AGENT.update(
    {
        "KubePodCrashLooping": "kubernetes-agent",
        "KubePodNotReady": "kubernetes-agent",
        "KubeDeploymentReplicasMismatch": "kubernetes-agent",
    }
)

# Keywords → agent (Layer 2)
_KEYWORD_AGENTS: list[tuple[str, str]] = [
    ("freshservice-agent", r"\bMI-\d+\b|\b(incident|incidents|mim|outage|outages|freshservice|freshstatus|mttr|mttd|briefing|escalat|ticket|tickets)\b"),
    ("kubernetes-agent", r"\b(pod|pods|namespace|crashloop|deployment|kube|k8s|container)\b"),
    ("prometheus-agent", r"\b(prometheus|promql|metric|metrics|rpm|latency|cpu|memory|haystack|dashboard|targets?|scrape|exporter|series|alertmanager|firing)\b|up\s*==|up\s*\{"),
    ("rds-agent", r"\b(rds|database|db|sql|postgres|mysql|slow\s+query|connection\s+pool)\b"),
]


class AgentRouter:
    def __init__(self, registry: AgentRegistry, llm: Optional[LLMClient] = None):
        self.registry = registry
        self.llm = llm or LLMClient()

    def route(
        self,
        query: str,
        alert: Optional[AlertContext] = None,
    ) -> RegisteredAgent:
        agents = self.registry.agents
        if not agents:
            raise RuntimeError(
                "No agents registered. Set AGENT_URLS or start specialist agents."
            )

        chosen = self._route_user_intent(query, agents)
        if chosen:
            logger.info("Route L0 user-intent → %s", chosen.name)
            return chosen

        chosen = self._route_metrics_alert(alert, agents)
        if chosen:
            logger.info("Route L1 metrics-alert → %s", chosen.name)
            return chosen

        chosen = self._route_alertname(query, alert, agents)
        if chosen:
            logger.info("Route L2 alertname → %s", chosen.name)
            return chosen

        chosen = self._route_keywords(query, agents)
        if chosen:
            logger.info("Route L3 keywords → %s", chosen.name)
            return chosen

        if self.llm.enabled() and os.environ.get("ENABLE_LLM_ROUTING", "true").lower() not in (
            "0",
            "false",
            "no",
        ):
            chosen = self._route_llm(query, agents, alert)
            if chosen:
                logger.info("Route L4 LLM → %s", chosen.name)
                return chosen

        # Default: K8s for NOC Kube alerts, else first agent
        if alert and (alert.alertname or "").startswith("Kube"):
            chosen = agents.get("kubernetes-agent")
            if chosen:
                return chosen
        return next(iter(agents.values()))

    @staticmethod
    def _user_question(query: str) -> str:
        marker = "\n\nKubernetes context:"
        if marker in query:
            return query.split(marker, 1)[0].strip()
        return query.strip()

    def _route_user_intent(
        self, query: str, agents: dict[str, RegisteredAgent]
    ) -> Optional[RegisteredAgent]:
        """Follow-up questions: 'why crashing' → k8s, 'what is RPM' → prometheus, 'MI-...' → freshservice."""
        user = self._user_question(query)
        if not user:
            return None
        # Freshservice / incident questions always win at L0 for explicit signals
        if _USER_FS.search(user) and "freshservice-agent" in agents:
            logger.debug("User-intent freshservice signal → freshservice-agent")
            return agents["freshservice-agent"]
        if _EXPLICIT_PROMQL.search(user) and "prometheus-agent" in agents:
            logger.debug("User-intent explicit promql → prometheus-agent")
            return agents["prometheus-agent"]
        # Do not treat label names inside PromQL (e.g. namespace=) as k8s intent
        intent_text = _EXPLICIT_PROMQL.sub("", user).strip() or user
        prom_hits = len(_USER_PROM.findall(intent_text))
        k8s_hits = len(_USER_K8S.findall(intent_text))
        logger.debug(
            "User-intent scores prom=%s k8s=%s question=%r",
            prom_hits,
            k8s_hits,
            user[:120],
        )
        # Metric/RPM questions always win unless user explicitly asks k8s ops
        if prom_hits and k8s_hits == 0 and "prometheus-agent" in agents:
            return agents["prometheus-agent"]
        if k8s_hits and prom_hits == 0 and "kubernetes-agent" in agents:
            return agents["kubernetes-agent"]
        if prom_hits >= k8s_hits and prom_hits > 0 and "prometheus-agent" in agents:
            return agents["prometheus-agent"]
        if k8s_hits > prom_hits and "kubernetes-agent" in agents:
            return agents["kubernetes-agent"]
        return None

    def _route_metrics_alert(
        self, alert: Optional[AlertContext], agents: dict[str, RegisteredAgent]
    ) -> Optional[RegisteredAgent]:
        """Pure Trigmetry metric alerts without a Kubernetes pod target."""
        if not alert or not is_metrics_alert(alert):
            return None
        if alert.pod_hint or (alert.namespace and alert.alert_sre_attributes):
            return None
        if "prometheus-agent" in agents:
            return agents["prometheus-agent"]
        return None

    def _route_alertname(
        self,
        query: str,
        alert: Optional[AlertContext],
        agents: dict[str, RegisteredAgent],
    ) -> Optional[RegisteredAgent]:
        if not alert or not alert.alertname:
            return None
        user = self._user_question(query)
        if user and _USER_PROM.search(user) and "prometheus-agent" in agents:
            if not _EXPLICIT_PROMQL.search(user) or _USER_PROM.search(
                _EXPLICIT_PROMQL.sub("", user)
            ):
                return agents["prometheus-agent"]
        name = _ALERTNAME_AGENT.get(alert.alertname)
        if not name and alert.alertname.startswith("Kube"):
            name = "kubernetes-agent"
        if not name:
            summary = (alert.summary or alert.raw_text or "").lower()
            if "rpm" in summary or "metric" in summary:
                name = "prometheus-agent"
            elif re.search(r"\b(db|rds|database|sql)\b", summary):
                name = "rds-agent"
        if name and name in agents:
            return agents[name]
        return None

    def _route_keywords(
        self, query: str, agents: dict[str, RegisteredAgent]
    ) -> Optional[RegisteredAgent]:
        text = query.lower()
        scores: dict[str, int] = {}
        for agent_name, pattern in _KEYWORD_AGENTS:
            if agent_name not in agents:
                continue
            scores[agent_name] = len(re.findall(pattern, text, re.I))
        if not scores or max(scores.values()) == 0:
            return None
        best = max(scores, key=scores.get)
        return agents[best]

    def _route_llm(
        self,
        query: str,
        agents: dict[str, RegisteredAgent],
        alert: Optional[AlertContext],
    ) -> Optional[RegisteredAgent]:
        catalog = []
        for a in agents.values():
            catalog.append(
                f"- {a.name}: {a.card.description}; tags={', '.join(sorted(a.tags)[:20])}"
            )
        alert_line = ""
        if alert:
            alert_line = (
                f"Alert context: alertname={alert.alertname}, "
                f"namespace={alert.namespace}, product={alert.product}"
            )
        system = (
            "Pick the best agent name for this ops question. "
            "Reply JSON only: {\"agent\": \"<exact agent name>\"}. "
            "Choices: " + ", ".join(agents.keys())
        )
        try:
            raw = self.llm.complete_json(
                system=system,
                user=f"{alert_line}\nQuestion: {query}\n\nAgents:\n" + "\n".join(catalog),
            )
            pick = (raw.get("agent") or "").strip()
            return agents.get(pick)
        except Exception as exc:
            logger.warning("LLM routing failed: %s", exc)
            return None
