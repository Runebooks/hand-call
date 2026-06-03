"""
Kubernetes A2A Agent — read-only cluster inspection.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.a2a_server import A2AServer
from common.models import Artifact, Task

from agents.kubernetes.intent import (
    K8sIntent,
    apply_intent_context,
    detect_mutating_operation,
    is_confirmation_no,
    is_confirmation_yes,
    normalize_confirmation,
    parse_intent_with_llm,
    parse_scale_replicas,
    wants_deployment_name_query,
    wants_deployment_pods,
    wants_scale_mutation,
)
from agents.kubernetes.mutations import (
    build_pending_from_intent,
    execute_mutation,
    mutations_enabled,
)
from agents.kubernetes.pending import PENDING_STORE
from agents.kubernetes.kube_client import (
    KubeClient,
    PodSummary,
    coerce_log_text,
    infer_deployment_from_pod_name,
    parse_deployment_name,
    parse_name_hint,
    parse_namespace,
    parse_slack_alert_hints,
)
from common.llm import LLMClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CARD_PATH = Path(__file__).parent / "agent_card.json"
DEFAULT_PORT = int(os.environ.get("A2A_PORT", "8082"))


class KubernetesAgent(A2AServer):
    def __init__(self, **kwargs):
        super().__init__(agent_card_path=str(CARD_PATH), **kwargs)
        self.kube = KubeClient(context=os.environ.get("K8S_CONTEXT"))
        self.llm = LLMClient()
        self._last_route = "init"

    async def on_startup(self) -> None:
        self.kube.connect()
        if self.llm.enabled():
            logger.info(
                "LLM intent parsing enabled (model=%s, mutations=%s)",
                self.llm.model,
                mutations_enabled(),
            )
        else:
            logger.info(
                "LLM disabled — heuristics only (mutations=%s)",
                mutations_enabled(),
            )

    async def process_task(self, task: Task) -> Task:
        query = task.message.get_text() if task.message else ""
        meta = task.metadata or {}
        meta_ns = (meta.get("namespace") or "").strip() or None
        meta_pod = (
            (meta.get("pod") or meta.get("alert_sre_attributes") or "").strip()
            or None
        )
        if not meta_ns or not meta_pod:
            hint_ns, hint_pod = parse_slack_alert_hints(query)
            meta_ns = meta_ns or hint_ns
            meta_pod = meta_pod or hint_pod
        if meta_ns and meta_pod and meta_pod.lower() == meta_ns.lower():
            logger.warning("metadata pod==namespace (%s), dropping pod", meta_pod)
            meta_pod = parse_slack_alert_hints(query)[1] or None

        logger.info(
            "Task metadata namespace=%s pod=%s query_prefix=%s",
            meta_ns,
            meta_pod,
            query[:120].replace("\n", " "),
        )
        if not query.strip():
            task.mark_failed("Empty query — ask about pods, logs, deployments, or events.")
            task.add_artifact(Artifact.text("Please send a question about your cluster."))
            return task

        try:
            session_id = task.session_id or task.id
            answer = self._handle_query(
                query,
                namespace=meta_ns,
                pod=meta_pod,
                session_id=session_id,
            )
            answer = self._append_routing_footer(answer)
            task.add_artifact(Artifact.text(answer, name="kubernetes-result"))
            task.mark_completed()
        except Exception as e:
            logger.exception("Query failed: %s", query)
            task.mark_failed(str(e))
            task.add_artifact(
                Artifact.text(f"Kubernetes agent error: {e}\n\nQuery: {query}")
            )
        return task

    def _handle_query(
        self,
        query: str,
        *,
        namespace: str | None = None,
        pod: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """LLM decides intent; staging mutations require yes/no confirmation."""
        user_text = _user_question_text(query)
        confirm_text = normalize_confirmation(user_text)

        if session_id:
            pending = PENDING_STORE.get(session_id)
            if pending:
                if is_confirmation_yes(confirm_text):
                    try:
                        result = execute_mutation(self.kube, pending)
                        PENDING_STORE.clear(session_id)
                        return result
                    except Exception as exc:
                        logger.exception("Mutation failed")
                        PENDING_STORE.clear(session_id)
                        return f"**Failed** — could not complete mutation: {exc}"
                if is_confirmation_no(confirm_text):
                    PENDING_STORE.clear(session_id)
                    return "**Cancelled** — no changes were made."
                return (
                    f"⚠️ **Pending confirmation**\n\n"
                    f"**Action:** {pending.summary}\n\n"
                    f"Reply **yes** to proceed or **no** to cancel."
                )

        if self.llm.enabled():
            try:
                self._last_route = f"llm/{self.llm.model}"
                intent = parse_intent_with_llm(
                    query, self.llm, namespace=namespace, pod=pod
                )
                intent = self._apply_metadata_hints(intent, namespace, pod)
                intent = apply_intent_context(
                    intent, query, namespace=namespace, pod=pod, kube=self.kube
                )
                if intent.is_mutating_action and mutations_enabled():
                    return self._request_confirmation(
                        intent, session_id, namespace, pod, query
                    )
                if intent.is_mutating_request:
                    return self._handle_out_of_scope(intent, namespace, pod)
                return self._execute_intent(intent, query)
            except Exception as e:
                logger.warning("LLM intent failed, falling back to heuristics: %s", e)
                self._last_route = "heuristic (llm-fallback)"

        self._last_route = "heuristic"
        mutating = detect_mutating_operation(user_text)
        if mutating == "blocked":
            intent = K8sIntent(
                action="out_of_scope",
                in_scope=False,
                requested_operation="mutate",
                namespace=namespace,
                pod=pod,
                scope_reason="Mutations are disabled (read-only mode).",
            )
            return self._handle_out_of_scope(intent, namespace, pod)
        if mutating and mutations_enabled():
            intent = K8sIntent(
                action=mutating,  # type: ignore[arg-type]
                namespace=namespace,
                pod=pod,
                scale_replicas=parse_scale_replicas(user_text),
            )
            intent = apply_intent_context(
                intent, query, namespace=namespace, pod=pod, kube=self.kube
            )
            return self._request_confirmation(
                intent, session_id, namespace, pod, query
            )

        if mutations_enabled() and wants_scale_mutation(user_text):
            intent = K8sIntent(
                action="scale_deployment",
                namespace=namespace,
                pod=pod,
            )
            intent = apply_intent_context(
                intent, query, namespace=namespace, pod=pod, kube=self.kube
            )
            return self._request_confirmation(
                intent, session_id, namespace, pod, query
            )

        return self._handle_query_heuristic(
            query, namespace=namespace, pod=pod, session_id=session_id
        )

    def _request_confirmation(
        self,
        intent: K8sIntent,
        session_id: str | None,
        namespace: str | None,
        pod: str | None,
        query: str = "",
    ) -> str:
        intent = apply_intent_context(
            intent, query or "", namespace=namespace, pod=pod, kube=self.kube
        )
        if intent.action == "scale_deployment" and intent.scale_replicas is None:
            dep = intent.deployment or "(unknown deployment)"
            return (
                f"**Scale `{dep}`** — how many replicas should I set?\n\n"
                f"Example: `@NOC Handover scale to 6 replicas for deployment {dep}`"
            )
        if intent.action == "scale_deployment" and not intent.deployment:
            return (
                "**Which deployment?** I could not infer it from the pod name.\n\n"
                "Example: `scale deployment my-app to 6 replicas in namespace a2a-ops`"
            )
        try:
            pending = build_pending_from_intent(
                intent, self.kube, namespace=namespace, pod=pod
            )
        except ValueError as exc:
            return f"**Cannot prepare mutation** — {exc}"

        if not session_id:
            return (
                f"⚠️ **Confirmation required**\n\n"
                f"**Action:** {pending.summary}\n\n"
                f"Reply **yes** in this Slack thread to proceed or **no** to cancel."
            )

        PENDING_STORE.set(session_id, pending)
        op_label = pending.operation.replace("_", " ")
        dep_note = ""
        if pending.deployment and pending.pod:
            dep_note = (
                f"\n_Inferred deployment `{pending.deployment}` from pod "
                f"`{pending.pod}`._"
            )
        return (
            f"⚠️ **Confirm {op_label}**\n\n"
            f"**Action:** {pending.summary}{dep_note}\n\n"
            f"Reply **yes** to proceed or **no** to cancel.\n\n"
            f"_Staging cluster — mutation runs after confirmation._"
        )

    @staticmethod
    def _apply_metadata_hints(
        intent: K8sIntent,
        namespace: str | None,
        pod: str | None,
    ) -> K8sIntent:
        """Slack/NOC alert metadata overrides LLM mistakes (e.g. pod=namespace)."""
        if namespace:
            intent.namespace = namespace.lower()
        if pod:
            intent.pod = pod
        if (
            intent.pod
            and intent.namespace
            and intent.pod.lower() == intent.namespace.lower()
        ):
            intent.pod = pod
        return intent

    def _handle_query_heuristic(
        self,
        query: str,
        *,
        namespace: str | None = None,
        pod: str | None = None,
        session_id: str | None = None,
    ) -> str:
        user_text = _user_question_text(query)
        lowered = user_text.lower()
        namespace = namespace or parse_namespace(query)
        name_hint = pod or parse_name_hint(query, namespace=namespace)
        if name_hint and namespace and name_hint.lower() == namespace.lower():
            name_hint = pod

        if _wants_logs(lowered):
            return self._handle_logs(query, namespace, name_hint)
        if wants_deployment_pods(lowered):
            dep = (
                parse_deployment_name(user_text, pod=name_hint)
                or self._resolve_deployment_hint(namespace, name_hint)
            )
            if dep and namespace:
                return self._handle_deployment_pods(namespace, dep)
        if wants_deployment_name_query(lowered):
            dep = self._resolve_deployment_hint(namespace, name_hint)
            return self._handle_deployment_name(namespace, name_hint, dep)
        if _wants_deployments(lowered) and not wants_scale_mutation(lowered):
            dep = self._resolve_deployment_hint(namespace, name_hint)
            return self._handle_deployments(namespace, dep, pod_hint=name_hint)
        if _wants_events(lowered):
            return self._handle_events(namespace, name_hint)
        if _wants_problem_pods(lowered):
            if name_hint and _wants_crash_diagnosis(lowered):
                detail = self._handle_pods(namespace, name_hint)
                logs = self._handle_logs(query, namespace, name_hint, tail=50)
                return f"{detail}\n\n{logs}"
            return self._handle_problem_pods(namespace, name_hint)
        return self._handle_pods(namespace, name_hint)

    def _handle_out_of_scope(
        self,
        intent: K8sIntent,
        namespace: str | None,
        pod: str | None,
    ) -> str:
        ns = intent.namespace or namespace
        p = intent.pod or pod
        op = (intent.requested_operation or "change the cluster").replace("_", " ")

        lines = [
            "**Out of scope** — kubernetes-agent is **read-only** (investigate only).",
            f"I cannot `{op}` for you.",
        ]
        if intent.scope_reason:
            lines.append(intent.scope_reason)
        else:
            lines.append(
                "I can help with pod status, logs, deployment replica health, "
                "and warning events."
            )

        if p and ns:
            kubectl = _suggest_kubectl(op, p, ns, self.kube)
            if kubectl:
                lines.append(
                    f"\n**Suggested command** (on-call runs manually):\n```\n{kubectl}\n```"
                )

            summary = self.kube.find_pod(p, namespace=ns)
            if summary:
                lines.append("\n**Current status**\n" + self._format_pod_detail(summary))

        return "\n".join(lines)

    def _execute_intent(self, intent: K8sIntent, query: str) -> str:
        ns = intent.namespace
        pod = intent.pod
        if pod and ns and pod.lower() == ns.lower():
            logger.warning("Ignoring pod==namespace hint %s", pod)
            pod = None
        if intent.action == "out_of_scope":
            return self._handle_out_of_scope(intent, intent.namespace, intent.pod)
        if intent.is_mutating_action:
            return self._request_confirmation(intent, None, intent.namespace, intent.pod)
        if intent.action == "pod_logs":
            return self._handle_logs(
                query,
                ns,
                pod,
                previous=intent.previous_logs,
                tail=intent.tail_lines,
            )
        if intent.action == "deployments":
            user = _user_question_text(query)
            if wants_deployment_name_query(user):
                dep = intent.deployment or self._resolve_deployment_hint(ns, pod)
                return self._handle_deployment_name(ns, pod, dep)
            dep = intent.deployment or self._resolve_deployment_hint(ns, pod)
            return self._handle_deployments(ns, dep, pod_hint=pod)
        if intent.action == "events":
            return self._handle_events(ns, pod)
        if intent.action == "problem_pods":
            return self._handle_problem_pods(ns, pod)
        if intent.action == "list_pods" and intent.deployment:
            return self._handle_deployment_pods(ns, intent.deployment)
        return self._handle_pods(ns, pod)

    def _resolve_deployment_hint(
        self, namespace: str | None, name_hint: str | None
    ) -> str | None:
        if not name_hint:
            return None
        inferred = infer_deployment_from_pod_name(name_hint)
        if inferred:
            return inferred
        if namespace:
            deps = self.kube.list_deployments(
                namespace=namespace, name_contains=name_hint, limit=5
            )
            if deps:
                return deps[0]["name"]
        return name_hint

    def _append_routing_footer(self, answer: str) -> str:
        show = os.environ.get("K8S_SHOW_ROUTING", "true").strip().lower()
        if show in ("0", "false", "no", "off"):
            return answer
        if self._last_route.startswith("llm/"):
            return f"{answer}\n\n_routing: {self._last_route}_"
        if self.llm.enabled():
            return f"{answer}\n\n_routing: {self._last_route}_"
        return (
            f"{answer}\n\n_routing: {self._last_route} "
            f"(set OPENAI_API_KEY in .env.local for LLM intent)_"
        )

    def _handle_deployment_name(
        self,
        namespace: str | None,
        pod_hint: str | None,
        deployment_hint: str | None = None,
    ) -> str:
        if not pod_hint and not deployment_hint:
            return (
                "Specify a pod (from the alert thread) to look up its owning deployment."
            )
        dep = deployment_hint or self._resolve_deployment_hint(namespace, pod_hint)
        if pod_hint and namespace:
            try:
                workload = self.kube.get_pod_workload(namespace, pod_hint)
            except Exception:
                workload = {}
            if workload.get("standalone"):
                return (
                    f"**Deployment name** — pod `{namespace}/{pod_hint}` is a "
                    f"**standalone Pod** (not owned by a Deployment)."
                )
            if workload.get("deployment"):
                dep = workload["deployment"]
            elif workload.get("job"):
                return (
                    f"**Deployment name** — pod `{namespace}/{pod_hint}` is owned by "
                    f"Job `{workload['job']}`, not a Deployment."
                )
        if not dep:
            return (
                f"Could not infer a deployment from pod `{pod_hint}`"
                + (f" in `{namespace}`." if namespace else ".")
            )
        return (
            f"**Deployment name** — pod `{pod_hint}` belongs to deployment "
            f"`{namespace}/{dep}`."
            if namespace and pod_hint
            else f"**Deployment name** — `{namespace}/{dep}`."
        )

    def _handle_deployment_pods(
        self, namespace: str | None, deployment_name: str
    ) -> str:
        if not namespace:
            return "Namespace is required to list deployment pods."
        pods = self.kube.list_pods_for_deployment(namespace, deployment_name)
        if not pods:
            return (
                f"No pods found for deployment `{deployment_name}` "
                f"in namespace `{namespace}`."
            )
        deps = self.kube.list_deployments(
            namespace=namespace, name_contains=deployment_name, limit=1
        )
        ready = deps[0]["ready"] if deps else "?"
        lines = [
            f"**Pods for deployment** `{namespace}/{deployment_name}` "
            f"(replicas {ready}, {len(pods)} pod(s) shown)\n",
            self._format_pod_table(pods),
        ]
        return "\n".join(lines)

    def _handle_pods(
        self, namespace: str | None, name_hint: str | None
    ) -> str:
        if name_hint and namespace and name_hint.lower() == namespace.lower():
            logger.warning(
                "Refusing to search for pod named like namespace (%s)", name_hint
            )
            name_hint = None
        if name_hint:
            pod = self.kube.find_pod(name_hint, namespace=namespace)
            if not pod:
                return (
                    f"No pod matching `{name_hint}`"
                    + (f" in namespace `{namespace}`" if namespace else "")
                    + ".\n\n"
                    + self._format_pod_table(
                        self.kube.list_pods(namespace=namespace, limit=15)
                    )
                )
            return self._format_pod_detail(pod)

        pods = self.kube.list_pods(namespace=namespace, limit=30)
        title = "Pods (all namespaces)" if not namespace else f"Pods in `{namespace}`"
        return f"**{title}**\n\n{self._format_pod_table(pods)}"

    def _handle_problem_pods(
        self, namespace: str | None, name_hint: str | None
    ) -> str:
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
        if name_hint and len(name_hint) >= 4:
            filtered = [p for p in bad if name_hint.lower() in p.name.lower()]
            if filtered:
                bad = filtered

        if not bad:
            scope = f"namespace `{namespace}`" if namespace else "cluster"
            return f"No unhealthy pods found in {scope}."

        lines = [f"**Unhealthy pods** ({len(bad)} found)\n"]
        for pod in bad[:20]:
            lines.append(self._format_pod_detail(pod, compact=False))
            lines.append("")
        return "\n".join(lines)

    def _handle_logs(
        self,
        query: str,
        namespace: str | None,
        name_hint: str | None,
        *,
        previous: bool | None = None,
        tail: int | None = None,
    ) -> str:
        if not name_hint:
            return (
                "Specify a pod name for logs, e.g. "
                "`Get logs for payments-7d4b8c-x2k9f in namespace production`."
            )

        pod = self.kube.find_pod(name_hint, namespace=namespace)
        if not pod:
            return f"No pod matching `{name_hint}` found."

        if previous is None:
            previous = "previous" in query.lower()
        if tail is None:
            tail = 100
            tail_match = re.search(r"last\s+(\d+)\s+lines?", query, re.I)
            if tail_match:
                tail = min(int(tail_match.group(1)), 500)

        logs = self.kube.get_pod_logs(
            namespace=pod.namespace,
            pod_name=pod.name,
            previous=previous,
            tail_lines=tail,
        )
        logs = coerce_log_text(logs)
        header = (
            f"**Logs** — `{pod.namespace}/{pod.name}`"
            + (" (previous container)" if previous else "")
            + f" (last {tail} lines)\n\n```\n"
        )
        return header + logs[-8000:] + "\n```"

    def _handle_deployments(
        self,
        namespace: str | None,
        name_hint: str | None,
        *,
        pod_hint: str | None = None,
    ) -> str:
        pod_name = pod_hint or name_hint
        inferred_dep = (
            infer_deployment_from_pod_name(pod_name)
            if pod_name
            else infer_deployment_from_pod_name(name_hint or "")
        )
        if name_hint and not inferred_dep:
            inferred_dep = infer_deployment_from_pod_name(name_hint)
        if pod_name and namespace and self.kube.find_pod(pod_name, namespace=namespace):
            pod = self.kube.find_pod(pod_name, namespace=namespace)
            if pod:
                try:
                    workload = self.kube.get_pod_workload(pod.namespace, pod.name)
                except Exception as exc:
                    logger.warning("Workload lookup failed: %s", exc)
                    workload = {"standalone": True, "restarts": pod.restarts}

                if workload.get("standalone"):
                    owners = workload.get("owner_kinds") or []
                    owner_note = ""
                    if owners:
                        owner_note = (
                            f"\n- Owner: {', '.join(owners)} (not a Deployment)"
                        )
                    return (
                        f"**Replica count** — `{pod.namespace}/{pod.name}` is a "
                        f"**standalone Pod**, not managed by a Deployment.\n\n"
                        f"- No deployment replica count applies to this workload.\n"
                        f"- Container restarts: **{pod.restarts}** "
                        f"(restart count, not replicas).\n"
                        f"- Ready: {pod.ready} · Status: {pod.status}"
                        f"{owner_note}\n\n"
                        f"_Tip: replica count questions apply to Deployments. "
                        f"This demo pod is defined directly as a Pod resource._"
                    )

                if workload.get("deployment"):
                    dep_name = workload["deployment"]
                    return (
                        f"**Deployment replicas** — `{pod.namespace}/{dep_name}`\n\n"
                        f"- Ready: **{workload['ready']}**\n"
                        f"- Available: **{workload.get('available', 0)}**\n"
                        f"- Pod `{pod.name}` is managed by this deployment."
                    )

                if workload.get("job"):
                    return (
                        f"**Replica count** — `{pod.namespace}/{pod.name}` is owned by "
                        f"Job `{workload['job']}`. Jobs do not have a replica count like "
                        f"Deployments."
                    )

        dep_filter = inferred_dep or name_hint
        if dep_filter and infer_deployment_from_pod_name(dep_filter):
            dep_filter = infer_deployment_from_pod_name(dep_filter)
        if not dep_filter and pod_name:
            dep_filter = infer_deployment_from_pod_name(pod_name) or pod_name

        deps = self.kube.list_deployments(
            namespace=namespace, name_contains=dep_filter, limit=30
        )
        if not deps and name_hint and name_hint != dep_filter:
            deps = self.kube.list_deployments(
                namespace=namespace, name_contains=name_hint, limit=30
            )
        if not deps:
            scope = f"namespace `{namespace}`" if namespace else "cluster"
            if name_hint:
                return (
                    f"No Deployments matching `{name_hint}` in {scope}.\n\n"
                    f"If you meant pod `{name_hint}`, it may be a standalone Pod "
                    f"(no Deployment replica count)."
                )
            return f"No deployments found in {scope}."

        lines = [
            f"**Deployments** ({len(deps)})\n",
            "| Namespace | Name | Ready | Conditions |",
            "|-----------|------|-------|------------|",
        ]
        for d in deps:
            cond = ", ".join(d["conditions"][:2]) or "—"
            lines.append(
                f"| {d['namespace']} | {d['name']} | {d['ready']} | {cond} |"
            )
        return "\n".join(lines)

    def _handle_events(
        self, namespace: str | None, name_hint: str | None
    ) -> str:
        events = self.kube.list_warning_events(
            namespace=namespace, involved_name=name_hint, limit=20
        )
        if not events:
            return "No recent **Warning** events found."

        lines = [f"**Warning events** ({len(events)})\n"]
        for ev in events:
            lines.append(
                f"- `{ev['namespace']}` {ev['object']}: **{ev['reason']}** "
                f"(×{ev['count']}, {ev['last']})\n  {ev['message']}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_pod_table(pods: list[PodSummary]) -> str:
        if not pods:
            return "_No pods found._"
        lines = [
            "| Namespace | Pod | Ready | Status | Restarts | Age |",
            "|-----------|-----|-------|--------|----------|-----|",
        ]
        for p in pods:
            lines.append(
                f"| {p.namespace} | {p.name} | {p.ready} | {p.status} | "
                f"{p.restarts} | {p.age} |"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_pod_detail(pod: PodSummary, compact: bool = True) -> str:
        lines = [
            f"**Pod** `{pod.namespace}/{pod.name}`",
            f"- Ready: {pod.ready}",
            f"- Status: {pod.status}",
            f"- Restarts: {pod.restarts}",
            f"- Age: {pod.age}",
        ]
        if pod.reason:
            lines.append(f"- Reason: {pod.reason}")
        if pod.message:
            lines.append(f"- Message: {pod.message}")
        if compact and pod.restarts > 0:
            lines.append(
                "\n_Tip: ask for logs with previous container, e.g. "
                f'"logs for {pod.name} previous"_'
            )
        return "\n".join(lines)


def _user_question_text(query: str) -> str:
    """Use only the on-call question for intent keywords (not pod names in context)."""
    marker = "\n\nKubernetes context:"
    if marker in query:
        return query.split(marker, 1)[0].strip()
    if "\n\nUse cluster context:" in query:
        return query.split("\n\nUse cluster context:", 1)[0].strip()
    return query.strip()


def _wants_logs(text: str) -> bool:
    return bool(re.search(r"\b(logs?|logging|stderr|stdout)\b", text))


def _wants_deployments(text: str) -> bool:
    if wants_scale_mutation(text):
        return False
    if wants_deployment_name_query(text):
        return False
    return bool(
        re.search(r"\b(deployments?|rollout|replicas?|replica\s+count)\b", text)
    )


def _wants_events(text: str) -> bool:
    return bool(re.search(r"\b(events?|warning)\b", text))


def _wants_problem_pods(text: str) -> bool:
    return bool(
        re.search(
            r"\b(restarting|crash|crashing|crashed|crashloop|crashloopbackoff|unhealthy|slow|why|oom|failed)\b",
            text,
        )
    )


def _wants_crash_diagnosis(text: str) -> bool:
    return bool(
        re.search(
            r"\b(why|reason|cause|wrong|crashing|crashed|crashloop|crashloopbackoff)\b",
            text,
        )
    )


def _suggest_kubectl(
    operation: str,
    pod: str,
    namespace: str,
    kube: KubeClient,
) -> str:
    op = operation.lower()
    if op in ("restart", "reboot", "recycle"):
        try:
            workload = kube.get_pod_workload(namespace, pod)
            if workload.get("deployment"):
                dep = workload["deployment"]
                return (
                    f"kubectl rollout restart deployment/{dep} -n {namespace}\n"
                    f"# or delete pod to recreate:\n"
                    f"kubectl delete pod {pod} -n {namespace}"
                )
        except Exception:
            pass
        return (
            f"kubectl delete pod {pod} -n {namespace}\n"
            f"# Standalone pods are not recreated automatically."
        )
    if op in ("delete", "remove", "kill", "terminate"):
        return f"kubectl delete pod {pod} -n {namespace}"
    if op == "scale":
        return (
            f"kubectl scale deployment/<name> --replicas=<N> -n {namespace}\n"
            f"# Replace <name> and <N> with the target deployment."
        )
    return ""


def main() -> None:
    host = os.environ.get("A2A_HOST", "0.0.0.0")
    port = DEFAULT_PORT
    agent = KubernetesAgent(host=host, port=port)
    agent.run()


if __name__ == "__main__":
    main()
