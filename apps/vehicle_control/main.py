import asyncio

from lib.gamepad.udp_receiver import open_receiver
from lib.ddsm115 import DDS115
from apps.vehicle_control.vehicle_controller import VehicleController
from lib.common.formatting import print_error, print_info


async def drive(motor: DDS115) -> None:
    transport, packets = await open_receiver()
    try:
        await VehicleController(motor=motor).run(packets)
    finally:
        transport.close()


def main():
    try:
        motor = DDS115()
    except RuntimeError as e:
        print_error(e)
        return

    try:
        # Ctrl+C cancels `drive`, whose `finally` stops the motors before
        # asyncio.run re-raises the KeyboardInterrupt here.
        asyncio.run(drive(motor))
    except KeyboardInterrupt:
        print_info("VehicleController stopped")
    finally:
        motor.close()


if __name__ == "__main__":
    main()
