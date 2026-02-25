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
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
import uvicorn

from .models import (
    A2AErrorCode,
    AgentCard,
    JSONRPCRequest,
    JSONRPCResponse,
    Task,
    TaskCancelParams,
    TaskQueryParams,
    TaskSendParams,
    TaskState,
)

logger = logging.getLogger(__name__)


class A2AServer(ABC):

    def __init__(
        self,
        agent_card_path: str,
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.host = host
        self.port = port

        self.agent_card = self._load_agent_card(agent_card_path)

        self._tasks: dict[str, Task] = {}
        self._sessions: dict[str, list[str]] = {}

        self.app = self._build_app()

    # ------------------------------------------------------------------
    # MUST IMPLEMENT
    # ------------------------------------------------------------------

    @abstractmethod
    async def process_task(self, task: Task) -> Task:
        ...

    # ------------------------------------------------------------------
    # OPTIONAL
    # ------------------------------------------------------------------

    async def process_task_stream(
        self, task: Task
    ) -> AsyncGenerator[Task, None]:
        result = await self.process_task(task)
        yield result

    async def on_startup(self) -> None:
        pass

    async def on_shutdown(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Agent Card
    # ------------------------------------------------------------------

    @staticmethod
    def _load_agent_card(path: str) -> AgentCard:
        card_path = Path(path)
        if not card_path.exists():
            raise FileNotFoundError(f"Agent Card not found: {card_path}")

        with open(card_path) as f:
            data = json.load(f)

        return AgentCard(**data)

    # ------------------------------------------------------------------
    # FastAPI App
    # ------------------------------------------------------------------

    def _build_app(self) -> FastAPI:

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            await self.on_startup()
            yield
            await self.on_shutdown()

        app = FastAPI(lifespan=lifespan)

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/.well-known/agent.json")
        async def get_agent_card():
            return self.agent_card.model_dump(mode="json")

        @app.get("/health")
        async def health():
            return {
                "status": "healthy",
                "agent": self.agent_card.name,
                "version": self.agent_card.version,
            }

        @app.post("/")
        async def jsonrpc_endpoint(request: Request):
            try:
                body = await request.json()
                rpc_request = JSONRPCRequest(**body)
            except Exception as e:
                return JSONResponse(
                    content=JSONRPCResponse.fail(
                        id=None,
                        code=A2AErrorCode.PARSE_ERROR,
                        message=str(e),
                    ).model_dump(mode="json"),
                    status_code=200,
                )

            return await self._dispatch_rpc(rpc_request)

        return app

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    async def _dispatch_rpc(self, request: JSONRPCRequest) -> Response:

        handlers = {
            "tasks/send": self._handle_task_send,
            "tasks/sendSubscribe": self._handle_task_send_subscribe,
            "tasks/get": self._handle_task_get,
            "tasks/cancel": self._handle_task_cancel,
        }

        handler = handlers.get(request.method)
        if handler is None:
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.METHOD_NOT_FOUND,
                    message=f"Unknown method: {request.method}",
                ).model_dump(mode="json")
            )

        try:
            return await handler(request)
        except Exception as e:
            logger.error(traceback.format_exc())
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.INTERNAL_ERROR,
                    message=str(e),
                ).model_dump(mode="json")
            )

    # ------------------------------------------------------------------
    # tasks/send
    # ------------------------------------------------------------------

    async def _handle_task_send(
        self, request: JSONRPCRequest
    ) -> JSONResponse:

        params = TaskSendParams(**request.params)

        task = Task(
            id=params.id,
            session_id=params.session_id or params.id,
            message=params.message,
            metadata=params.metadata,
        )

        self._store_task(task)

        task.mark_working("Processing request")
        self._store_task(task)

        try:
            task = await self.process_task(task)
        except Exception as e:
            task.mark_failed(str(e))

        if task.status.state == TaskState.WORKING:
            task.mark_completed()

        self._store_task(task)

        return JSONResponse(
            content=JSONRPCResponse.success(
                id=request.id,
                result=task.model_dump(mode="json"),
            ).model_dump(mode="json")
        )

    # ------------------------------------------------------------------
    # tasks/sendSubscribe (Streaming)
    # ------------------------------------------------------------------

    async def _handle_task_send_subscribe(
        self, request: JSONRPCRequest
    ) -> EventSourceResponse:

        params = TaskSendParams(**request.params)

        task = Task(
            id=params.id,
            session_id=params.session_id or params.id,
            message=params.message,
            metadata=params.metadata,
        )

        self._store_task(task)
        task.mark_working("Processing (stream)")
        self._store_task(task)

        async def event_generator():
            try:
                async for updated in self.process_task_stream(task):
                    self._store_task(updated)

                    yield {
                        "event": "task_update",
                        "data": json.dumps(
                            JSONRPCResponse.success(
                                id=request.id,
                                result=updated.model_dump(mode="json"),
                            ).model_dump(mode="json")
                        ),
                    }

                final = self._tasks.get(task.id, task)

                if final.status.state == TaskState.WORKING:
                    final.mark_completed()
                    self._store_task(final)

                yield {
                    "event": "task_complete",
                    "data": json.dumps(
                        JSONRPCResponse.success(
                            id=request.id,
                            result=final.model_dump(mode="json"),
                        ).model_dump(mode="json")
                    ),
                }

            except Exception as e:
                task.mark_failed(str(e))
                self._store_task(task)

                yield {
                    "event": "task_error",
                    "data": json.dumps(
                        JSONRPCResponse.fail(
                            id=request.id,
                            code=A2AErrorCode.INTERNAL_ERROR,
                            message=str(e),
                        ).model_dump(mode="json")
                    ),
                }

        return EventSourceResponse(event_generator())

    # ------------------------------------------------------------------
    # tasks/get
    # ------------------------------------------------------------------

    async def _handle_task_get(
        self, request: JSONRPCRequest
    ) -> JSONResponse:

        params = TaskQueryParams(**request.params)
        task = self._tasks.get(params.id)

        if task is None:
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.TASK_NOT_FOUND,
                    message=f"Task not found: {params.id}",
                ).model_dump(mode="json")
            )

        return JSONResponse(
            content=JSONRPCResponse.success(
                id=request.id,
                result=task.model_dump(mode="json"),
            ).model_dump(mode="json")
        )

    # ------------------------------------------------------------------
    # tasks/cancel
    # ------------------------------------------------------------------

    async def _handle_task_cancel(
        self, request: JSONRPCRequest
    ) -> JSONResponse:

        params = TaskCancelParams(**request.params)
        task = self._tasks.get(params.id)

        if task is None:
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.TASK_NOT_FOUND,
                    message=f"Task not found: {params.id}",
                ).model_dump(mode="json")
            )

        if task.status.state not in (
            TaskState.SUBMITTED,
            TaskState.WORKING,
        ):
            return JSONResponse(
                content=JSONRPCResponse.fail(
                    id=request.id,
                    code=A2AErrorCode.TASK_NOT_CANCELABLE,
                    message=f"Task {params.id} cannot be canceled",
                ).model_dump(mode="json")
            )

        task.mark_canceled(params.message or "Canceled")
        self._store_task(task)

        return JSONResponse(
            content=JSONRPCResponse.success(
                id=request.id,
                result=task.model_dump(mode="json"),
            ).model_dump(mode="json")
        )

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _store_task(self, task: Task) -> None:
        self._tasks[task.id] = task

        if task.session_id not in self._sessions:
            self._sessions[task.session_id] = []

        if task.id not in self._sessions[task.session_id]:
            self._sessions[task.session_id].append(task.id)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        uvicorn.run(self.app, host=self.host, port=self.port)

    async def run_async(self) -> None:
        config = uvicorn.Config(self.app, host=self.host, port=self.port)
        server = uvicorn.Server(config)
        await server.serve()
