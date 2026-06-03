"""Staging-only cluster mutations (behind K8S_MUTATIONS_ENABLED + confirmation)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from kubernetes.client.rest import ApiException

from agents.kubernetes.kube_client import KubeClient, parse_deployment_name
from agents.kubernetes.pending import PendingMutation


def mutations_enabled() -> bool:
    flag = os.environ.get("K8S_MUTATIONS_ENABLED", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def execute_mutation(kube: KubeClient, pending: PendingMutation) -> str:
    op = pending.operation
    ns = pending.namespace

    if op == "delete_pod":
        if not pending.pod:
            raise ValueError("pod name required for delete")
        kube.delete_pod(ns, pending.pod)
        return (
            f"**Done** — deleted pod `{ns}/{pending.pod}`.\n\n"
            f"_Staging mutation executed successfully._"
        )

    if op == "restart_pod":
        if pending.deployment:
            kube.rollout_restart_deployment(ns, pending.deployment)
            return (
                f"**Done** — rollout restart triggered for deployment "
                f"`{ns}/{pending.deployment}`.\n\n"
                f"_Pods will roll gradually._"
            )
        if not pending.pod:
            raise ValueError("pod name required for restart")
        kube.delete_pod(ns, pending.pod)
        return (
            f"**Done** — deleted pod `{ns}/{pending.pod}` to force restart.\n\n"
            f"_Standalone pod: it will not recreate unless a controller exists._"
        )

    if op == "rollout_restart":
        if not pending.deployment:
            raise ValueError("deployment name required")
        kube.rollout_restart_deployment(ns, pending.deployment)
        return (
            f"**Done** — rollout restart triggered for `{ns}/{pending.deployment}`."
        )

    if op == "scale_deployment":
        if not pending.deployment or pending.scale_replicas is None:
            raise ValueError("deployment and replica count required")
        kube.scale_deployment(ns, pending.deployment, pending.scale_replicas)
        return (
            f"**Done** — scaled deployment `{ns}/{pending.deployment}` "
            f"to **{pending.scale_replicas}** replica(s)."
        )

    raise ValueError(f"Unsupported mutation operation: {op}")


def build_pending_from_intent(
    intent,
    kube: KubeClient,
    *,
    namespace: Optional[str],
    pod: Optional[str],
) -> PendingMutation:
    ns = intent.namespace or namespace
    p = intent.pod or pod
    if not ns:
        raise ValueError("namespace is required for mutations")

    op = intent.action
    if op in ("restart_pod", "delete_pod") and p:
        workload = kube.get_pod_workload(ns, p)
        dep = workload.get("deployment")
        standalone = workload.get("standalone")
        if op == "restart_pod" and dep:
            return PendingMutation(
                operation="restart_pod",
                namespace=ns,
                pod=p,
                deployment=dep,
                summary=(
                    f"Rollout restart deployment `{dep}` in `{ns}` "
                    f"(pod `{p}` will be recreated)"
                ),
            )
        verb = "delete pod" if op == "delete_pod" else "delete pod to restart"
        extra = (
            " Standalone pod — will **not** auto-recreate."
            if standalone
            else ""
        )
        return PendingMutation(
            operation=op if op == "delete_pod" else "restart_pod",
            namespace=ns,
            pod=p,
            summary=f"{verb.capitalize()} `{p}` in `{ns}`.{extra}",
        )

    if op == "rollout_restart" and (intent.deployment or p):
        dep = intent.deployment
        if not dep and p:
            workload = kube.get_pod_workload(ns, p)
            dep = workload.get("deployment")
        if not dep:
            raise ValueError("Could not resolve deployment for rollout restart")
        return PendingMutation(
            operation="rollout_restart",
            namespace=ns,
            pod=p,
            deployment=dep,
            summary=f"Rollout restart deployment `{dep}` in `{ns}`",
        )

    if op == "scale_deployment":
        dep = intent.deployment
        replicas = intent.scale_replicas
        if not dep and p:
            inferred = parse_deployment_name("", pod=p)
            if inferred:
                dep = inferred
            else:
                workload = kube.get_pod_workload(ns, p)
                dep = workload.get("deployment")
        if not dep:
            raise ValueError("Could not resolve deployment to scale")
        if replicas is None:
            raise ValueError("Specify target replica count (e.g. scale to 3)")
        return PendingMutation(
            operation="scale_deployment",
            namespace=ns,
            pod=p,
            deployment=dep,
            scale_replicas=replicas,
            summary=f"Scale deployment `{dep}` in `{ns}` to **{replicas}** replicas",
        )

    raise ValueError(f"Cannot build pending mutation for action {op}")
