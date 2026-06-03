"""
HTTP webhook API for n8n — Slack @mention → n8n → this API → investigation result → n8n → Slack.

n8n typically calls:
  POST /investigate
  Body: { "text": "<thread or alert body>", "channel": "...", "thread_ts": "..." }

Optional: set post_to_slack=true to reply in-thread using SLACK_BOT_TOKEN.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from master.alert_parser import AlertContext, parse_alert_message
from master.master_client import MasterAgentClient
from master.slack_thread import build_query
from master.slack_thread import extract_user_prompt

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="A2A Ops Webhook (n8n)",
    description="Investigate K8s alerts — called from n8n after Slack @mention",
    version="1.0.0",
)

_orchestrator = MasterAgentClient()


class InvestigateRequest(BaseModel):
    """Payload from n8n (map Slack trigger fields into this)."""

    text: str = Field(
        description="Alert body and/or @mention text. Include full thread context if possible."
    )
    channel: Optional[str] = Field(default=None, description="Slack channel ID")
    thread_ts: Optional[str] = Field(default=None, description="Slack thread timestamp")
    user_prompt: Optional[str] = Field(
        default=None,
        description="Extra instructions from the user (e.g. 'investigate this')"
    )
    post_to_slack: bool = Field(
        default=False,
        description="If true and SLACK_BOT_TOKEN is set, post formatted reply to thread",
    )
    session_id: Optional[str] = Field(default=None, description="Optional A2A session id")


class InvestigateResponse(BaseModel):
    ok: bool
    alertname: Optional[str] = None
    namespace: Optional[str] = None
    pod_hint: Optional[str] = None
    routed_agent: Optional[str] = None
    query: Optional[str] = None
    answer: str
    slack_text: Optional[str] = None
    posted_to_slack: bool = False
    error: Optional[str] = None


def _check_api_key(authorization: Optional[str], x_api_key: Optional[str]) -> None:
    expected = os.environ.get("WEBHOOK_API_KEY", "").strip()
    if not expected:
        return
    token = ""
    if x_api_key:
        token = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _build_query_from_request(
    alert: AlertContext, user_prompt: str | None, mention_text: str
) -> str:
    extra = user_prompt or extract_user_prompt(mention_text) or ""
    return build_query(alert, extra)


def _post_slack_reply(channel: str, thread_ts: str, text: str) -> None:
    from slack_sdk import WebClient

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    client = WebClient(token=token)
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "service": "a2a-webhook-api",
        "agents": _orchestrator.list_agents(),
        "master": "router",
    }


@app.post("/investigate", response_model=InvestigateResponse)
def investigate(
    body: InvestigateRequest,
    authorization: Optional[str] = Header(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> InvestigateResponse:
    """
    Main endpoint for n8n.

    Map Slack trigger output in n8n:
      - text  ← thread messages or alert body (concatenate in n8n if needed)
      - channel, thread_ts ← from Slack event
    """
    _check_api_key(authorization, x_api_key)

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    alert = parse_alert_message(text)
    if alert is None:
        return InvestigateResponse(
            ok=False,
            answer="",
            error=(
                "Could not parse alert. Ensure text includes alertname: and namespace: "
                "(Trigmetry / NOC-Automator format)."
            ),
        )

    query = _build_query_from_request(alert, body.user_prompt, text)
    session = body.session_id or (
        f"n8n-{body.channel}-{body.thread_ts}" if body.channel and body.thread_ts else None
    )

    try:
        result = _orchestrator.investigate(
            query,
            alert=alert,
            session_id=session,
            extra_metadata={"source": "n8n"},
        )
        extra = body.user_prompt or extract_user_prompt(text) or ""
        slack_text = _orchestrator.format_reply(alert, result, user_prompt=extra)
        posted = False

        if body.post_to_slack and body.channel and body.thread_ts:
            _post_slack_reply(body.channel, body.thread_ts, slack_text)
            posted = True

        return InvestigateResponse(
            ok=True,
            alertname=alert.alertname,
            namespace=alert.namespace,
            pod_hint=alert.pod_hint,
            routed_agent=result.agent.name,
            query=query,
            answer=result.answer,
            slack_text=slack_text,
            posted_to_slack=posted,
        )
    except Exception as exc:
        logger.exception("Investigation failed")
        return InvestigateResponse(ok=False, answer="", error=str(exc))


def main() -> None:
    import uvicorn

    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBHOOK_PORT", "8090"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
