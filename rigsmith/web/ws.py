"""Live updates over WebSocket.

The UI needs two things pushed rather than polled: background job progress
(separation runs for minutes) and device state. Both go down one socket as
JSON messages with a ``type`` field.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, WebSocket
from starlette.websockets import WebSocketDisconnect

from ..core.jobs import get_manager

#: How often to re-send job state. Jobs push their own events, but a poll
#: also catches jobs started from the CLI or an agent while the page is open.
POLL_SECONDS = 1.0


def install_websocket(app: FastAPI) -> None:
    """Attach the live-update socket to the app."""

    @app.websocket("/ws")
    async def live(websocket: WebSocket) -> None:
        await websocket.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def on_event(event) -> None:
            # Job events arrive on a worker thread; hand them to the loop.
            loop.call_soon_threadsafe(
                queue.put_nowait, {"type": "job", **event.to_dict()}
            )

        manager = get_manager()
        unsubscribes = [job.listen(on_event) for job in manager.list_jobs()]

        async def pump_queue() -> None:
            while True:
                message = await queue.get()
                await websocket.send_text(json.dumps(message))

        async def poll_jobs() -> None:
            known: set[str] = {j.id for j in manager.list_jobs()}
            while True:
                await asyncio.sleep(POLL_SECONDS)
                jobs = manager.list_jobs()
                for job in jobs:
                    if job.id not in known:
                        known.add(job.id)
                        unsubscribes.append(job.listen(on_event))
                await websocket.send_text(
                    json.dumps(
                        {"type": "jobs", "jobs": [j.to_dict() for j in jobs]}
                    )
                )

        async def read_client() -> None:
            # The client sends pings; nothing needs acting on yet, but reading
            # is how a closed socket is noticed. A disconnect here is the
            # normal way this ends, so it returns rather than raising - an
            # exception would propagate out of the endpoint at teardown and
            # be logged as a server error for an ordinary page close.
            try:
                while True:
                    await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                return

        tasks = [
            asyncio.create_task(pump_queue()),
            asyncio.create_task(poll_jobs()),
            asyncio.create_task(read_client()),
        ]
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "jobs",
                        "jobs": [j.to_dict() for j in manager.list_jobs()],
                    }
                )
            )
            # Whichever finishes first ends the session - normally the reader,
            # when the page closes.
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            # return_exceptions keeps a task that already failed from
            # re-raising out of the endpoint while we are cleaning up.
            await asyncio.gather(*tasks, return_exceptions=True)
            for unsubscribe in unsubscribes:
                unsubscribe()
