"""
A2A Protocol Data Models

Defines all the data structures used in the Agent-to-Agent (A2A) protocol.
These models are shared between the A2A server (agents) and A2A client (master agent).

Reference: Google A2A Protocol Specification
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Agent Card — describes what an agent can do (served at /.well-known/agent.json)
# ---------------------------------------------------------------------------


class AgentSkill(BaseModel):
    """A single capability that an agent offers."""

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentCapabilities(BaseModel):
    """What communication modes the agent supports."""

    streaming: bool = False
    push_notifications: bool = False


class AgentAuthentication(BaseModel):
    """Authentication schemes the agent accepts."""

    schemes: list[str] = Field(default_factory=lambda: ["bearer"])


class AgentCard(BaseModel):
    """
    The Agent Card — a JSON document that describes an agent's identity,
    capabilities, and skills. Hosted at /.well-known/agent.json

    This is how the master agent discovers what each agent can do.
    """

    name: str
    description: str
    url: str
    version: str = "1.0.0"
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    skills: list[AgentSkill] = Field(default_factory=list)
    authentication: AgentAuthentication = Field(
        default_factory=AgentAuthentication
    )

    def get_all_tags(self) -> set[str]:
        """Collect all tags from all skills — used for keyword routing."""
        tags = set()
        for skill in self.skills:
            tags.update(skill.tags)
        return tags

    def get_all_examples(self) -> list[str]:
        """Collect all examples from all skills — used for semantic routing."""
        examples = []
        for skill in self.skills:
            examples.extend(skill.examples)
        return examples


# ---------------------------------------------------------------------------
# Message Parts — the building blocks of messages and artifacts
# ---------------------------------------------------------------------------


class PartType(str, Enum):
    """Types of content that can be sent in a message or artifact."""

    TEXT = "text"
    FILE = "file"
    DATA = "data"


class TextPart(BaseModel):
    """A text content part."""

    type: PartType = PartType.TEXT
    text: str


class FilePart(BaseModel):
    """A file content part (inline bytes or URI reference)."""

    type: PartType = PartType.FILE
    file_name: str
    mime_type: str = "application/octet-stream"
    uri: Optional[str] = None
    data: Optional[str] = None  # base64-encoded


class DataPart(BaseModel):
    """A structured data part (JSON-serializable dict)."""

    type: PartType = PartType.DATA
    data: dict[str, Any]


# Union type for all part types
Part = TextPart | FilePart | DataPart


# ---------------------------------------------------------------------------
# Messages — what gets sent between client and agent
# ---------------------------------------------------------------------------


class MessageRole(str, Enum):
    """Who sent the message."""

    USER = "user"
    AGENT = "agent"


class Message(BaseModel):
    """
    A message in a conversation. Contains one or more Parts.

    The user sends a message to an agent, and the agent responds with
    artifacts (results). Messages maintain conversation context within
    a session.
    """

    role: MessageRole
    parts: list[Part]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def user(cls, text: str, **metadata: Any) -> Message:
        """Convenience: create a user message with a single text part."""
        return cls(
            role=MessageRole.USER,
            parts=[TextPart(text=text)],
            metadata=metadata,
        )

    @classmethod
    def agent(cls, text: str, **metadata: Any) -> Message:
        """Convenience: create an agent message with a single text part."""
        return cls(
            role=MessageRole.AGENT,
            parts=[TextPart(text=text)],
            metadata=metadata,
        )

    def get_text(self) -> str:
        """Extract all text parts joined together."""
        texts = []
        for part in self.parts:
            if isinstance(part, TextPart):
                texts.append(part.text)
        return "\n".join(texts)


# ---------------------------------------------------------------------------
# Task — the core unit of work in A2A
# ---------------------------------------------------------------------------


class TaskState(str, Enum):
    """Lifecycle states of a task."""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class TaskStatus(BaseModel):
    """Current status of a task."""

    state: TaskState
    message: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class Artifact(BaseModel):
    """
    The output/result produced by an agent for a task.

    An artifact contains one or more Parts (text, files, structured data).
    A single task can produce multiple artifacts (e.g., a text summary
    AND a CSV file).
    """

    name: Optional[str] = None
    description: Optional[str] = None
    parts: list[Part]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def text(cls, content: str, name: Optional[str] = None) -> Artifact:
        """Convenience: create an artifact with a single text part."""
        return cls(name=name, parts=[TextPart(text=content)])

    @classmethod
    def data(
        cls,
        payload: dict[str, Any],
        name: Optional[str] = None,
    ) -> Artifact:
        """Convenience: create an artifact with a single data part."""
        return cls(name=name, parts=[DataPart(data=payload)])

    def get_text(self) -> str:
        """Extract all text parts joined together."""
        texts = []
        for part in self.parts:
            if isinstance(part, TextPart):
                texts.append(part.text)
        return "\n".join(texts)


class Task(BaseModel):
    """
    The core unit of work in the A2A protocol.

    A client sends a message (the question/request) wrapped in a Task.
    The agent processes it and attaches Artifacts (results) to the Task.
    The Task tracks its lifecycle via TaskStatus.

    session_id groups multiple tasks into a conversation — the agent
    can use it to maintain context across follow-up questions.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: TaskStatus = Field(
        default_factory=lambda: TaskStatus(state=TaskState.SUBMITTED)
    )
    message: Optional[Message] = None
    artifacts: list[Artifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def mark_working(self, message: Optional[str] = None) -> None:
        self.status = TaskStatus(state=TaskState.WORKING, message=message)
        self.updated_at = datetime.utcnow()

    def mark_completed(self, message: Optional[str] = None) -> None:
        self.status = TaskStatus(state=TaskState.COMPLETED, message=message)
        self.updated_at = datetime.utcnow()

    def mark_failed(self, message: Optional[str] = None) -> None:
        self.status = TaskStatus(state=TaskState.FAILED, message=message)
        self.updated_at = datetime.utcnow()

    def mark_canceled(self, message: Optional[str] = None) -> None:
        self.status = TaskStatus(state=TaskState.CANCELED, message=message)
        self.updated_at = datetime.utcnow()

    def mark_input_required(self, message: Optional[str] = None) -> None:
        self.status = TaskStatus(
            state=TaskState.INPUT_REQUIRED, message=message
        )
        self.updated_at = datetime.utcnow()

    def add_artifact(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)
        self.updated_at = datetime.utcnow()


# ---------------------------------------------------------------------------
# JSON-RPC Request / Response wrappers (A2A uses JSON-RPC 2.0 over HTTP)
# ---------------------------------------------------------------------------


class JSONRPCRequest(BaseModel):
    """Incoming JSON-RPC 2.0 request."""

    jsonrpc: str = "2.0"
    id: Optional[str | int] = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JSONRPCError(BaseModel):
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Optional[Any] = None


class JSONRPCResponse(BaseModel):
    """Outgoing JSON-RPC 2.0 response."""

    jsonrpc: str = "2.0"
    id: Optional[str | int] = None
    result: Optional[Any] = None
    error: Optional[JSONRPCError] = None

    @classmethod
    def success(cls, id: Optional[str | int], result: Any) -> JSONRPCResponse:
        return cls(id=id, result=result)

    @classmethod
    def fail(
        cls,
        id: Optional[str | int],
        code: int,
        message: str,
        data: Optional[Any] = None,
    ) -> JSONRPCResponse:
        return cls(id=id, error=JSONRPCError(code=code, message=message, data=data))


# ---------------------------------------------------------------------------
# A2A Error Codes (standard JSON-RPC + A2A-specific)
# ---------------------------------------------------------------------------


class A2AErrorCode:
    """Standard error codes used in A2A JSON-RPC responses."""

    # JSON-RPC standard errors
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # A2A-specific errors
    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    AGENT_UNAVAILABLE = -32003
    AUTHENTICATION_REQUIRED = -32004
    SKILL_NOT_FOUND = -32005


# ---------------------------------------------------------------------------
# Convenience: Task send/receive params (what goes inside JSON-RPC params)
# ---------------------------------------------------------------------------


class TaskSendParams(BaseModel):
    """Parameters for the tasks/send and tasks/sendSubscribe methods."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: Optional[str] = None
    message: Message
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskQueryParams(BaseModel):
    """Parameters for the tasks/get method."""

    id: str


class TaskCancelParams(BaseModel):
    """Parameters for the tasks/cancel method."""

    id: str
    message: Optional[str] = None

