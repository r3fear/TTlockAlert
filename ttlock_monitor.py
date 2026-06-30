import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from camera import capture_frame, get_photo_path

logger = logging.getLogger("ttlock_monitor")

# recordType → (display name, category)
_RECORD_TYPES = {
    1:  ("App Bluetooth",     "open"),
    4:  ("Código numérico",   "open"),
    7:  ("Huella digital",    "open"),
    8:  ("Tarjeta IC",        "open"),
    9:  ("Código incorrecto", "failed"),
    10: ("Puerta forzada",    "forced"),
    11: ("Batería baja",      "battery"),
}

_OPEN_TYPES = {1, 4, 7, 8}


class TTLockMonitor:
    def __init__(self, config: dict, event_callback):
        """
        event_callback: async def callback(message: str, image_path: str, priority: str, event: dict)
        priority values: "normal" | "high" | "critical"
        event: raw event dict from Vercel relay (lockId, recordType, success, username, …)
        """
        tt = config["ttlock"]
        self._api_url = tt.get("api_url", "https://euapi.ttlock.com").rstrip("/")
        self._client_id = tt["client_id"]
        self._client_secret = tt["client_secret"]
        self._username = tt["username"]
        self._password_md5 = tt["password_md5"]
        self._lock_id = int(tt.get("lock_id", 0))
        self._lock_name = tt.get("lock_name", "Cerradura")
        self._vercel_url = tt["vercel_url"].rstrip("/")
        self._api_key = tt["api_key"]
        self._polling_interval = int(tt.get("polling_interval", 5))
        self._token_file = tt.get("token_file", "ttlock_token.cache")

        self._cam_cfg = config.get("camera", {})
        self._storage_cfg = config.get("storage", {})

        self._callback = event_callback
        self._running = False
        self._battery = -1
        self._token_data = None  # {"access_token", "refresh_token", "expires_at"}

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _load_token(self) -> bool:
        if not os.path.isfile(self._token_file):
            return False
        try:
            with open(self._token_file, encoding="utf-8") as f:
                data = json.load(f)
            if "access_token" in data and "expires_at" in data:
                self._token_data = data
                logger.debug("Token loaded from %s", self._token_file)
                return True
        except Exception as e:
            logger.warning("Could not load token file: %s", e)
        return False

    def _save_token(self, data: dict) -> None:
        try:
            with open(self._token_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
            logger.debug("Token saved to %s", self._token_file)
        except Exception as e:
            logger.warning("Could not save token file: %s", e)

    def _post_oauth(self, params: dict) -> dict:
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"{self._api_url}/oauth2/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def _fetch_token(self) -> bool:
        """Obtain a new access token via password grant."""
        try:
            data = self._post_oauth({
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "password",
                "username": self._username,
                "password": self._password_md5,
            })
            if "access_token" not in data:
                logger.error("Token fetch failed — response: %s", data)
                return False
            expires_in = int(data.get("expires_in", 7776000))
            token = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
                "expires_at": time.time() + expires_in,
            }
            self._token_data = token
            self._save_token(token)
            logger.info("New access token obtained, expires in %d days", expires_in // 86400)
            return True
        except Exception as e:
            logger.error("Token fetch error: %s", e)
            return False

    def _do_refresh(self) -> bool:
        """Refresh token via refresh_token grant. Falls back to password grant on failure."""
        if not self._token_data or not self._token_data.get("refresh_token"):
            return self._fetch_token()
        try:
            data = self._post_oauth({
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._token_data["refresh_token"],
            })
            if "access_token" not in data:
                logger.warning("Token refresh failed (%s) — re-authenticating with password", data)
                return self._fetch_token()
            expires_in = int(data.get("expires_in", 7776000))
            token = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", self._token_data["refresh_token"]),
                "expires_at": time.time() + expires_in,
            }
            self._token_data = token
            self._save_token(token)
            logger.info("Token refreshed, expires in %d days", expires_in // 86400)
            return True
        except Exception as e:
            logger.error("Token refresh error: %s — re-authenticating with password", e)
            return self._fetch_token()

    def _ensure_token(self) -> bool:
        """Guarantee a valid token, refreshing if within 24 hours of expiry."""
        if self._token_data is None:
            self._load_token()
        if self._token_data is None:
            return self._fetch_token()

        remaining = self._token_data.get("expires_at", 0) - time.time()
        if remaining < 86400:
            logger.info("Token expires in %.1fh — refreshing now", remaining / 3600)
            return self._do_refresh()
        return True

    # ------------------------------------------------------------------
    # Event polling via Vercel relay
    # ------------------------------------------------------------------

    def _poll_events(self) -> list:
        """GET /api/ttlock-events from Vercel relay. Returns event list or []."""
        url = f"{self._vercel_url}/api/ttlock-events"
        req = urllib.request.Request(url, headers={"x-api-key": self._api_key})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return data.get("events", [])
        except urllib.error.HTTPError as e:
            logger.error("Vercel relay HTTP %d: %s", e.code, e.read().decode(errors="replace"))
            return []
        except Exception as e:
            logger.debug("Vercel relay poll error: %s", e)
            return []

    # ------------------------------------------------------------------
    # Event processing
    # ------------------------------------------------------------------

    def _format_date(self, lock_date) -> str:
        """Convert TTLock millisecond timestamp to local datetime string."""
        try:
            return datetime.fromtimestamp(int(lock_date) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(lock_date)

    def _build_message(self, event: dict, type_name: str, category: str) -> str:
        username = event.get("username", "")
        keyboard_pwd = event.get("keyboardPwd", "")
        fecha = self._format_date(event.get("lockDate", 0))
        battery = self._battery
        name = self._lock_name

        if category == "open":
            lines = [f"🔓 {name} — Puerta abierta", f"Método: {type_name}"]
            if username:
                lines.append(f"Usuario: {username}")
            if keyboard_pwd:
                lines.append(f"Clave: {keyboard_pwd}")
            lines += [f"Hora: {fecha}", f"Batería: {battery}%"]

        elif category == "failed":
            lines = [f"⚠️ {name} — Intento fallido de acceso", f"Hora: {fecha}"]
            if username:
                lines.append(f"Usuario: {username}")

        elif category == "forced":
            lines = [f"🚨 ALERTA: {name} — Puerta forzada", f"Hora: {fecha}", f"Batería: {battery}%"]

        elif category == "battery":
            lines = [f"🔋 {name} — Batería baja: {battery}%", f"Hora: {fecha}"]

        else:
            lines = [f"{name} — {type_name}", f"Hora: {fecha}"]

        return "\n".join(lines)

    async def _process_event(self, event: dict) -> None:
        record_type = int(event.get("recordType", -1))
        if record_type not in _RECORD_TYPES:
            logger.debug("Ignoring unknown recordType %d", record_type)
            return

        type_name, category = _RECORD_TYPES[record_type]

        battery = event.get("electricQuantity")
        if battery is not None:
            self._battery = int(battery)

        priority = "critical" if category == "forced" else "high" if category == "failed" else "normal"

        message = self._build_message(event, type_name, category)
        image_path = ""

        # Capture photo only on successful open events
        capture_on_open = self._cam_cfg.get("capture_on_open", True)
        is_open_success = record_type in _OPEN_TYPES and int(event.get("success", 0)) == 1
        if capture_on_open and is_open_success:
            rtsp_url = self._cam_cfg.get("rtsp_url", "")
            ffmpeg_path = self._cam_cfg.get("ffmpeg_path", "ffmpeg")
            if rtsp_url:
                photos_dir = self._storage_cfg.get("photos_dir", "fotos")
                output_path = get_photo_path(photos_dir, "open")
                ok = capture_frame(rtsp_url, output_path, ffmpeg_path)
                if ok:
                    image_path = output_path
            else:
                logger.warning("capture_on_open is true but camera.rtsp_url is not set")

        logger.info(
            "Event recordType=%d (%s) success=%s priority=%s battery=%s photo=%s",
            record_type,
            type_name,
            event.get("success"),
            priority,
            f"{self._battery}%" if self._battery >= 0 else "?",
            image_path or "none",
        )
        await self._callback(message, image_path, priority, event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Main polling loop. Runs until stop() is called."""
        self._running = True
        logger.info(
            "TTLockMonitor started — lock: %s, polling every %ds",
            self._lock_name,
            self._polling_interval,
        )
        while self._running:
            if not self._ensure_token():
                logger.error("Could not obtain a valid token — retrying in %ds", self._polling_interval)
                await asyncio.sleep(self._polling_interval)
                continue

            events = self._poll_events()
            for event in events:
                await self._process_event(event)

            await asyncio.sleep(self._polling_interval)

        logger.info("TTLockMonitor stopped")

    def stop(self) -> None:
        self._running = False

    def get_lock_detail(self) -> dict:
        """GET /v3/lock/detail — returns lock info including electricQuantity and lockAlias.

        Returns empty dict on any error; caller must treat missing keys as unavailable.
        """
        if not self._ensure_token():
            return {}
        params = urllib.parse.urlencode({
            "clientId": self._client_id,
            "accessToken": self._token_data["access_token"],
            "lockId": self._lock_id,
            "date": int(time.time() * 1000),
        })
        try:
            with urllib.request.urlopen(
                f"{self._api_url}/v3/lock/detail?{params}", timeout=10
            ) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.warning("get_lock_detail error: %s", e)
            return {}

    def get_last_record(self) -> dict:
        """GET /v3/lockRecord/list with pageSize=1 — returns the most recent lock record.

        Returns empty dict on any error or when the list is empty.
        """
        if not self._ensure_token():
            return {}
        params = urllib.parse.urlencode({
            "clientId": self._client_id,
            "accessToken": self._token_data["access_token"],
            "lockId": self._lock_id,
            "pageNo": 1,
            "pageSize": 1,
            "date": int(time.time() * 1000),
        })
        try:
            with urllib.request.urlopen(
                f"{self._api_url}/v3/lockRecord/list?{params}", timeout=10
            ) as resp:
                data = json.loads(resp.read())
                records = data.get("list", [])
                return records[0] if records else {}
        except Exception as e:
            logger.warning("get_last_record error: %s", e)
            return {}

    def get_battery(self) -> int:
        """Return last known battery level (0-100), or -1 if no event has been received yet."""
        return self._battery


# ------------------------------------------------------------------
# CLI — print events in real time
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if not os.path.exists(config_path):
        logger.error("config.yaml not found — copy config.yaml.example and fill in your values")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    async def print_event(message: str, image_path: str, priority: str, event: dict) -> None:
        print(f"\n{'='*50}")
        print(f"[priority={priority}]")
        print(message)
        if image_path:
            print(f"Photo: {image_path}")
        print("=" * 50)

    monitor = TTLockMonitor(config, print_event)

    try:
        asyncio.run(monitor.start())
    except KeyboardInterrupt:
        monitor.stop()
        logger.info("Interrupted by user")
