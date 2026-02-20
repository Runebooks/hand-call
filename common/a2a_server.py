"""
A2A Server — Base class for all A2A-compatible agents.

This module provides the HTTP server skeleton that handles all A2A protocol
plumbing: serving the Agent Card, receiving tasks, returning artifacts,
streaming via SSE, and error handling.

Each specialized agent (Prometheus, RDS, K8s) inherits from A2AServer
and only implements the `process_task()` method with its own logic.

Usage:
    class PrometheusAgent(A2AServer):
        async def process_task(self, task: Task) -> Task:
            # Your logic here — query Prometheus, format result
            task.add_artifact(Artifact.text("CPU: 72%"))
            task.mark_completed()
            return task

    agent = PrometheusAgent(
        agent_card_path="agents/prometheus/agent_card.json",
        host="0.0.0.0",
        port=8080,
    )
    agent.run()
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sse_starlette.sse import EventSourceResponse
import uvicorn

from .models import (
    A2AErrorCode,
    AgentCard,
    Artifact,
    JSONRPCRequest,
    JSONRPCResponse,
    Message,
    Task,
    TaskCancelParams,
    TaskQueryParams,
    TaskSendParams,
    TaskState,
)

logger = logging.getLogger(__name__)


class A2AServer(ABC):
    """
    Base A2A protocol server.

    Handles:
    - GET  /.well-known/agent.json      → Serve Agent Card (discovery)
    - POST /                             → JSON-RPC endpoint for all task operations
    - GET  /health                       → Health check

    Supported JSON-RPC methods:
    - tasks/send          → Send a task and get result synchronously
    - tasks/sendSubscribe → Send a task and stream results via SSE
    - tasks/get           → Get status of an existing task
    - tasks/cancel        → Cancel a running task

    Subclasses MUST implement:
    - process_task(task)  → Execute the task and return results

    Subclasses MAY override:
    - process_task_stream(task) → Stream partial results (for SSE support)
    - on_startup()              → Run setup logic when server starts
    - on_shutdown()             → Run cleanup logic when server stops
    """

    def __init__(
        self,
        agent_card_path: str,
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.host = host
        self.port = port

        # Load and validate the Agent Card
        self.agent_card = self._load_agent_card(agent_card_path)

        # In-memory task store (task_id -> Task)
        # In production, replace with Redis or a database
        self._tasks: dict[str, Task] = {}

        # Session store (session_id -> list of task_ids)
        # Tracks conversation history per session
        self._sessions: dict[str, list[str]] = {}

        # Build the FastAPI app
        self.app = self._build_app()

    # ------------------------------------------------------------------
    # Abstract method — each agent MUST implement this
    # ------------------------------------------------------------------

    @abstractmethod
    async def process_task(self, task: Task) -> Task:
        """
        Process a task and return results.

        This is the ONLY method each agent needs to implement.

        Steps:
        1. Read the user's message from task.message.get_text()
        2. Do your work (query Prometheus, run SQL, inspect K8s, etc.)
        3. Add results via task.add_artifact(Artifact.text("your result"))
        4. Set final status via task.mark_completed() or task.mark_failed()
        5. Return the task

        Args:
            task: The Task containing the user's message and metadata.

        Returns:
            The same Task, with artifacts and status updated.
        """
        ...

    # ------------------------------------------------------------------
    # Optional overrides — agents CAN implement these
    # ------------------------------------------------------------------

    async def process_task_stream(
        self, task: Task
    ) -> AsyncGenerator[Task, None]:
        """
        Process a task and yield partial results for streaming (SSE).

        Override this to support streaming responses. Each yield sends
        a Server-Sent Event to the client with the current task state.

        Default implementation: calls process_task() and yields once.

        Args:
            task: The Task containing the user's message.

        Yields:
            The Task with progressively updated artifacts/status.
        """
        result = await self.process_task(task)
        yield result

    async def on_startup(self) -> None:
        """Called when the server starts. Override for setup logic."""
        pass

    async def on_shutdown(self) -> None:
        """Called when the server stops. Override for cleanup logic."""
        pass

    # ------------------------------------------------------------------
    # Agent Card loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_agent_card(path: str) -> AgentCard:
        """Load and validate the Agent Card from a JSON file."""
        card_path = Path(path)
        if not card_path.exists():
            raise FileNotFoundError(
                f"Agent Card not found at: {card_path.absolute()}"
            )

        with open(card_path) as f:
            data = json.load(f)

        card = AgentCard(**data)
        logger.info(
            "Loaded Agent Card: name=%s, skills=%d, url=%s",
            card.name,
            len(card.skills),
            card.url,
        )
        return card

    # ------------------------------------------------------------------
    # FastAPI app construction
    # ------------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        """Create the FastAPI application with all A2A routes."""

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # Startup
            logger.info(
                "Starting A2A agent: %s on %s:%d",
                self.agent_card.name,
                self.host,
                self.port,
            )
            await self.on_startup()
            yield
            # Shutdown
            logger.info("Shutting down A2A agent: %s", self.agent_card.name)
            await self.on_shutdown()

        app = FastAPI(
            title=f"A2A Agent: {self.agent_card.name}",
            description=self.agent_card.description,
            version=self.agent_card.version,
            lifespan=lifespan,
        )

        # CORS — allow master agent to call from any origin
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # ---- Routes ----

        @app.get("/.well-known/agent.json")
        async def get_agent_card():
            """Serve the Agent Card for discovery."""
            return self.agent_card.model_dump()

        @app.get("/health")
        async def health_check():
            """Simple health check endpoint."""
            return {
                "status": "healthy",
                "agent": self.agent_card.name,
                "version": self.agent_card.version,
            }

        @app.post("/")
        async def jsonrpc_endpoint(request: Request):
            """
            Main JSON-RPC endpoint.

            All A2A operations go through this single endpoint.
            The 'method' field in the JSON-RPC request determines
            which operation to execute.
            """
            try:
                body = await request.json()
                rpc_request = JSONRPCRequest(**body)
            except Exception as e:
                logger.error("Failed to parse JSON-RPC request: %s", e)
                return JSONResponse(
                    content=JSONRPCResponse.fail(
                        id=None,
                        code=A2AErrorCode.PARSE_ERROR,
                        message=f"Failed to parse request: {str(e)}",
                    ).model_dump(),
                    status_code=200,  # JSON-RPC always returns 200
                )

            return await self._dispatch_rpc(rpc_request)

        return app

    # ------------------------------------------------------------------
    # JSON-RPC method dispatcher
    # ------------------------------------------------------------------

    async def _dispatch_rpc(
        self, request: JSONRPCRequest
    ) -> Response:
        """Route a JSON-RPC request to the appropriate handler."""

        handlers = {
            "tasks/send": self._handle_task_send,
            "tasks/sendSubscribe": self._handle_task_send_subscribe,
            "tasks/get": self._handle_task_get,
            "tasks/cancel": self._handle_task_cancel,
        }

        handler = handlers.get(request.method)
        if handler is None:
            logger.warning("Unknown method: %s", request.method)
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.METHOD_NOT_FOUND,
                    message=f"Unknown method: {request.method}",
                ).model_dump()
            )

        try:
            return await handler(request)
        except Exception as e:
            logger.error(
                "Error handling %s: %s\n%s",
                request.method,
                e,
                traceback.format_exc(),
            )
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.INTERNAL_ERROR,
                    message=f"Internal error: {str(e)}",
                ).model_dump()
            )

    # ------------------------------------------------------------------
    # Handler: tasks/send (synchronous request-response)
    # ------------------------------------------------------------------

    async def _handle_task_send(
        self, request: JSONRPCRequest
    ) -> JSONResponse:
        """
        Handle tasks/send — synchronous task execution.

        1. Parse the task parameters
        2. Create a Task object
        3. Call process_task() (the agent's implementation)
        4. Return the completed task with artifacts
        """
        params = TaskSendParams(**request.params)

        # Create the Task
        task = Task(
            id=params.id,
            session_id=params.session_id or params.id,
            message=params.message,
            metadata=params.metadata,
        )

        # Store it
        self._store_task(task)

        logger.info(
            "Task received: id=%s, session=%s, message=%s",
            task.id,
            task.session_id,
            task.message.get_text()[:100] if task.message else "None",
        )

        # Mark as working
        task.mark_working("Processing request")
        self._store_task(task)

        # Execute the agent's logic
        try:
            task = await self.process_task(task)
        except Exception as e:
            logger.error("Agent process_task failed: %s", e)
            task.mark_failed(f"Agent error: {str(e)}")

        # Ensure task has a terminal status
        if task.status.state == TaskState.WORKING:
            task.mark_completed()

        self._store_task(task)

        logger.info(
            "Task completed: id=%s, status=%s, artifacts=%d",
            task.id,
            task.status.state.value,
            len(task.artifacts),
        )

        return JSONResponse(
            content=JSONRPCResponse.success(
                id=request.id,
                result=task.model_dump(),
            ).model_dump()
        )

    # ------------------------------------------------------------------
    # Handler: tasks/sendSubscribe (streaming via SSE)
    # ------------------------------------------------------------------

    async def _handle_task_send_subscribe(
        self, request: JSONRPCRequest
    ) -> EventSourceResponse:
        """
        Handle tasks/sendSubscribe — streaming task execution via SSE.

        Uses Server-Sent Events to stream partial results back to the
        client as the agent processes the task.
        """
        params = TaskSendParams(**request.params)

        task = Task(
            id=params.id,
            session_id=params.session_id or params.id,
            message=params.message,
            metadata=params.metadata,
        )

        self._store_task(task)
        task.mark_working("Processing request (streaming)")
        self._store_task(task)

        logger.info(
            "Streaming task received: id=%s, message=%s",
            task.id,
            task.message.get_text()[:100] if task.message else "None",
        )

        async def event_generator():
            """Yield SSE events as the agent produces results."""
            try:
                async for updated_task in self.process_task_stream(task):
                    self._store_task(updated_task)
                    yield {
                        "event": "task_update",
                        "data": json.dumps(
                            JSONRPCResponse.success(
                                id=request.id,
                                result=updated_task.model_dump(),
                            ).model_dump(),
                            default=str,
                        ),
                    }

                # Send final event
                final_task = self._tasks.get(task.id, task)
                if final_task.status.state == TaskState.WORKING:
                    final_task.mark_completed()
                    self._store_task(final_task)

                yield {
                    "event": "task_complete",
                    "data": json.dumps(
                        JSONRPCResponse.success(
                            id=request.id,
                            result=final_task.model_dump(),
                        ).model_dump(),
                        default=str,
                    ),
                }
            except Exception as e:
                logger.error("Streaming error: %s", e)
                task.mark_failed(f"Streaming error: {str(e)}")
                self._store_task(task)
                yield {
                    "event": "task_error",
                    "data": json.dumps(
                        JSONRPCResponse.fail(
                            id=request.id,
                            code=A2AErrorCode.INTERNAL_ERROR,
                            message=str(e),
                        ).model_dump(),
                        default=str,
                    ),
                }

        return EventSourceResponse(event_generator())

    # ------------------------------------------------------------------
    # Handler: tasks/get (check task status)
    # ------------------------------------------------------------------

    async def _handle_task_get(
        self, request: JSONRPCRequest
    ) -> JSONResponse:
        """
        Handle tasks/get — retrieve the current state of a task.

        Used by the client to poll for status on long-running tasks.
        """
        params = TaskQueryParams(**request.params)
        task = self._tasks.get(params.id)

        if task is None:
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.TASK_NOT_FOUND,
                    message=f"Task not found: {params.id}",
                ).model_dump()
            )

        return JSONResponse(
            content=JSONRPCResponse.success(
                id=request.id,
                result=task.model_dump(),
            ).model_dump()
        )

    # ------------------------------------------------------------------
    # Handler: tasks/cancel (cancel a running task)
    # ------------------------------------------------------------------

    async def _handle_task_cancel(
        self, request: JSONRPCRequest
    ) -> JSONResponse:
        """
        Handle tasks/cancel — cancel a running task.

        Only tasks in SUBMITTED or WORKING state can be canceled.
        """
        params = TaskCancelParams(**request.params)
        task = self._tasks.get(params.id)

        if task is None:
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.TASK_NOT_FOUND,
                    message=f"Task not found: {params.id}",
                ).model_dump()
            )

        # Can only cancel tasks that are still running
        if task.status.state not in (TaskState.SUBMITTED, TaskState.WORKING):
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.TASK_NOT_CANCELABLE,
                    message=(
                        f"Task {params.id} cannot be canceled — "
                        f"current state: {task.status.state.value}"
                    ),
                ).model_dump()
            )

        task.mark_canceled(params.message or "Canceled by client")
        self._store_task(task)

        logger.info("Task canceled: id=%s", task.id)

        return JSONResponse(
            content=JSONRPCResponse.success(
                id=request.id,
                result=task.model_dump(),
            ).model_dump()
        )

    # ------------------------------------------------------------------
    # Task storage helpers
    # ------------------------------------------------------------------

    def _store_task(self, task: Task) -> None:
        """Store a task and update session tracking."""
        self._tasks[task.id] = task

        if task.session_id not in self._sessions:
            self._sessions[task.session_id] = []

        if task.id not in self._sessions[task.session_id]:
            self._sessions[task.session_id].append(task.id)

    def get_session_history(self, session_id: str) -> list[Task]:
        """
        Get all tasks in a session, ordered by creation time.

        Useful for agents that need conversation context
        (e.g., follow-up questions).
        """
        task_ids = self._sessions.get(session_id, [])
        tasks = [self._tasks[tid] for tid in task_ids if tid in self._tasks]
        return sorted(tasks, key=lambda t: t.created_at)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get a specific task by ID."""
        return self._tasks.get(task_id)

    # ------------------------------------------------------------------
    # Run the server
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the A2A server (blocking)."""
        logger.info(
            "Starting %s on %s:%d",
            self.agent_card.name,
            self.host,
            self.port,
        )
        uvicorn.run(
            self.app,
            host=self.host,
            port=self.port,
            log_level="info",
        )

    async def run_async(self) -> None:
        """Start the A2A server (async, for running in an event loop)."""
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await server.serve()

