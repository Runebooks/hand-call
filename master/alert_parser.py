"""
Parse Trigmetry / NOC-Automator / Haystack-style Slack alert messages into structured fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from common.haystack_tenant import is_haystack_tenant


@dataclass
class AlertContext:
    """Normalized alert fields from a Slack notification."""

    raw_text: str
    subject: str = ""
    alertname: str = ""
    namespace: str = ""
    cluster: str = ""
    status: str = ""
    severity: str = ""
    priority: str = ""
    region: str = ""
    product: str = ""
    hostname: str = ""
    dashboard: str = ""
    notification_channel: str = ""
    alert_sre_attributes: str = ""
    oncall: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return self.fields.get("summary", "")

    @property
    def is_firing(self) -> bool:
        return self.status.lower() in ("firing", "open", "")

    @property
    def pod_hint(self) -> Optional[str]:
        """
        Pod name from NOC-Automator field alert_sre_attributes (primary).
        Falls back to hostname only when alert_sre_attributes is empty.
        """
        sre = (self.alert_sre_attributes or "").strip().strip("`")
        if sre:
            return sre.split()[-1]
        hostname = (self.hostname or "").strip().strip("`")
        if hostname:
            token = hostname.split()[-1]
            if _looks_like_pod_name(token):
                return token
        return None


# Trigmetry / NOC-Automator keys (also matched inline in dense message bodies)
_INLINE_ALERT_KEYS = (
    "alertname",
    "namespace",
    "status",
    "cluster",
    "region",
    "severity",
    "priority",
    "hostname",
    "product",
    "oncall",
    "alert_sre_attributes",
    "notification_channel",
    "source",
    "service",
    "current_value",
    "threshold",
    "alert_type",
    "summary",
    "dashboard",
    "dashboard1",
)


def _extract_inline_alert_fields(text: str) -> dict[str, str]:
    """Parse key:value pairs anywhere in text (single-line / block bodies)."""
    fields: dict[str, str] = {}
    if not text:
        return fields
    text = normalize_alert_text(text)
    for key in _INLINE_ALERT_KEYS:
        pattern = rf"(?i)(?:^|[\s\n])`?{re.escape(key)}`?\s*:\s*([^\s\n\r`]+)"
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip().strip("`")
            if _valid_field_value(key, value):
                fields[key.lower()] = value
    return fields


def _looks_like_pod_name(name: str) -> bool:
    if len(name) < 4 or len(name) > 253:
        return False
    if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name.lower()):
        return False
    return "-" in name or re.search(r"\d", name)


def normalize_alert_text(text: str) -> str:
    """NOC-Automator uses Slack mrkdwn `field`:value — normalize to field:value."""
    if not text:
        return ""
    # `alertname`:KubePodCrashLooping → alertname:KubePodCrashLooping
    return re.sub(r"`([a-zA-Z0-9_]+)`\s*:", r"\1:", text)


def _valid_field_value(key: str, value: str) -> bool:
    if not value or len(value) < 2:
        return False
    if value.lower() in ("and", "or", "the", "na"):
        return False
    if key == "namespace" and value == ".":
        return False
    if key == "alertname" and value.lower() in ("and", "or"):
        return False
    return True


def parse_alert_message(text: str) -> Optional[AlertContext]:
    """
    Parse key:value alert bodies posted to Slack.

    Returns None if the message does not look like an ops alert.
    """
    if not text:
        return None

    text = normalize_alert_text(text)
    lowered = text.lower()
    if "alertname:" not in lowered and "kubepod" not in lowered and "kube" not in lowered:
        return None

    fields: dict[str, str] = {}
    subject = ""

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("show less"):
            break
        if stripped.lower().startswith("subject:"):
            subject = stripped.split(":", 1)[-1].strip()
            continue
        if not subject:
            kube_subject = re.search(r"(Kube[A-Za-z0-9]+)", stripped)
            if kube_subject and re.search(r"reopen|firing|alert", stripped, re.I):
                subject = stripped
        match = re.match(r"^`?([a-zA-Z0-9_]+)`?\s*:\s*(.+)$", stripped)
        if match:
            key = match.group(1).lower()
            value = match.group(2).strip().strip("`")
            if _valid_field_value(key, value):
                fields[key] = value

    for key, value in _extract_inline_alert_fields(text).items():
        fields.setdefault(key, value)

    alertname = fields.get("alertname", "")
    if not alertname and subject:
        kube_match = re.search(r"(Kube[A-Za-z0-9]+)", subject)
        if kube_match:
            alertname = kube_match.group(1)
        elif re.search(r"crashloop", subject, re.I):
            alertname = "CrashLoopBackOff"
    if not alertname:
        return None

    return _alert_from_fields(text, subject, fields)


def _alert_from_fields(text: str, subject: str, fields: dict[str, str]) -> AlertContext:
    alertname = fields.get("alertname", "")
    if not alertname and subject:
        kube_match = re.search(r"(Kube[A-Za-z0-9]+)", subject)
        if kube_match:
            alertname = kube_match.group(1)
    return AlertContext(
        raw_text=text,
        subject=subject,
        alertname=alertname,
        namespace=fields.get("namespace", ""),
        cluster=fields.get("cluster", ""),
        status=fields.get("status", ""),
        severity=fields.get("severity", ""),
        priority=fields.get("priority", ""),
        region=fields.get("region", ""),
        product=fields.get("product", ""),
        hostname=fields.get("hostname", ""),
        dashboard=fields.get("dashboard", "") or fields.get("dashboard1", ""),
        notification_channel=fields.get("notification_channel", ""),
        alert_sre_attributes=fields.get("alert_sre_attributes", ""),
        oncall=fields.get("oncall", ""),
        fields=fields,
    )


def infer_alert_from_context(text: str, user_prompt: str = "") -> Optional[AlertContext]:
    """
    Build a minimal alert when thread text has namespace (etc.) but line-parse failed,
    or the user asks a K8s question in natural language.
    """
    fields = _extract_inline_alert_fields(text)
    prompt = (user_prompt or "").lower()
    combined = f"{text}\n{user_prompt}".lower()

    alertname = fields.get("alertname", "")
    if not alertname:
        if re.search(r"kubepodcrashloop|crashloopbackoff|crashloop", combined):
            alertname = "CrashLoopBackOff" if "backoff" in combined else "KubePodCrashLooping"
        elif re.search(r"kubepodnotready|not\s+ready", combined):
            alertname = "KubePodNotReady"
        elif re.search(r"kubedeploymentreplicas|replica", combined):
            alertname = "KubeDeploymentReplicasMismatch"

    if not alertname:
        return None

    has_target = fields.get("namespace") or fields.get("hostname") or fields.get(
        "alert_sre_attributes"
    )
    if not has_target and not re.search(r"\b(pod|namespace|deployment)\b", combined):
        return None

    fields["alertname"] = alertname
    if not fields.get("status"):
        fields["status"] = "firing"
    if (
        fields.get("hostname")
        and not fields.get("alert_sre_attributes")
        and not is_haystack_tenant(fields["hostname"])
    ):
        fields["alert_sre_attributes"] = fields["hostname"]

    return _alert_from_fields(text, "", fields)


K8S_ALERT_HANDLERS: dict[str, str] = {
    # Prometheus kube-state-metrics / classic names
    "KubePodCrashLooping": "crashloop",
    "KubePodNotReady": "not_ready",
    "KubeDeploymentReplicasMismatch": "deployments",
    "KubeContainerWaiting": "problem_pods",
    "KubeJobFailed": "problem_pods",
    # NOC-Automator / Trigmetry alertname values (no Kube prefix)
    "CrashLoopBackOff": "crashloop",
    "PodCrashLooping": "crashloop",
    "PodNotReady": "not_ready",
    "DeploymentReplicasMismatch": "deployments",
    "ContainerWaiting": "problem_pods",
    "JobFailed": "problem_pods",
}


def is_k8s_alertname(name: str) -> bool:
    """True for Kube* alerts and common NOC-Automator Kubernetes alert names."""
    if not name or name.lower() in ("and", "or", "the"):
        return False
    if name in K8S_ALERT_HANDLERS or name.startswith("Kube"):
        return True
    lower = name.lower()
    k8s_tokens = (
        "crashloop",
        "podnotready",
        "notready",
        "deployment",
        "replica",
        "container",
        "jobfailed",
        "pod",
        "kube",
        "k8s",
        "oom",
        "imagepull",
    )
    return any(token in lower for token in k8s_tokens)


def is_metrics_alert(alert: Optional[AlertContext]) -> bool:
    """Trigmetry/Haystack-style metric alert (RPM, threshold, app hostname)."""
    if alert is None:
        return False
    fields = alert.fields or {}
    if fields.get("current_value") or fields.get("threshold"):
        return True
    summary = (alert.summary or alert.raw_text or "").lower()
    if re.search(r"\b(rpm|latency|cpu|memory|metric|threshold)\b", summary):
        return True
    alert_type = (fields.get("alert_type") or "").lower()
    if alert_type == "app" and alert.hostname and not alert.pod_hint:
        return True
    return False


def is_valid_k8s_alert(alert: Optional[AlertContext]) -> bool:
    """Reject polluted parses (e.g. alertname=and from bot error text)."""
    if alert is None:
        return False
    name = (alert.alertname or "").strip()
    ns = (alert.namespace or "").strip()
    if not name or not is_k8s_alertname(name):
        return False
    if ns and ns != ".":
        return True
    # NOC alerts often use hostname instead of namespace — pod hint is enough to start
    return bool(alert.pod_hint or alert.alert_sre_attributes or alert.hostname)


def enrich_alert_from_text(alert: AlertContext, text: str) -> AlertContext:
    """Fill missing NOC fields (especially alert_sre_attributes) from raw thread text."""
    loose = _extract_inline_alert_fields(text)
    if not loose:
        return alert
    updates: dict[str, str] = {}
    if loose.get("alert_sre_attributes") and not alert.alert_sre_attributes:
        updates["alert_sre_attributes"] = loose["alert_sre_attributes"]
    if loose.get("hostname") and not alert.alert_sre_attributes:
        updates["alert_sre_attributes"] = loose["hostname"]
    if loose.get("namespace") and not alert.namespace:
        updates["namespace"] = loose["namespace"]
    if loose.get("alertname") and not alert.alertname:
        updates["alertname"] = loose["alertname"]
    if loose.get("cluster") and not alert.cluster:
        updates["cluster"] = loose["cluster"]
    if not updates:
        return alert
    merged_fields = {**alert.fields, **{k: loose[k] for k in updates if k in loose}}
    return AlertContext(
        raw_text=alert.raw_text,
        subject=alert.subject,
        alertname=updates.get("alertname", alert.alertname),
        namespace=updates.get("namespace", alert.namespace),
        cluster=updates.get("cluster", alert.cluster),
        status=alert.status or loose.get("status", ""),
        severity=alert.severity or loose.get("severity", ""),
        priority=alert.priority or loose.get("priority", ""),
        region=alert.region or loose.get("region", ""),
        product=alert.product,
        hostname=alert.hostname,
        dashboard=alert.dashboard,
        notification_channel=alert.notification_channel,
        alert_sre_attributes=updates.get("alert_sre_attributes", alert.alert_sre_attributes),
        oncall=alert.oncall or loose.get("oncall", ""),
        fields=merged_fields,
    )


def build_investigation_query(alert: AlertContext) -> str:
    """Turn an alert into a natural-language question for the kubernetes agent."""
    ns = alert.namespace or ""
    handler = K8S_ALERT_HANDLERS.get(alert.alertname, "problem_pods")
    pod = alert.pod_hint or (alert.alert_sre_attributes or "").strip().strip("`") or None
    ns_clause = f"in namespace {ns}" if ns else "across all namespaces"

    if handler == "crashloop":
        if pod:
            return (
                f"Investigate {alert.alertname}: what is wrong with pod {pod} "
                f"{ns_clause}? Show status, restart reason, and last 50 log lines."
            )
        return (
            f"Investigate {alert.alertname}: show pods in CrashLoopBackOff or "
            f"restarting {ns_clause} with reasons."
        )

    if handler == "deployments":
        return (
            f"Investigate {alert.alertname}: list deployments {ns_clause} "
            f"and highlight any that are not fully ready."
        )

    if handler == "not_ready":
        if pod:
            return (
                f"Investigate {alert.alertname}: describe pod {pod} "
                f"{ns_clause} and why it is not ready."
            )
        return (
            f"Investigate {alert.alertname}: list pods that are not ready {ns_clause}."
        )

    if pod:
        return (
            f"Investigate {alert.alertname}: what is wrong with pod {pod} "
            f"{ns_clause}? Include status and recent warning events."
        )
    return (
        f"Investigate {alert.alertname}: show unhealthy pods {ns_clause} "
        f"including CrashLoopBackOff, ImagePullBackOff, and high restart counts."
    )


def format_slack_reply(alert: AlertContext, investigation: str) -> str:
    """Format agent output for Slack mrkdwn."""
    pod = alert.pod_hint or (alert.alert_sre_attributes or "").strip()
    header = (
        f":kubernetes: *{alert.alertname}*"
        f" | namespace `{alert.namespace or 'unknown'}`"
        + (f" | pod `{pod}`" if pod else "")
        + f" | status `{alert.status or 'unknown'}`"
    )
    meta_parts: list[str] = []
    if alert.cluster:
        meta_parts.append(f"cluster: `{alert.cluster}`")
    if alert.region:
        meta_parts.append(f"region: `{alert.region}`")
    if alert.severity:
        meta_parts.append(f"severity: `{alert.severity}`")
    if alert.priority:
        meta_parts.append(f"priority: `{alert.priority}`")
    meta_line = " · ".join(dict.fromkeys(meta_parts))

    lines = [header]
    if meta_line:
        lines.append(meta_line)
    if alert.dashboard:
        lines.append(f"<{alert.dashboard}|Haystack dashboard>")
    lines.append("")
    lines.append(investigation[:12000])
    if alert.oncall.lower() == "yes":
        lines.append("\n_oncall: yes — human follow-up may be required_")
    return "\n".join(lines)
