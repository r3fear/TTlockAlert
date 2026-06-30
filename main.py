import asyncio
import logging
import os
import sys

import yaml

from health_monitor import HealthMonitor
from ttlock_monitor import TTLockMonitor
from wa_sender import WhatsAppSender

_LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(logs_dir: str) -> None:
    os.makedirs(logs_dir, exist_ok=True)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)

    file_handler = logging.FileHandler(
        os.path.join(logs_dir, "ttlock-alert.log"), encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    if not os.path.isfile(config_path):
        print("ERROR: config.yaml not found — copy config.yaml.example and fill in your values")
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def main() -> None:
    print("================================================")
    print("  TTLock Alert iniciando...")
    print("================================================")

    config = load_config()
    setup_logging("logs")
    logger = logging.getLogger("main")
    logger.info("Configuration loaded")

    wa = WhatsAppSender(config)

    alive = wa.is_gateway_alive()
    if alive:
        logger.info("wa-gateway: connected")
        wa.register_consumer()
    else:
        logger.warning("wa-gateway: not reachable at startup — notifications will fall back to email")

    # Defined before TTLockMonitor so the closure is set up before any event fires.
    # health is assigned after on_ttlock_event is defined; the closure captures it
    # by reference so it will be fully initialized by the time any event arrives.
    async def on_ttlock_event(message: str, image_path: str, priority: str, event: dict) -> None:
        if message:
            if priority == "critico":
                # "critico" events are never silenceable
                wa.send_alert(message, image_path)
            else:
                if health.is_silenced():
                    logger.info("Alert suppressed (silenced): priority=%s", priority)
                else:
                    wa.send_alert(message, image_path)
        health.register_event(event)

    ttlock = TTLockMonitor(config, on_ttlock_event)
    health = HealthMonitor(config, wa, ttlock)

    async def poll_inbox() -> None:
        logger.info("Inbox polling started (every 5s)")
        while True:
            messages = wa.poll_inbox()
            for msg in messages:
                body = msg.get("body", "")
                sender = msg.get("from", "")
                if body and sender:
                    await health.process_command(body, sender)
            await asyncio.sleep(5)

    logger.info("All services starting")
    try:
        await asyncio.gather(
            ttlock.start(),
            health.start(),
            poll_inbox(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        ttlock.stop()
        health.stop()
        logger.info("TTLock Alert stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
