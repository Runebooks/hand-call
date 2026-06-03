"""
LLM-backed intent + scope parsing for the Kubernetes agent.

Read-only by default. When K8S_MUTATIONS_ENABLED=true (staging), mutating
actions require yes/no confirmation before execution.
"""

from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from agents.kubernetes.kube_client import (
    infer_deployment_from_pod_name,
    parse_deployment_name,
    parse_namespace,
)
from agents.kubernetes.mutations import mutations_enabled
from common.llm import LLMClient

logger = logging.getLogger(__name__)

ReadAction = Literal[
    "list_pods",
    "problem_pods",
    "pod_logs",
    "deployments",
    "events",
]

MutateAction = Literal[
    "restart_pod",
    "delete_pod",
    "scale_deployment",
    "rollout_restart",
]

Action = Literal[
    "list_pods",
    "problem_pods",
    "pod_logs",
    "deployments",
    "events",
    "restart_pod",
    "delete_pod",
    "scale_deployment",
    "rollout_restart",
    "out_of_scope",
]

READ_SCOPE = """
IN SCOPE (read-only investigation):
- Pod status, CrashLoopBackOff diagnosis, logs, deployment replica info, warning events
"""

MUTATE_SCOPE = """
IN SCOPE (staging mutations — require user confirmation before run):
- restart_pod: restart a pod (rollout restart if Deployment-owned, else delete pod)
- delete_pod: delete a pod
- scale_deployment: change deployment replica count (set scale_replicas)
- rollout_restart: kubectl rollout restart on a deployment

NOT YET AUTOMATED (action=out_of_scope, explain + suggest kubectl):
- kubectl exec, apply, patch raw YAML, cordon/drain nodes
"""

READ_ONLY_SCOPE = """
OUT OF SCOPE (mutating — action=out_of_scope):
- restart, delete, scale, apply, patch, exec, rollout restart
- Set requested_operation and scope_reason; suggest kubectl for on-call
"""


def _system_prompt() -> str:
    if mutations_enabled():
        scope = READ_SCOPE + MUTATE_SCOPE
        actions = (
            "list_pods, problem_pods, pod_logs, deployments, events, "
            "restart_pod, delete_pod, scale_deployment, rollout_restart, out_of_scope"
        )
        extra = (
            "- 'can you restart this pod' → restart_pod (NOT problem_pods)\n"
            "- 'delete the pod' → delete_pod\n"
            "- 'scale to 3 replicas' → scale_deployment, scale_replicas=3\n"
            "- 'rollout restart deployment X' → rollout_restart\n"
        )
    else:
        scope = READ_SCOPE + READ_ONLY_SCOPE
        actions = (
            "list_pods, problem_pods, pod_logs, deployments, events, out_of_scope"
        )
        extra = (
            "- 'can you restart this pod' → out_of_scope, requested_operation=restart\n"
        )

    return f"""You parse Kubernetes NOC questions into JSON.

{scope}

Return a single JSON object:
- action: one of {actions}
- in_scope: boolean
- requested_operation: string or null
- scope_reason: string or null
- namespace: string or null
- pod: string or null
- deployment: string or null
- scale_replicas: integer or null
- previous_logs: boolean
- tail_lines: integer 20-500 (default 100)

Rules:
{extra}- 'what is the replica count' / 'how many replicas' → deployments (read-only, do NOT mutate)
- 'increase/set/scale replica count to N' → scale_deployment, scale_replicas=N
- Pod test-agent-workload-5675697b79-2xh2z → deployment test-agent-workload (strip ReplicaSet suffix)
- If user names deployment explicitly, set deployment field
- 'why is the pod restarting' → problem_pods (read-only diagnosis)
- 'get logs' → pod_logs
- Never set pod to the namespace value
- Output JSON only"""


class K8sIntent(BaseModel):
    action: Action
    in_scope: bool = True
    requested_operation: Optional[str] = None
    scope_reason: Optional[str] = None
    namespace: Optional[str] = None
    pod: Optional[str] = None
    deployment: Optional[str] = None
    scale_replicas: Optional[int] = None
    previous_logs: bool = False
    tail_lines: int = Field(default=100, ge=20, le=500)

    @property
    def is_mutating_action(self) -> bool:
        return self.action in (
            "restart_pod",
            "delete_pod",
            "scale_deployment",
            "rollout_restart",
        )

    @property
    def is_mutating_request(self) -> bool:
        if self.is_mutating_action:
            return True
        return self.action == "out_of_scope" or not self.in_scope


_MUTATING_PATTERNS: list[tuple[str, str]] = [
    (r"\bscale\s+(?:up|down)\b.*\b(?:deployment|deploy)\b", "scale_deployment"),
    (r"\bscale\s+(?:up|down)\b.*?\bto\s+\d+\b", "scale_deployment"),
    (r"\b(increase|decrease|set|change|update)\b.*\breplica", "scale_deployment"),
    (r"\breplica\s+count\s+to\s+\d+\b", "scale_deployment"),
    (r"\b(can you|please|could you)\s+restart\b", "restart_pod"),
    (r"\brestart\s+(the|this|my)?\s*pod\b", "restart_pod"),
    (r"\brollout\s+restart\b", "rollout_restart"),
    (r"\b(delete|remove|kill|terminate)\s+(the|this|my)?\s*pod\b", "delete_pod"),
    (r"\bscale\s+(?:the|this|my)?\s*(?:deployment|deploy)\b.*\bto\s+\d+\b", "scale_deployment"),
    (r"\bscale\s+to\s+\d+\b", "scale_deployment"),
]


def _is_scale_mutation_phrasing(text: str) -> bool:
    return bool(
        re.search(
            r"\bscale\s+(?:up|down)\b|"
            r"\b("
            r"increase|decrease|scale|set|change|update"
            r")\b.*\b(replica|replicas)\b|"
            r"\bscale\s+.*?\bto\s+\d+\b|"
            r"\breplica\s+count\s+to\s+\d+\b|"
            r"\breplicas?\s+to\s+\d+\b",
            text,
            re.I,
        )
    )


def _is_read_replica_phrasing(text: str) -> bool:
    return bool(
        re.search(
            r"\b(what|how many|show|get|current|tell me)\b.*\breplica|"
            r"\breplica\s+count\b(?!\s+to\s+\d)",
            text,
            re.I,
        )
    )


def wants_scale_mutation(text: str) -> bool:
    """User wants to change replica count (not just read it)."""
    return _is_scale_mutation_phrasing(text)


def wants_read_replica_query(text: str) -> bool:
    """Read-only: what is the current replica count."""
    if _is_scale_mutation_phrasing(text):
        return False
    return _is_read_replica_phrasing(text)


def _is_read_crash_diagnosis(text: str) -> bool:
    if detect_mutating_operation(text):
        return False
    return bool(
        re.search(
            r"\b(why|reason|cause)\b.*\b(crash|crashloop|restart|fail)",
            text,
            re.I,
        )
        or re.search(
            r"\b(crash|crashloop|restarting)\b.*\b(reason|cause|why)\b",
            text,
            re.I,
        )
    )


def wants_deployment_name_query(text: str) -> bool:
    """Read-only: which deployment owns this pod."""
    if _is_scale_mutation_phrasing(text):
        return False
    return bool(
        re.search(
            r"\bdeployment\s+name\b|"
            r"\bname\s+of\s+(?:the\s+|this\s+)?deployment\b|"
            r"\bwhich\s+deployment\b|"
            r"\bwhat\s+deployment\b",
            text,
            re.I,
        )
    )


def wants_deployment_pods(text: str) -> bool:
    """List/describe all pods belonging to a deployment."""
    if _is_scale_mutation_phrasing(text):
        return False
    if wants_deployment_name_query(text):
        return False
    return bool(
        re.search(
            r"\b(all|every|each)\s+pods?\b.*\b(deployment|deploy)\b|"
            r"\bpods?\b.*\b(in|for|of)\s+(?:this\s+)?(?:the\s+)?(deployment|deploy)\b|"
            r"\bstatus\b.*\bpods?\b.*\b(?:in\s+)?(?:this\s+)?(?:the\s+)?(deployment|deploy)\b|"
            r"\bpods?\b.*\b(?:in\s+)?this\s+deployment\b",
            text,
            re.I,
        )
    )


def parse_scale_replicas(text: str) -> Optional[int]:
    patterns = [
        r"\bscale\s+(?:up|down)\s+(?:the\s+)?deployment\s+to\s+(\d+)\b",
        r"\bscale\s+(?:up|down)\s+(?:the\s+)?(?:deployment\s+[\w-]+\s+)?to\s+(\d+)\b",
        r"\bscale\s+(?:up|down)\s+(?:the\s+)?(?:deployment\s+[\w-]+\s+)?(?:replicas?\s+)?to\s+(\d+)\b",
        r"\breplica\s+count\b.*?\bto\s+(\d+)\b",
        r"\bscale\s+to\s+(\d+)\b",
        r"\b(?:increase|decrease|set|change|update)\b.*?\bto\s+(\d+)\b",
        r"\breplica\s+count\s+to\s+(\d+)\b",
        r"\bfor\s+(?:the\s+)?deployment\b.*?\bto\s+(\d+)\b",
        r"\b(\d+)\s+replicas?\b",
        r"\breplicas?\s+to\s+(\d+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1))
    return None


def apply_intent_context(
    intent: K8sIntent,
    query: str,
    *,
    namespace: Optional[str] = None,
    pod: Optional[str] = None,
    kube: Optional["KubeClient"] = None,
) -> K8sIntent:
    """Enrich intent from Slack metadata, pod→deployment inference, and phrasing."""
    user = _user_question(query)
    p = intent.pod or pod
    ns = intent.namespace or namespace

    if ns:
        intent.namespace = ns.lower()
    if p:
        intent.pod = p

    explicit_dep = parse_deployment_name(user, pod=p)
    if explicit_dep:
        intent.deployment = explicit_dep
    elif p and not intent.deployment:
        intent.deployment = infer_deployment_from_pod_name(p)

    if kube and p and ns and not intent.deployment:
        try:
            workload = kube.get_pod_workload(ns, p)
            if workload.get("deployment"):
                intent.deployment = workload["deployment"]
        except Exception:
            pass

    if wants_scale_mutation(user):
        intent.action = "scale_deployment"
        intent.in_scope = True
        if intent.scale_replicas is None:
            intent.scale_replicas = parse_scale_replicas(user)
    elif detect_mutating_operation(user) and mutations_enabled():
        op = detect_mutating_operation(user)
        if op and op != "blocked":
            intent.action = op  # type: ignore[assignment]
            intent.in_scope = True
            if op == "scale_deployment" and intent.scale_replicas is None:
                intent.scale_replicas = parse_scale_replicas(user)
    elif wants_deployment_pods(user):
        intent.action = "list_pods"
        if not intent.deployment:
            intent.deployment = (
                parse_deployment_name(user, pod=p)
                or (infer_deployment_from_pod_name(p) if p else None)
            )
    elif wants_deployment_name_query(user):
        intent.action = "deployments"
        if not intent.deployment and p:
            intent.deployment = infer_deployment_from_pod_name(p)
    elif wants_read_replica_query(user) and not intent.is_mutating_action:
        intent.action = "deployments"
    elif _is_read_crash_diagnosis(user) and not intent.is_mutating_action:
        intent.action = "problem_pods"

    return intent


def detect_mutating_operation(text: str) -> Optional[str]:
    """Heuristic mutating intent when LLM is disabled."""
    if not mutations_enabled():
        lowered = text.lower()
        for pattern, _op in [
            (r"\b(restart|reboot|recycle)\b", "restart"),
            (r"\b(delete|remove|kill)\b", "delete"),
            (r"\bscale\b", "scale"),
        ]:
            if re.search(pattern, lowered):
                if re.search(
                    r"\b(why|restarting|restart\s+count|restarts?|crashloop)\b",
                    lowered,
                ) and not re.search(
                    r"\b(can you|please|restart (the|this|my)?\s*pod)\b", lowered
                ):
                    continue
                return "blocked"
        return None

    lowered = text.lower()
    for pattern, action in _MUTATING_PATTERNS:
        if re.search(pattern, lowered):
            if action == "restart_pod" and re.search(
                r"\b(why|restarting|restart\s+count)\b", lowered
            ):
                if not re.search(r"\b(can you|please|restart (the|this|my)?\s*pod)\b", lowered):
                    continue
            return action
    return None


def parse_intent_with_llm(
    query: str,
    client: Optional[LLMClient] = None,
    *,
    namespace: Optional[str] = None,
    pod: Optional[str] = None,
) -> K8sIntent:
    llm = client or LLMClient()
    meta = ""
    if namespace or pod:
        meta = f"\nSlack/NOC metadata: namespace={namespace or 'null'}, pod={pod or 'null'}"
    raw = llm.complete_json(
        system=_system_prompt(),
        user=f"Question: {query}{meta}",
    )
    intent = K8sIntent.model_validate(raw)
    if intent.namespace:
        intent.namespace = intent.namespace.lower()
    elif namespace:
        intent.namespace = namespace.lower()
    elif ns := parse_namespace(query):
        intent.namespace = ns
    if pod and not intent.pod:
        intent.pod = pod
    if intent.action == "scale_deployment" and intent.scale_replicas is None:
        intent.scale_replicas = parse_scale_replicas(_user_question(query))
    intent = apply_intent_context(
        intent, query, namespace=intent.namespace, pod=intent.pod
    )
    logger.info(
        "LLM intent: action=%s deployment=%s replicas=%s namespace=%s pod=%s",
        intent.action,
        intent.deployment,
        intent.scale_replicas,
        intent.namespace,
        intent.pod,
    )
    return intent


def _user_question(query: str) -> str:
    marker = "\n\nKubernetes context:"
    if marker in query:
        return query.split(marker, 1)[0].strip()
    return query.strip()


def normalize_confirmation(text: str) -> str:
    """Strip @mention leftovers and punctuation so ', yes' and 'Yes.' match."""
    t = (text or "").strip().lower()
    t = re.sub(r"^[,\s:;]+", "", t)
    t = re.sub(r"[.!?,]+$", "", t)
    return t.strip()


def is_confirmation_yes(text: str) -> bool:
    t = normalize_confirmation(text)
    if not t:
        return False
    if t in ("yes", "y", "confirm", "confirmed", "proceed", "go", "go ahead", "do it", "ok", "okay"):
        return True
    return bool(re.fullmatch(r"(yes|y|ok|okay|confirm(ed)?|proceed|go ahead|do it)([.!])?", t))


def is_confirmation_no(text: str) -> bool:
    t = normalize_confirmation(text)
    if not t:
        return False
    if t in ("no", "n", "cancel", "cancelled", "abort", "stop", "don't", "dont"):
        return True
    return bool(re.fullmatch(r"(no|n|cancel(led)?|abort|stop)([.!])?", t))
