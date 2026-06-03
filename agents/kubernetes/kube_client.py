"""
Read-only Kubernetes API client for the A2A kubernetes agent.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)


def coerce_log_text(logs: Union[str, bytes, None]) -> str:
    """Normalize pod log API responses to plain text."""
    if logs is None:
        return ""
    if isinstance(logs, bytes):
        return logs.decode("utf-8", errors="replace")
    text = str(logs)
    stripped = text.strip()
    if (
        (stripped.startswith("b'") and stripped.endswith("'"))
        or (stripped.startswith('b"') and stripped.endswith('"'))
    ):
        try:
            value = ast.literal_eval(stripped)
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
        except (SyntaxError, ValueError):
            pass
    return text


@dataclass
class PodSummary:
    namespace: str
    name: str
    ready: str
    status: str
    restarts: int
    age: str
    reason: Optional[str] = None
    message: Optional[str] = None


class KubeClient:
    """Thin wrapper around the official Kubernetes Python client."""

    def __init__(self, context: Optional[str] = None):
        self._context = context or os.environ.get("K8S_CONTEXT")
        self._core: Optional[client.CoreV1Api] = None
        self._apps: Optional[client.AppsV1Api] = None

    def connect(self) -> None:
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster kubeconfig")
        except config.ConfigException:
            if self._context:
                config.load_kube_config(context=self._context)
            else:
                config.load_kube_config()
            logger.info("Loaded kubeconfig from file (context=%s)", self._context or "default")

        self._core = client.CoreV1Api()
        self._apps = client.AppsV1Api()

    @property
    def core(self) -> client.CoreV1Api:
        if self._core is None:
            raise RuntimeError("KubeClient not connected — call connect() first")
        return self._core

    @property
    def apps(self) -> client.AppsV1Api:
        if self._apps is None:
            raise RuntimeError("KubeClient not connected — call connect() first")
        return self._apps

    def list_namespaces(self) -> list[str]:
        ns_list = self.core.list_namespace()
        return sorted(item.metadata.name for item in ns_list.items)

    def list_pods(
        self,
        namespace: Optional[str] = None,
        name_contains: Optional[str] = None,
        field_selector: Optional[str] = None,
        limit: int = 50,
    ) -> list[PodSummary]:
        if namespace:
            pod_list = self.core.list_namespaced_pod(
                namespace=namespace,
                field_selector=field_selector,
                limit=limit,
            )
            namespaces = [namespace]
        else:
            pod_list = self.core.list_pod_for_all_namespaces(
                field_selector=field_selector,
                limit=limit,
            )
            namespaces = []

        summaries: list[PodSummary] = []
        for pod in pod_list.items:
            ns = pod.metadata.namespace or "default"
            name = pod.metadata.name or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue

            ready = "0/0"
            restarts = 0
            if pod.status and pod.status.container_statuses:
                total = len(pod.status.container_statuses)
                ready_count = sum(1 for c in pod.status.container_statuses if c.ready)
                ready = f"{ready_count}/{total}"
                restarts = sum(c.restart_count for c in pod.status.container_statuses)

            phase = pod.status.phase if pod.status else "Unknown"
            reason = None
            message = None
            if pod.status and pod.status.container_statuses:
                for cs in pod.status.container_statuses:
                    if cs.state and cs.state.waiting:
                        reason = cs.state.waiting.reason
                        message = cs.state.waiting.message
                        break
                    if cs.last_state and cs.last_state.terminated:
                        reason = cs.last_state.terminated.reason
                        message = cs.last_state.terminated.message
                        break

            age = _format_age(pod.metadata.creation_timestamp)
            summaries.append(
                PodSummary(
                    namespace=ns,
                    name=name,
                    ready=ready,
                    status=phase if not reason else f"{phase} ({reason})",
                    restarts=restarts,
                    age=age,
                    reason=reason,
                    message=message,
                )
            )

        summaries.sort(key=lambda p: (p.namespace, p.name))
        return summaries[:limit]

    def get_pod_logs(
        self,
        namespace: str,
        pod_name: str,
        container: Optional[str] = None,
        previous: bool = False,
        tail_lines: int = 100,
    ) -> str:
        try:
            logs = self.core.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                container=container,
                previous=previous,
                tail_lines=tail_lines,
            )
            return coerce_log_text(logs)
        except ApiException as e:
            if e.status == 400 and previous:
                return "(no previous container logs available)"
            raise

    def find_pod(self, name_hint: str, namespace: Optional[str] = None) -> Optional[PodSummary]:
        hint = name_hint.strip().lower()
        pods = self.list_pods(namespace=namespace, name_contains=hint, limit=200)
        if not pods:
            pods = self.list_pods(namespace=namespace, limit=200)
            pods = [p for p in pods if hint in p.name.lower()]

        if not pods:
            return None
        pods.sort(key=lambda p: (-p.restarts, p.name))
        return pods[0]

    def get_pod_workload(self, namespace: str, pod_name: str) -> dict:
        """
        Resolve whether a pod is standalone or owned by ReplicaSet/Deployment.
        Returns replica info when a Deployment manages the pod.
        """
        pod_obj = self.core.read_namespaced_pod(name=pod_name, namespace=namespace)
        owner_refs = pod_obj.metadata.owner_references or []

        if not owner_refs:
            summary = self.find_pod(pod_name, namespace=namespace)
            restarts = summary.restarts if summary else 0
            ready = summary.ready if summary else "?"
            status = summary.status if summary else "?"
            return {
                "standalone": True,
                "pod": pod_name,
                "namespace": namespace,
                "restarts": restarts,
                "ready": ready,
                "status": status,
            }

        for ref in owner_refs:
            kind = (ref.kind or "").lower()
            name = ref.name or ""
            if kind == "replicaset":
                try:
                    rs = self.apps.read_namespaced_replica_set(
                        name=name, namespace=namespace
                    )
                    rs_owners = rs.metadata.owner_references or []
                    dep_name = None
                    for rs_ref in rs_owners:
                        if (rs_ref.kind or "").lower() == "deployment":
                            dep_name = rs_ref.name
                            break
                    if dep_name:
                        dep = self.apps.read_namespaced_deployment(
                            name=dep_name, namespace=namespace
                        )
                        spec_replicas = dep.spec.replicas if dep.spec else 0
                        ready = dep.status.ready_replicas if dep.status else 0
                        available = (
                            dep.status.available_replicas if dep.status else 0
                        )
                        return {
                            "standalone": False,
                            "pod": pod_name,
                            "namespace": namespace,
                            "deployment": dep_name,
                            "ready": f"{ready or 0}/{spec_replicas or 0}",
                            "available": available or 0,
                            "spec_replicas": spec_replicas or 0,
                        }
                except ApiException:
                    pass
            if kind == "job":
                return {
                    "standalone": False,
                    "pod": pod_name,
                    "namespace": namespace,
                    "job": name,
                    "message": "Pod is owned by a Job (not a Deployment replica set).",
                }

        summary = self.find_pod(pod_name, namespace=namespace)
        return {
            "standalone": True,
            "pod": pod_name,
            "namespace": namespace,
            "restarts": summary.restarts if summary else 0,
            "ready": summary.ready if summary else "?",
            "status": summary.status if summary else "?",
            "owner_kinds": [ref.kind for ref in owner_refs],
        }

    def list_deployments(
        self,
        namespace: Optional[str] = None,
        name_contains: Optional[str] = None,
        limit: int = 30,
    ) -> list[dict]:
        if namespace:
            dep_list = self.apps.list_namespaced_deployment(namespace=namespace, limit=limit)
            items = dep_list.items
        else:
            dep_list = self.apps.list_deployment_for_all_namespaces(limit=limit)
            items = dep_list.items

        results = []
        for dep in items:
            name = dep.metadata.name or ""
            if name_contains and name_contains.lower() not in name.lower():
                continue
            spec_replicas = dep.spec.replicas if dep.spec else 0
            ready = 0
            if dep.status:
                ready = dep.status.ready_replicas or 0
            results.append(
                {
                    "namespace": dep.metadata.namespace,
                    "name": name,
                    "ready": f"{ready}/{spec_replicas}",
                    "available": dep.status.available_replicas if dep.status else 0,
                    "conditions": [
                        f"{c.type}={c.status}"
                        for c in (dep.status.conditions or [])
                    ]
                    if dep.status
                    else [],
                }
            )
        return results[:limit]

    def list_pods_for_deployment(
        self, namespace: str, deployment_name: str, limit: int = 50
    ) -> list[PodSummary]:
        """List pods for a deployment (label selector, then name prefix)."""
        try:
            dep = self.apps.read_namespaced_deployment(
                name=deployment_name, namespace=namespace
            )
            selector = (
                dep.spec.selector.match_labels
                if dep.spec and dep.spec.selector
                else {}
            )
            if selector:
                label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
                pod_list = self.core.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=label_selector,
                    limit=limit,
                )
                names = [p.metadata.name for p in pod_list.items if p.metadata.name]
                if names:
                    all_pods = self.list_pods(namespace=namespace, limit=200)
                    name_set = set(names)
                    matched = [p for p in all_pods if p.name in name_set]
                    matched.sort(key=lambda p: p.name)
                    return matched[:limit]
        except ApiException:
            pass

        prefix = f"{deployment_name}-"
        all_pods = self.list_pods(namespace=namespace, limit=200)
        matched = [p for p in all_pods if p.name.startswith(prefix)]
        matched.sort(key=lambda p: p.name)
        return matched[:limit]

    def delete_pod(self, namespace: str, pod_name: str) -> None:
        self.core.delete_namespaced_pod(name=pod_name, namespace=namespace)
        logger.info("Deleted pod %s/%s", namespace, pod_name)

    def rollout_restart_deployment(self, namespace: str, deployment_name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now,
                        }
                    }
                }
            }
        }
        self.apps.patch_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body=patch,
        )
        logger.info("Rollout restart %s/%s", namespace, deployment_name)

    def scale_deployment(
        self, namespace: str, deployment_name: str, replicas: int
    ) -> None:
        patch = {"spec": {"replicas": replicas}}
        self.apps.patch_namespaced_deployment(
            name=deployment_name,
            namespace=namespace,
            body=patch,
        )
        logger.info(
            "Scaled deployment %s/%s to %s replicas",
            namespace,
            deployment_name,
            replicas,
        )

    def list_warning_events(
        self,
        namespace: Optional[str] = None,
        involved_name: Optional[str] = None,
        limit: int = 25,
    ) -> list[dict]:
        if namespace:
            event_list = self.core.list_namespaced_event(namespace=namespace, limit=200)
            items = event_list.items
        else:
            event_list = self.core.list_event_for_all_namespaces(limit=200)
            items = event_list.items

        events = []
        for ev in items:
            if ev.type and ev.type.lower() != "warning":
                continue
            obj_name = ""
            if ev.involved_object:
                obj_name = ev.involved_object.name or ""
            if involved_name and involved_name.lower() not in obj_name.lower():
                continue
            events.append(
                {
                    "namespace": ev.metadata.namespace,
                    "object": f"{ev.involved_object.kind}/{obj_name}"
                    if ev.involved_object
                    else "unknown",
                    "reason": ev.reason,
                    "message": (ev.message or "")[:200],
                    "count": ev.count,
                    "last": _format_age(ev.last_timestamp or ev.event_time),
                }
            )

        events.sort(key=lambda e: e.get("count", 0), reverse=True)
        return events[:limit]


def _format_age(timestamp) -> str:
    if not timestamp:
        return "?"
    from datetime import datetime, timezone

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - timestamp
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def parse_slack_alert_hints(text: str) -> tuple[Optional[str], Optional[str]]:
    """Parse [noc-alert] pod=... namespace=... or alert_sre_attributes: from Slack body."""
    pod = None
    ns = None
    block = re.search(
        r"\[noc-alert\]\s*pod=([a-z0-9][-a-z0-9]*)\s+namespace=([a-z0-9][-a-z0-9]*)",
        text,
        re.IGNORECASE,
    )
    if block:
        pod, ns = block.group(1).lower(), block.group(2).lower()
        return ns, pod
    sre = re.search(
        r"alert_sre_attributes[`'\"]?\s*:\s*([a-z0-9][-a-z0-9]*)",
        text,
        re.IGNORECASE,
    )
    if sre:
        pod = sre.group(1).lower()
    ns = parse_namespace(text)
    return ns, pod


def parse_namespace(text: str) -> Optional[str]:
    """Extract a namespace hint. K8s names are lowercase DNS labels only."""
    patterns = [
        r"namespace\s+[`'\"]?([a-z][-a-z0-9]*)[`'\"]?",
        r"in\s+the\s+[`'\"]?([a-z][-a-z0-9]*)[`'\"]?\s+namespace",
        r"in\s+namespace\s+[`'\"]?([a-z][-a-z0-9]*)[`'\"]?",
        r"-n\s+([a-z][-a-z0-9]*)",
        r"namespace:\s*([a-z][-a-z0-9]*)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return None


def parse_name_hint(text: str, namespace: Optional[str] = None) -> Optional[str]:
    explicit = re.search(
        r"(?:pod|target\s+pod)[:\s]+[`'\"]?([a-z0-9][-a-z0-9]*)[`'\"]?",
        text,
        re.IGNORECASE,
    )
    if not explicit:
        explicit = re.search(r"pod\s+`([a-z0-9][-a-z0-9]*)`", text, re.IGNORECASE)
    if explicit:
        name = explicit.group(1).lower()
        if not namespace or name != namespace.lower():
            return name

    sre = re.search(
        r"alert_sre_attributes[`'\"]?\s*:\s*([a-z0-9][-a-z0-9]*)",
        text,
        re.IGNORECASE,
    )
    if sre:
        return sre.group(1).lower()

    quoted = re.findall(r"[`'\"]([^`'\"]+)[`'\"]", text)
    if quoted:
        for candidate in reversed(quoted):
            c = candidate.lower()
            if namespace and c == namespace.lower():
                continue
            if c not in ("firing", "critical") and "-" in c:
                return candidate
    ns = namespace or parse_namespace(text)
    tokens = re.findall(r"[a-z0-9][-a-z0-9]{2,}", text.lower())
    stop = {
        "kubernetes", "cluster", "namespace", "show", "list", "what", "why",
        "pods", "pod", "logs", "log", "deployment", "deployments", "events",
        "status", "recent", "warning", "crashloopbackoff", "crashloop", "backoff",
        "restarting", "the", "for", "in", "is", "are", "get", "me", "all",
        "that", "or", "and", "with", "any", "have", "has", "been", "which",
        "investigate", "firing", "kubepodcrashlooping", "context", "alert",
        "use", "from", "attributes", "sre", "reason", "cause",
    }
    for token in reversed(tokens):
        if ns and token == ns:
            continue
        if token not in stop and len(token) > 2:
            return token
    return None


def infer_deployment_from_pod_name(pod_name: str) -> Optional[str]:
    """
    Derive deployment name from pod name.
    e.g. test-agent-workload-5675697b79-2xh2z → test-agent-workload
    """
    if not pod_name:
        return None
    pod = pod_name.strip().lower()
    match = re.match(
        r"^(?P<dep>.+)-[a-f0-9]{8,10}-[a-z0-9]{4,6}$",
        pod,
    )
    if match:
        return match.group("dep")
    return None


_DEPLOYMENT_NAME_STOP = frozenset(
    {"to", "up", "down", "the", "is", "name", "this", "my", "a", "an", "for", "in"}
)


def parse_deployment_name(text: str, pod: Optional[str] = None) -> Optional[str]:
    """Explicit deployment name in user text, else infer from pod."""
    patterns: list[tuple[str, bool]] = [
        (r"\bdeployment\s+name\s+is\s+[`'\"]?([a-z0-9][-a-z0-9]{2,})[`'\"]?", True),
        (
            r"\bfor\s+(?:the\s+|this\s+)?deployment\s+[`'\"]?([a-z0-9][-a-z0-9]{2,})[`'\"]?",
            True,
        ),
        (
            r"\b(?:scale|increase|decrease|set)\b.*?\bfor\s+(?:the\s+)?deployment\s+[`'\"]?([a-z0-9][-a-z0-9]{2,})[`'\"]?",
            True,
        ),
        (
            r"\bdeployment\s+[`'\"]?([a-z0-9][-a-z0-9]{2,})[`'\"]?\s+in\s+(?:the\s+)?namespace\b",
            True,
        ),
        (r"\bdeploy\s+[`'\"]?([a-z0-9][-a-z0-9]{2,})[`'\"]?", False),
    ]
    for pattern, explicit in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            name = match.group(1).lower()
            if name in _DEPLOYMENT_NAME_STOP:
                continue
            if not explicit:
                inferred = infer_deployment_from_pod_name(pod) if pod else None
                if pod and name in (pod.lower(), inferred or ""):
                    continue
            return name
    if pod:
        return infer_deployment_from_pod_name(pod)
    return None
