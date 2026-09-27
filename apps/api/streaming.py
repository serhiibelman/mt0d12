"""Plumbing for WebSocket routes that push updates from a queue.

A route decides what to stream; `forward_until_disconnect` does the part every
such route shares - sending while listening for the viewer to leave - so the
route itself stays a few lines long.
"""

import asyncio

from fastapi import WebSocket

from apps.api.exceptions import ViewerLeft


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
