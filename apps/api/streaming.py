"""Plumbing for routes that stream to a viewer until they leave.

A route decides what to stream; the functions here do the sending and notice
the viewer going away, so the route itself stays a few lines long.

- `serve_until_disconnect`: WebSocket, status from a queue out and drive
  commands in (/ws/status).
- `mjpeg_parts`: HTTP multipart, JPEG frames from the camera (/camera/stream).
"""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import WebSocket

from apps.api.exceptions import ViewerLeft
from apps.api.services.camera import CameraService
from apps.api.services.drive import DriveSession, axis

# Separates the JPEGs in a multipart/x-mixed-replace body; the route puts the
# same value in the Content-Type header.
MJPEG_BOUNDARY = "FRAME"

# How long a timed-out command read looks for a command that was already there.
STALL_GRACE = 0.05


def locked_sender(websocket: WebSocket) -> Callable[[str], Awaitable[None]]:
    """`send_text`, one message at a time.

    Status and drive state go out from different tasks. One frame is one write
    today, but nothing promises that, and two frames interleaved would be one
    corrupt message; the lock makes it not matter.
    """
    lock = asyncio.Lock()

    async def send(text: str) -> None:
        async with lock:
            await websocket.send_text(text)

    return send


async def serve_until_disconnect(
    websocket: WebSocket,
    send: Callable[[str], Awaitable[None]],
    updates: asyncio.Queue[str],
    session: DriveSession,
    link_timeout: float,
) -> None:
    """Status out, drive commands in, until the viewer disconnects.

    Three things wait at once - the next update, the next command and a free
    motor bus - so each gets a task, under a TaskGroup: when any ends with an
    exception the group cancels the others. The disconnect ends it quietly;
    anything else, such as a send that failed for a real reason, propagates.
    Either way the caller's `DriveSession.close` stops the motors after.
    """
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(_forward(send, updates))
            tasks.create_task(_read_commands(websocket, session, link_timeout))
            tasks.create_task(session.run_motors())
    except* ViewerLeft:
        pass


async def _forward(send: Callable[[str], Awaitable[None]], updates: asyncio.Queue[str]) -> None:
    while True:
        await send(await updates.get())


async def _read_commands(websocket: WebSocket, session: DriveSession, link_timeout: float) -> None:
    """Read commands until the viewer disconnects, and stop on silence.

    A closing browser sends a disconnect message, and listening is how it is
    noticed at once rather than on the next failed send. A viewer that is only
    watching sends nothing and may stay quiet forever; one that is driving
    sends 20 commands a second, so `link_timeout` without one means the link
    has gone and the motors stop - `VehicleController`'s fail-safe, as a
    timeout rather than a timestamp. The page has to arm again after, so a
    link that comes back with a stick still pushed does not lurch forward.
    """
    while True:
        try:
            async with asyncio.timeout(link_timeout if session.armed else None):
                message = await websocket.receive()
        except TimeoutError:
            message = await _already_arrived(websocket)
            if message is None:
                await session.disarm(f"No command for {link_timeout:g}s - stopped")
                continue
        if message["type"] == "websocket.disconnect":
            raise ViewerLeft
        await _handle_command(session, _parse(message))


async def _already_arrived(websocket: WebSocket) -> dict[str, Any] | None:
    """A command that came in while this process was too busy to read it.

    When the event loop stalls - the camera opening takes the Pi 1's only core
    for a second or more - the timeout and the commands queued behind it come
    due together, and the timeout can win although the link is fine. A
    message already waiting is returned at once; only a real silence costs
    `STALL_GRACE` on top of the link timeout.
    """
    try:
        async with asyncio.timeout(STALL_GRACE):
            return await websocket.receive()
    except TimeoutError:
        return None


def _parse(message: dict[str, Any]) -> dict[str, Any] | None:
    text = message.get("text")
    if text is None:
        return None
    try:
        command = json.loads(text)
    except ValueError:
        return None
    return command if isinstance(command, dict) else None


async def _handle_command(session: DriveSession, command: dict[str, Any] | None) -> None:
    # Anything that is not a well-formed command is ignored, not an error: the
    # stream is for watchers too, and a stray message must not cost them it.
    # The producer sets the pace, so a chatty client cannot speed it up.
    if command is None:
        return
    kind = command.get("type")
    if kind == "arm":
        await session.arm()
    elif kind == "stop":
        await session.disarm("Stopped")
    elif kind == "drive":
        throttle, steer = axis(command.get("throttle")), axis(command.get("steer"))
        if throttle is not None and steer is not None:
            session.command(throttle, steer)


async def mjpeg_parts(service: CameraService) -> AsyncIterator[bytes]:
    """Yield MJPEG parts until the camera stops.

    The wait for a frame is a coroutine, so a viewer costs the event loop
    nothing between frames and holds no worker thread. A viewer leaving is
    Starlette's to notice: `StreamingResponse` listens for the disconnect and
    cancels this generator. The viewer slot is handed back by the route's
    background task, which runs even when the generator never started.
    """
    last_seq = -1
    while True:
        try:
            result = await service.next_frame(last_seq)
        except RuntimeError:
            # The camera died mid-stream; /camera/status carries the reason.
            return
        if result is None:
            return
        last_seq, frame = result
        yield (
            f"--{MJPEG_BOUNDARY}\r\n"
            f"Content-Type: image/jpeg\r\n"
            f"Content-Length: {len(frame)}\r\n\r\n"
        ).encode() + frame + b"\r\n"
