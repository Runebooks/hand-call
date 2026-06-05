"""
MCP tool layer for the Kubernetes agent.

This module is the "tool dispatcher" of the pod: it exposes cluster operations
as MCP tools backed by ``KubeClient``. The LLM "brain" (see ``agent_loop.py``)
decides which tools to call; this module executes them.

Two surfaces are provided:

- ``KubeToolDispatcher`` + ``tool_specs()`` + ``call_tool()`` — an in-process
  dispatcher the agent loop uses directly (reliable, synchronous).
- ``build_mcp_server()`` — a genuine ``FastMCP`` server registering the same
  tools, so the agent is a real MCP server (mountable / inspectable). The
  ``mcp`` SDK is optional: if it is not installed the dispatcher still works.

Read tools are always available. Mutation tools are only exposed when
``K8S_MUTATIONS_ENABLED=true`` and they NEVER execute directly — they return a
``confirmation_required`` payload plus a ``PendingMutation`` so the Slack
yes/no confirmation flow in ``server.py`` can run the change after approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

from agents.kubernetes.kube_client import KubeClient, PodSummary
from agents.kubernetes.mutations import build_pending_from_intent, mutations_enabled
from agents.kubernetes.pending import PendingMutation

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Result of a tool call.

    ``data`` is JSON-serializable and fed back to the LLM. ``pending`` is set
    only for mutation tools, so the caller can request Slack confirmation
    instead of executing the change.
    """

    data: Any
    pending: Optional[PendingMutation] = None


def _pod_to_dict(pod: PodSummary) -> dict[str, Any]:
    return {
        "namespace": pod.namespace,
        "name": pod.name,
        "ready": pod.ready,
        "status": pod.status,
        "restarts": pod.restarts,
        "age": pod.age,
        "reason": pod.reason,
        "message": pod.message,
    }


READ_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_namespaces",
            "description": (
                "List ALL namespaces in the cluster and their count. Use for "
                "'how many namespaces' / 'list namespaces' — do NOT infer the "
                "count from pods, which misses empty namespaces."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_pods",
            "description": (
                "List pods, optionally filtered by namespace and a name "
                "substring. Use for inventory / 'how many pods' questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Namespace; omit for all namespaces."},
                    "name_contains": {"type": "string", "description": "Filter pods whose name contains this."},
                    "limit": {"type": "integer", "description": "Max pods to return (default 50)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_pod",
            "description": "Find the single best-matching pod by name hint (returns its detailed status).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Pod name or hint."},
                    "namespace": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "problem_pods",
            "description": (
                "List unhealthy pods (restarts, CrashLoopBackOff, OOM, errors). "
                "Use to diagnose 'why is it restarting/crashing'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "name_contains": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pod_logs",
            "description": "Fetch container logs for a pod. Set previous=true for the last crashed instance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pod": {"type": "string"},
                    "namespace": {"type": "string"},
                    "previous": {"type": "boolean", "description": "Logs from previous (crashed) container."},
                    "tail_lines": {"type": "integer", "description": "20-500, default 100."},
                },
                "required": ["pod"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deployments",
            "description": "List deployments and replica health, optionally filtered by name substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "name_contains": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deployment_pods",
            "description": "List the pods belonging to a specific deployment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "deployment": {"type": "string"},
                },
                "required": ["namespace", "deployment"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pod_workload",
            "description": (
                "Resolve whether a pod is standalone or owned by a Deployment/Job, "
                "including replica counts. Use for 'which deployment owns this pod' "
                "or replica-count questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                },
                "required": ["namespace", "pod"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "warning_events",
            "description": "List recent Warning events, optionally for a specific object name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "involved_name": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "namespace_pod_summary",
            "description": "Summarize pod health counts for a namespace (total / running / unhealthy).",
            "parameters": {
                "type": "object",
                "properties": {"namespace": {"type": "string"}},
                "required": ["namespace"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_nodes",
            "description": (
                "List cluster nodes with readiness (Ready/NotReady), roles, "
                "kubelet version, and resource pressure (Memory/Disk/PID). Use "
                "for 'how many nodes', 'are any nodes NotReady', node health."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_pod",
            "description": (
                "Detailed per-container spec and status for one pod: image, "
                "command/args (entrypoint), env var names, resource "
                "requests/limits, restart count, and current/last state with "
                "exit code & reason. Use to find WHY a pod crashes (e.g. exit "
                "code, OOMKilled, bad command) or what image/config it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                },
                "required": ["namespace", "pod"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": (
                "List Services with type, clusterIP, ports and selector, "
                "optionally filtered by namespace and name substring."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "name_contains": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cluster_overview",
            "description": (
                "High-level cluster snapshot: namespace count, node "
                "ready/total, and pod total/unhealthy across all namespaces. "
                "Use for 'cluster health' / 'give me an overview'."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

MUTATE_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "restart_pod",
            "description": (
                "Restart a pod (rollout restart if Deployment-owned, else delete "
                "the pod). Requires user confirmation before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                },
                "required": ["namespace", "pod"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_pod",
            "description": "Delete a pod. Requires user confirmation before it runs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "pod": {"type": "string"},
                },
                "required": ["namespace", "pod"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scale_deployment",
            "description": "Set a deployment's replica count. Requires user confirmation before it runs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "deployment": {"type": "string"},
                    "replicas": {"type": "integer"},
                },
                "required": ["namespace", "deployment", "replicas"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollout_restart",
            "description": "Rollout restart a deployment. Requires user confirmation before it runs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string"},
                    "deployment": {"type": "string"},
                },
                "required": ["namespace", "deployment"],
            },
        },
    },
]

READ_TOOL_NAMES = {spec["function"]["name"] for spec in READ_TOOL_SPECS}
MUTATE_TOOL_NAMES = {spec["function"]["name"] for spec in MUTATE_TOOL_SPECS}


def tool_specs(include_mutations: Optional[bool] = None) -> list[dict[str, Any]]:
    """OpenAI-format tool list. Mutation tools included only when enabled."""
    if include_mutations is None:
        include_mutations = mutations_enabled()
    specs = list(READ_TOOL_SPECS)
    if include_mutations:
        specs += MUTATE_TOOL_SPECS
    return specs


class KubeToolDispatcher:
    """Executes MCP tool calls against a shared ``KubeClient``."""

    def __init__(self, kube: KubeClient):
        self.kube = kube

    def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        args = args or {}
        if name in MUTATE_TOOL_NAMES:
            if not mutations_enabled():
                return ToolResult(
                    data={
                        "error": "mutations_disabled",
                        "message": "Cluster is read-only (K8S_MUTATIONS_ENABLED=false).",
                    }
                )
            return self._mutation(name, args)
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolResult(data={"error": "unknown_tool", "tool": name})
        try:
            return ToolResult(data=handler(args))
        except Exception as exc:  # surface errors to the LLM, do not crash the loop
            logger.warning("Tool %s failed: %s", name, exc)
            return ToolResult(data={"error": "tool_failed", "tool": name, "message": str(exc)})

    # ----- read tools -----
    def _tool_list_namespaces(self, args: dict[str, Any]) -> Any:
        names = self.kube.list_namespaces()
        return {"count": len(names), "namespaces": names}

    def _tool_list_pods(self, args: dict[str, Any]) -> Any:
        pods = self.kube.list_pods(
            namespace=args.get("namespace"),
            name_contains=args.get("name_contains"),
            limit=int(args.get("limit") or 50),
        )
        return {"count": len(pods), "pods": [_pod_to_dict(p) for p in pods]}

    def _tool_find_pod(self, args: dict[str, Any]) -> Any:
        pod = self.kube.find_pod(args["name"], namespace=args.get("namespace"))
        return _pod_to_dict(pod) if pod else {"found": False, "name": args.get("name")}

    def _tool_problem_pods(self, args: dict[str, Any]) -> Any:
        namespace = args.get("namespace")
        name_contains = args.get("name_contains")
        pods = self.kube.list_pods(namespace=namespace, limit=100)
        bad = [
            p
            for p in pods
            if p.restarts > 0
            or "crash" in p.status.lower()
            or "error" in p.status.lower()
            or "oom" in (p.reason or "").lower()
            or (p.reason and p.reason not in ("Completed", "ContainerCreating"))
        ]
        if name_contains and len(name_contains) >= 4:
            filtered = [p for p in bad if name_contains.lower() in p.name.lower()]
            if filtered:
                bad = filtered
        return {"count": len(bad), "pods": [_pod_to_dict(p) for p in bad[:20]]}

    def _tool_pod_logs(self, args: dict[str, Any]) -> Any:
        pod = self.kube.find_pod(args["pod"], namespace=args.get("namespace"))
        if not pod:
            return {"found": False, "pod": args.get("pod")}
        tail = int(args.get("tail_lines") or 100)
        tail = max(20, min(tail, 500))
        logs = self.kube.get_pod_logs(
            namespace=pod.namespace,
            pod_name=pod.name,
            previous=bool(args.get("previous")),
            tail_lines=tail,
        )
        return {
            "namespace": pod.namespace,
            "pod": pod.name,
            "previous": bool(args.get("previous")),
            "tail_lines": tail,
            "logs": (logs or "")[-8000:],
        }

    def _tool_deployments(self, args: dict[str, Any]) -> Any:
        deps = self.kube.list_deployments(
            namespace=args.get("namespace"),
            name_contains=args.get("name_contains"),
            limit=30,
        )
        return {"count": len(deps), "deployments": deps}

    def _tool_deployment_pods(self, args: dict[str, Any]) -> Any:
        pods = self.kube.list_pods_for_deployment(args["namespace"], args["deployment"])
        return {"count": len(pods), "pods": [_pod_to_dict(p) for p in pods]}

    def _tool_pod_workload(self, args: dict[str, Any]) -> Any:
        return self.kube.get_pod_workload(args["namespace"], args["pod"])

    def _tool_warning_events(self, args: dict[str, Any]) -> Any:
        events = self.kube.list_warning_events(
            namespace=args.get("namespace"),
            involved_name=args.get("involved_name"),
            limit=20,
        )
        return {"count": len(events), "events": events}

    def _tool_namespace_pod_summary(self, args: dict[str, Any]) -> Any:
        namespace = args["namespace"]
        pods = self.kube.list_pods(namespace=namespace, limit=200)
        running = [p for p in pods if _is_running_healthy(p)]
        return {
            "namespace": namespace,
            "total": len(pods),
            "running_ready": len(running),
            "not_ready": len(pods) - len(running),
            "pods": [_pod_to_dict(p) for p in pods],
        }

    def _tool_list_nodes(self, args: dict[str, Any]) -> Any:
        nodes = self.kube.list_nodes()
        not_ready = [n["name"] for n in nodes if not n["ready"]]
        return {
            "count": len(nodes),
            "ready": len(nodes) - len(not_ready),
            "not_ready": not_ready,
            "nodes": nodes,
        }

    def _tool_describe_pod(self, args: dict[str, Any]) -> Any:
        pod = self.kube.find_pod(args["pod"], namespace=args.get("namespace"))
        if not pod:
            return {"found": False, "pod": args.get("pod")}
        return self.kube.describe_pod(pod.namespace, pod.name)

    def _tool_list_services(self, args: dict[str, Any]) -> Any:
        services = self.kube.list_services(
            namespace=args.get("namespace"),
            name_contains=args.get("name_contains"),
        )
        return {"count": len(services), "services": services}

    def _tool_cluster_overview(self, args: dict[str, Any]) -> Any:
        namespaces = self.kube.list_namespaces()
        nodes = self.kube.list_nodes()
        pods = self.kube.list_pods(namespace=None, limit=500)
        unhealthy = [p for p in pods if not _is_running_healthy(p)]
        return {
            "namespaces": len(namespaces),
            "nodes_total": len(nodes),
            "nodes_ready": sum(1 for n in nodes if n["ready"]),
            "nodes_not_ready": [n["name"] for n in nodes if not n["ready"]],
            "pods_total": len(pods),
            "pods_unhealthy": len(unhealthy),
            "unhealthy_sample": [_pod_to_dict(p) for p in unhealthy[:15]],
        }

    # ----- mutation tools (confirmation only) -----
    def _mutation(self, name: str, args: dict[str, Any]) -> ToolResult:
        intent = SimpleNamespace(
            action=name,
            namespace=args.get("namespace"),
            pod=args.get("pod"),
            deployment=args.get("deployment"),
            scale_replicas=args.get("replicas"),
        )
        try:
            pending = build_pending_from_intent(
                intent,
                self.kube,
                namespace=args.get("namespace"),
                pod=args.get("pod"),
            )
        except ValueError as exc:
            return ToolResult(data={"error": "cannot_prepare_mutation", "message": str(exc)})
        return ToolResult(
            data={
                "confirmation_required": True,
                "operation": pending.operation,
                "summary": pending.summary,
                "note": "Awaiting user yes/no confirmation before execution.",
            },
            pending=pending,
        )


def _is_running_healthy(pod: PodSummary) -> bool:
    import re

    match = re.match(r"(\d+)/(\d+)", pod.ready)
    if not match or match.group(1) != match.group(2) or match.group(1) == "0":
        return False
    reason = (pod.reason or "").lower()
    if reason in (
        "crashloopbackoff",
        "imagepullbackoff",
        "errimagepull",
        "createcontainerconfigerror",
        "oomkilled",
    ):
        return False
    status = pod.status.lower()
    if "crash" in status or "error" in status:
        return False
    return status.startswith("running")


def build_mcp_server(kube: KubeClient, dispatcher: Optional[KubeToolDispatcher] = None):
    """Build a real ``FastMCP`` server exposing the same tools.

    Returns ``None`` if the optional ``mcp`` SDK is not installed; the agent
    loop falls back to the in-process dispatcher in that case.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - optional dependency
        logger.info("mcp SDK not available (%s); using in-process dispatcher only", exc)
        return None

    disp = dispatcher or KubeToolDispatcher(kube)
    server = FastMCP("kubernetes-agent")

    def _register(spec: dict[str, Any]) -> None:
        fn = spec["function"]
        tool_name = fn["name"]

        async def _tool(**kwargs: Any) -> Any:
            return disp.call_tool(tool_name, kwargs).data

        _tool.__name__ = tool_name
        server.tool(name=tool_name, description=fn.get("description", ""))(_tool)

    for spec in tool_specs(include_mutations=True):
        _register(spec)
    return server
