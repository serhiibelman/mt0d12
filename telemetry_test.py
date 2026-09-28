"""One-shot check that this machine can publish to AWS IoT Core.

Run on the Pi from the project root, before starting the API:

    python telemetry_test.py

It touches no motors and no camera - it only resolves the configuration,
reports what is missing, and publishes a single test message.

Spooling is switched off here on purpose: this asks "can this machine reach
IoT Core right now", so a failure has to be a failure, not a message parked in
the vehicle's spool for the API to deliver later.
"""

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from lib.common.formatting import print_error, print_info, print_success, print_warning
from lib.telemetry import TelemetryConfig, TelemetryPublisher

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def check_file(label: str, path: str) -> bool:
    if not path:
        print_error(f"  {label}: not set")
        return False
    if not Path(path).is_file():
        print_error(f"  {label}: {path} (file not found)")
        return False
    print_success(f"  {label}: {path}")
    return True


def main() -> int:
    config = replace(TelemetryConfig(), spool_path="")

    print_info("Configuration (from .env in the current directory)")
    if config.endpoint:
        print_success(f"  endpoint: {config.endpoint}:{config.port}")
    else:
        print_error("  endpoint: not set (IOT_ENDPOINT)")
    print_info(f"  client id: {config.client_id or config.thing_name}")
    print_info(f"  topic:     {config.resolved_topic}")

    files_ok = all(
        [
            check_file("certificate", config.cert_path),
            check_file("private key", config.key_path),
            check_file("root CA", config.root_ca_path),
        ]
    )

    publisher = TelemetryPublisher(snapshot=_test_snapshot, config=config)
    if not publisher.configured or not files_ok:
        print_error("\nNot configured - nothing would be published.")
        print_warning("Set IOT_ENDPOINT, IOT_CERT_PATH and IOT_KEY_PATH in .env,")
        print_warning("and run this from the project root so .env is found.")
        return 1

    print_info(f"\nPublishing one message to {config.resolved_topic} ...")
    sent = asyncio.run(_publish_one(publisher))
    if not sent:
        print_error("Publish failed - see the warning above for the reason.")
        print_warning("Common causes: the certificate is not attached to the")
        print_warning("thing or policy, the endpoint is wrong, or the clock is off.")
        return 1

    print_success("Published. It should appear in the IoT Core MQTT test client")
    print_success(f"if you are subscribed to {config.resolved_topic} or rover/#")
    return 0


async def _publish_one(publisher: TelemetryPublisher) -> bool:
    try:
        return await publisher.publish_once(trigger="test")
    finally:
        await publisher.stop()


def _test_snapshot() -> dict:
    return {
        "service": "mt0d12-vehicle-api",
        "overall_status": "test",
        "timestamp": datetime.now(timezone.utc),
        "detail": "telemetry_test.py",
    }


if __name__ == "__main__":
    raise SystemExit(main())
