import asyncio
import logging
import os
import re
import time
from datetime import datetime

logger = logging.getLogger("health_monitor")

_UNLOCK_TYPES = {1, 4, 7, 8, 9, 10, 12, 32}
_ALL_KNOWN_TYPES = {1, 4, 7, 8, 9, 10, 12, 29, 31, 32, 44, 48, 50, 64, 65, 66}
# recordType 30 (sensor cerrada) is always ignored, excluded from _ALL_KNOWN_TYPES

_RECORD_TYPE_NAMES = {
    1:  "App",
    4:  "Código numérico",
    7:  "Tarjeta IC",
    8:  "Huella digital",
    9:  "Pulsera",
    10: "Llave mecánica",
    12: "Gateway",
    29: "Fuerza aplicada a la cerradura",
    31: "Sensor de puerta — abierta",
    32: "Desde adentro",
    44: "Alerta de manipulación",
    48: "Sistema bloqueado",
    50: "Alta temperatura",
    64: "Alarma puerta sin cerrar",
    65: "Falló al abrir",
    66: "Falló al cerrar",
}

_RECORD_TYPE_EMOJIS = {
    1: "🔓", 4: "🔓", 7: "🔓", 8: "🔓", 9: "🔓", 10: "🔓", 12: "🔓", 32: "🔓",
    29: "🚨",
    31: "🚪",
    44: "🚨",
    48: "🔒",
    50: "🌡️",
    64: "⚠️",
    65: "⚠️",
    66: "⚠️",
}


class HealthMonitor:
    def __init__(self, config: dict, wa_sender, ttlock_monitor):
        self._wa = wa_sender
        self._monitor = ttlock_monitor

        tt = config["ttlock"]
        self._lock_name = tt.get("lock_name", "Cerradura")
        self._battery_threshold = int(tt.get("battery_alert_threshold", 30))

        self._daily_report_time = config.get("monitoring", {}).get("daily_report_time", "08:00")

        storage = config.get("storage", {})
        self._photos_dir = storage.get("photos_dir", "fotos")
        self._retention_days = int(storage.get("retention_days", 90))

        self._recipients = [r for r in config["whatsapp"].get("recipients", []) if r.strip()]

        self._report_enabled = config.get("monitoring", {}).get("daily_report_enabled", True)

        self._running = False
        self._gateway_was_alive = None  # None = not yet checked
        self._silence_until = 0.0
        self._last_battery_alert = 0.0
        self._last_report_date = None
        self._last_openings = []  # max 3 opens: {username, record_type_name, lockDate} — TT ESTADO fallback
        self._last_events = []   # max 20 events of any type — TT HISTORIAL fallback
        self._counts_date = None
        self._openings_today_count = 0
        self._failed_today_count = 0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        logger.info("HealthMonitor started")
        while self._running:
            self.check_gateway_health()
            self.check_battery()
            self.check_daily_report()
            await asyncio.sleep(60)
        logger.info("HealthMonitor stopped")

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Gateway health
    # ------------------------------------------------------------------

    def check_gateway_health(self) -> None:
        """Verify that wa-gateway responds in HTTP and log state changes.

        Los emails de alerta por desconexión de WhatsApp los gestiona wa-gateway.
        Este método solo loguea el estado.
        """
        alive = self._wa.is_gateway_alive()
        if self._gateway_was_alive is None:
            logger.info("wa-gateway initial state: %s", "connected" if alive else "disconnected")
        elif alive and not self._gateway_was_alive:
            logger.info("wa-gateway reconnected")
        elif not alive and self._gateway_was_alive:
            logger.warning("wa-gateway disconnected")
        self._gateway_was_alive = alive

    # ------------------------------------------------------------------
    # Battery check
    # ------------------------------------------------------------------

    def check_battery(self) -> None:
        battery = self._monitor.get_battery()
        if battery < 0:
            return  # no event received yet
        if battery >= self._battery_threshold:
            return
        if time.time() - self._last_battery_alert < 86400:
            return  # already alerted in the last 24h
        message = (
            f"🔋 {self._lock_name} — Batería baja: {battery}%\n"
            f"Nivel por debajo del umbral configurado ({self._battery_threshold}%)."
        )
        self._wa.send_alert(message)
        self._last_battery_alert = time.time()
        logger.info("Battery alert sent: %d%%", battery)

    # ------------------------------------------------------------------
    # Daily report and photo cleanup
    # ------------------------------------------------------------------

    def check_daily_report(self) -> None:
        if not self._report_enabled:
            return
        now = datetime.now()
        today = now.date()
        if now.strftime("%H:%M") != self._daily_report_time:
            return
        if self._last_report_date == today:
            return  # already sent today
        self._last_report_date = today
        report = self._build_daily_report(now)
        self._wa.send_alert(report)
        logger.info("Daily report sent")
        self._cleanup_old_photos()

    def _photos_today(self) -> int:
        today = datetime.now()
        day_dir = os.path.join(
            self._photos_dir,
            today.strftime("%Y"),
            today.strftime("%m"),
            today.strftime("%d"),
        )
        if not os.path.isdir(day_dir):
            return 0
        return sum(1 for f in os.listdir(day_dir) if f.lower().endswith(".jpg"))

    def _build_daily_report(self, now: datetime) -> str:
        # Battery — live API, fall back to last in-memory value
        detail = self._monitor.get_lock_detail()
        live_battery = detail.get("electricQuantity")
        if live_battery is not None:
            battery_str = f"{live_battery}%"
        else:
            mem_battery = self._monitor.get_battery()
            battery_str = f"{mem_battery}% (caché)" if mem_battery >= 0 else "desconocida"

        # Today's counts — live API, fall back to in-memory counters
        today_records = self._monitor.get_today_records()
        if today_records is not None:
            openings = sum(
                1 for r in today_records
                if int(r.get("recordType", -1)) in _UNLOCK_TYPES and int(r.get("success", 0)) == 1
            )
            failures = sum(
                1 for r in today_records
                if (
                    int(r.get("recordType", -1)) in _UNLOCK_TYPES and int(r.get("success", 0)) == 0
                ) or int(r.get("recordType", -1)) in {48, 65}
            )
            suffix = ""
        else:
            self._check_date_reset(now.date())
            openings = self._openings_today_count
            failures = self._failed_today_count
            suffix = " (caché)"

        lines = [
            f"📊 Reporte diario — {now.strftime('%d/%m/%Y')}",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"Batería cerradura: {battery_str}",
            f"Aperturas hoy: {openings}{suffix}",
            f"Intentos fallidos hoy: {failures}{suffix}",
        ]
        if self.is_silenced():
            until_str = datetime.fromtimestamp(self._silence_until).strftime("%H:%M")
            lines.append(f"⚠️ Alertas silenciadas hasta las {until_str}")
        return "\n".join(lines)

    def _cleanup_old_photos(self) -> None:
        if self._retention_days <= 0 or not os.path.isdir(self._photos_dir):
            return
        cutoff = time.time() - self._retention_days * 86400
        deleted = 0
        for root, _dirs, files in os.walk(self._photos_dir, topdown=False):
            for fname in files:
                if not fname.lower().endswith(".jpg"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    if os.path.getmtime(fpath) < cutoff:
                        os.remove(fpath)
                        deleted += 1
                except Exception as e:
                    logger.warning("Could not delete %s: %s", fpath, e)
            if root != self._photos_dir and not os.listdir(root):
                try:
                    os.rmdir(root)
                except Exception:
                    pass
        if deleted:
            logger.info("Cleanup: deleted %d photos older than %d days", deleted, self._retention_days)

    # ------------------------------------------------------------------
    # Silence
    # ------------------------------------------------------------------

    def is_silenced(self) -> bool:
        return time.time() < self._silence_until

    def silence(self, hours: float) -> None:
        self._silence_until = time.time() + hours * 3600
        logger.info("Alerts silenced for %.1f hours", hours)

    # ------------------------------------------------------------------
    # Opening history
    # ------------------------------------------------------------------

    def _check_date_reset(self, today) -> None:
        if self._counts_date != today:
            self._openings_today_count = 0
            self._failed_today_count = 0
            self._counts_date = today

    def register_event(self, event: dict) -> None:
        """Update daily counters and event history. Call from main.py for every event."""
        today = datetime.now().date()
        self._check_date_reset(today)

        record_type = int(event.get("recordType", -1))

        try:
            date_str = datetime.fromtimestamp(int(event.get("lockDate", 0)) / 1000).strftime("%d/%m/%Y %H:%M")
        except Exception:
            date_str = "?"

        if record_type in _UNLOCK_TYPES and int(event.get("success", 1)) == 1:
            self._openings_today_count += 1
            self._last_openings.insert(0, {
                "username": event.get("username", ""),
                "record_type_name": _RECORD_TYPE_NAMES.get(record_type, str(record_type)),
                "lockDate": date_str,
            })
            self._last_openings = self._last_openings[:3]

        elif (record_type in _UNLOCK_TYPES and int(event.get("success", 1)) == 0) or record_type in {48, 65}:
            self._failed_today_count += 1

        if record_type in _ALL_KNOWN_TYPES:
            self._last_events.insert(0, {
                "record_type": record_type,
                "username": event.get("username", ""),
                "lockDate": date_str,
            })
            self._last_events = self._last_events[:20]

    # ------------------------------------------------------------------
    # WhatsApp command processing
    # ------------------------------------------------------------------

    def _is_authorized_sender(self, sender: str) -> bool:
        sender_digits = re.sub(r"\D", "", sender)
        return any(re.sub(r"\D", "", r) == sender_digits for r in self._recipients)

    async def process_command(self, message: str, sender: str) -> None:
        """Process incoming WhatsApp message. Ignores anything not starting with 'TT '.

        Messages without the 'TT ' prefix are silently ignored — they may belong
        to other services sharing the same WhatsApp number (e.g. RingAlert).
        """
        stripped = message.strip()
        if not stripped.upper().startswith("TT "):
            return

        if not self._is_authorized_sender(sender):
            logger.warning("Unauthorized TT command from %s — ignored", sender)
            return

        cmd = stripped.upper()

        if cmd == "TT HISTORIAL" or cmd.startswith("TT HISTORIAL "):
            parts = cmd.split()
            count = 3
            if len(parts) == 3:
                try:
                    count = max(1, min(20, int(parts[2])))
                except ValueError:
                    self._wa.send_direct(sender, "Formato inválido. Ejemplo: TT HISTORIAL 10 (máximo 20)")
                    return
            reply = self._reply_historial(count)

        elif cmd.startswith("TT SILENCIAR "):
            match = re.match(r"TT SILENCIAR ([\d.]+)H$", cmd)
            if not match:
                reply = "Formato inválido. Ejemplo: TT SILENCIAR 2h"
            else:
                hours = float(match.group(1))
                self.silence(hours)
                until_str = datetime.fromtimestamp(self._silence_until).strftime("%H:%M")
                reply = f"🔕 Alertas de {self._lock_name} silenciadas por {hours}h (hasta las {until_str})"

        elif cmd == "TT REACTIVAR":
            if not self.is_silenced():
                reply = "Las alertas ya estaban activas."
            else:
                self._silence_until = 0.0
                reply = f"🔔 Alertas de {self._lock_name} reactivadas."

        elif cmd == "TT ESTADO":
            reply = self._reply_estado()

        else:
            logger.debug("Unknown TT command from %s: %s", sender, stripped)
            return

        self._wa.send_direct(sender, reply)

    def _reply_historial(self, count: int = 3) -> str:
        header = f"📋 Historial — {self._lock_name} (últimos {count})\n━━━━━━━━━━━━━━━━━━━━━"

        records = self._monitor.get_recent_records(count)

        if records is not None:
            if not records:
                return f"{header}\nSin eventos registrados."
            lines = [header]
            for i, r in enumerate(records, 1):
                rt = int(r.get("recordType", -1))
                emoji = _RECORD_TYPE_EMOJIS.get(rt, "•")
                type_name = _RECORD_TYPE_NAMES.get(rt, f"tipo {rt}")
                name = r.get("username") or "—"
                try:
                    date_str = datetime.fromtimestamp(
                        int(r.get("lockDate", 0)) / 1000
                    ).strftime("%d/%m/%Y %H:%M")
                except Exception:
                    date_str = "?"
                lines.append(f"{i}. {emoji} {name} ({type_name}) — {date_str}")
            return "\n".join(lines)

        # Fallback to in-memory events when API is unavailable
        if not self._last_events:
            return f"{header}\nSin eventos registrados desde que inició el servicio."
        lines = [header]
        for i, entry in enumerate(self._last_events[:count], 1):
            rt = entry["record_type"]
            emoji = _RECORD_TYPE_EMOJIS.get(rt, "•")
            type_name = _RECORD_TYPE_NAMES.get(rt, f"tipo {rt}")
            name = entry["username"] or "—"
            lines.append(f"{i}. {emoji} {name} ({type_name}) — {entry['lockDate']} (caché)")
        return "\n".join(lines)

    def _reply_estado(self) -> str:
        # Battery — live API, fall back to last known value from events
        detail = self._monitor.get_lock_detail()
        live_battery = detail.get("electricQuantity")
        if live_battery is not None:
            battery_str = f"{live_battery}%"
            lock_alias = detail.get("lockAlias") or self._lock_name
        else:
            mem_battery = self._monitor.get_battery()
            battery_str = f"{mem_battery}% (caché)" if mem_battery >= 0 else "desconocida"
            lock_alias = self._lock_name

        # Last access — live API, fall back to in-memory history
        record = self._monitor.get_last_record()
        if record:
            username = record.get("username") or "—"
            rt = int(record.get("recordType", -1))
            method = _RECORD_TYPE_NAMES.get(rt, f"tipo {rt}")
            try:
                date_str = datetime.fromtimestamp(
                    int(record.get("lockDate", 0)) / 1000
                ).strftime("%d/%m/%Y %H:%M")
            except Exception:
                date_str = "?"
            last_access = f"{username} ({method}) — {date_str}"
        elif self._last_openings:
            e = self._last_openings[0]
            last_access = f"{e['username'] or '—'} ({e['record_type_name']}) — {e['lockDate']} (caché)"
        else:
            last_access = "Sin aperturas registradas"

        lines = [
            f"📍 Estado — {lock_alias}",
            f"🔋 Batería: {battery_str}",
            f"🔓 Último acceso: {last_access}",
        ]
        if self.is_silenced():
            until_str = datetime.fromtimestamp(self._silence_until).strftime("%H:%M")
            lines.append(f"🔕 Alertas silenciadas hasta las {until_str}")
        return "\n".join(lines)
