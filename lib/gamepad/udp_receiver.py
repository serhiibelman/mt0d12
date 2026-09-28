"""Gamepad packets off UDP, as an asyncio protocol.

The event loop calls `datagram_received` for every packet the moment it lands,
so nothing has to poll the socket. Only the newest packet is kept: a driver who
has moved the stick since does not want the old position acted on, the rule
the rest of the vehicle applies to status updates and drive commands.
"""

import asyncio
import json
from typing import Any, Optional

from lib.common.formatting import print_error
from lib.gamepad.state import AxesState, ButtonsState, ControllerState

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 5005


def parse_packet(data: bytes) -> Optional[ControllerState]:
    """A `ControllerState` out of one datagram, or None if it is not one."""
    try:
        payload = json.loads(data.decode())
        return ControllerState(
            timestamp=payload["timestamp"],
            axes=AxesState(**payload["axes"]),
            buttons=ButtonsState(**payload["buttons"]),
        )
    except (ValueError, KeyError, TypeError) as e:
        # ValueError covers bad JSON and bad UTF-8; TypeError a field too many.
        print_error(f"Invalid packet: {e}")
        return None


class ControllerStateProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self._latest: Optional[ControllerState] = None
        self._arrived = asyncio.Event()
        # Loop time of the last good packet, None until the first. Stamped on
        # arrival rather than read from the packet: the laptop that stamps
        # `timestamp` need not agree with this clock.
        self.last_arrival: Optional[float] = None

    def datagram_received(self, data: bytes, addr: Any) -> None:
        state = parse_packet(data)
        if state is None:
            return
        self._latest = state
        self.last_arrival = asyncio.get_running_loop().time()
        self._arrived.set()

    async def next_state(self) -> ControllerState:
        """Wait for a packet newer than the last one taken, and take it."""
        await self._arrived.wait()
        self._arrived.clear()
        state, self._latest = self._latest, None
        assert state is not None
        return state


async def open_receiver(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> tuple[asyncio.DatagramTransport, ControllerStateProtocol]:
    """Bind the socket; close the returned transport to release it."""
    loop = asyncio.get_running_loop()
    return await loop.create_datagram_endpoint(ControllerStateProtocol, local_addr=(host, port))


if __name__ == "__main__":

    async def _print_packets() -> None:
        transport, protocol = await open_receiver()
        try:
            while True:
                print("receive:", await protocol.next_state())
        finally:
            transport.close()

    asyncio.run(_print_packets())
