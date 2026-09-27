"""Plumbing for routes that stream to a viewer until they leave.

A route decides what to stream; the functions here do the sending and notice
the viewer going away, so the route itself stays a few lines long.

- `forward_until_disconnect`: WebSocket, messages from a queue (/ws/status).
- `mjpeg_parts`: HTTP multipart, JPEG frames from the camera (/camera/stream).
"""

import asyncio
from collections.abc import AsyncIterator

from fastapi import Request, WebSocket
from fastapi.concurrency import run_in_threadpool

from apps.api.exceptions import ViewerLeft
from apps.api.services.camera import CameraService

# Separates the JPEGs in a multipart/x-mixed-replace body; the route puts the
# same value in the Content-Type header.
MJPEG_BOUNDARY = "FRAME"


async def forward_until_disconnect(websocket: WebSocket, updates: asyncio.Queue[str]) -> None:
    """Send every message from `updates` to the viewer until they disconnect.

    Two things wait at once - the next update, and the viewer's disconnect - so
    each gets a task, under a TaskGroup: when either ends with an exception the
    group cancels the other. The disconnect ends it quietly; anything else, such
    as a send that failed for a real reason, propagates.
    """
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_forward(websocket, updates))
            tasks.create_task(_listen_until_disconnect(websocket))
    except* ViewerLeft:
        pass


async def _forward(websocket: WebSocket, updates: asyncio.Queue[str]) -> None:
    while True:
        await websocket.send_text(await updates.get())


async def _listen_until_disconnect(websocket: WebSocket) -> None:
    # A closing browser sends a disconnect message, and listening is how it is
    # noticed at once rather than on the next failed send. Anything else the
    # viewer sends is ignored for now; the producer sets the pace, so a chatty
    # client cannot speed the stream up.
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise ViewerLeft


async def mjpeg_parts(request: Request, service: CameraService) -> AsyncIterator[bytes]:
    """Yield MJPEG parts until the camera stops or the viewer goes away.

    The frame wait is blocking, so it runs in a worker thread; the generator
    itself stays async so a disconnect reliably reaches the ``finally`` and
    hands the viewer slot back.
    """
    try:
        last_seq = -1
        while not await request.is_disconnected():
            try:
                result = await run_in_threadpool(service.next_frame, last_seq)
            except RuntimeError:
                # The camera died mid-stream; /camera/status carries the reason.
                break
            if result is None:
                break
            last_seq, frame = result
            yield (
                f"--{MJPEG_BOUNDARY}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame)}\r\n\r\n"
            ).encode() + frame + b"\r\n"
    finally:
        service.release_client_slot()
