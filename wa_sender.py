import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("wa_sender")


class WhatsAppSender:
    def __init__(self, config: dict):
        wa = config["whatsapp"]
        self._base_url = wa["gateway_url"].rstrip("/")
        self._recipients = [r for r in wa.get("recipients", []) if r.strip()]
        self._email_cfg = config.get("email", {})

    # ------------------------------------------------------------------
    # Gateway health
    # ------------------------------------------------------------------

    def is_gateway_alive(self) -> bool:
        """Return True only when wa-gateway is up and WhatsApp is connected."""
        try:
            with urllib.request.urlopen(f"{self._base_url}/status", timeout=5) as resp:
                data = json.loads(resp.read())
                return bool(data.get("ok")) and bool(data.get("connected"))
        except Exception as e:
            logger.debug("Gateway status check failed: %s", e)
            return False

    def register_consumer(self) -> bool:
        """POST /register-consumer — registers this project as a dedicated inbox consumer."""
        payload = json.dumps({"consumer": "ttlockalert"}).encode()
        req = urllib.request.Request(
            f"{self._base_url}/register-consumer",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                if data.get("ok"):
                    logger.info("Consumer 'ttlockalert' registered (queued: %d)", data.get("queued", 0))
                    return True
                logger.warning("register_consumer failed: %s", data.get("error", "unknown"))
                return False
        except Exception as e:
            logger.warning("register_consumer error: %s", e)
            return False

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _send_one(self, to: str, message: str, image_path: str = "") -> bool:
        """POST /send to a single recipient. Returns True on HTTP 200."""
        payload = json.dumps({"to": to, "message": message, "imagePath": image_path}).encode()
        req = urllib.request.Request(
            f"{self._base_url}/send",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            logger.error("POST /send HTTP %d for %s: %s", e.code, to, e.read().decode(errors="replace"))
            return False
        except Exception as e:
            logger.error("POST /send error for %s: %s", to, e)
            return False

    def send_direct(self, to: str, message: str, image_path: str = "") -> bool:
        """Send to a specific recipient without email fallback.

        Used to reply to WhatsApp commands — does not broadcast to all recipients.
        Accepts a phone number (digits only) or a JID with @.
        """
        ok = self._send_one(to, message, image_path)
        logger.info("send_direct → %s: %s", to, "ok" if ok else "failed")
        return ok

    def send_alert(self, message: str, image_path: str = "") -> bool:
        """Broadcast to all configured recipients. Returns True if at least one succeeds.

        If wa-gateway is unreachable (connection error, timeout, or process down),
        falls back to send_email_fallback and returns its result.
        """
        if not self._recipients:
            logger.warning("No recipients configured")
            return False
        results = []
        gateway_unreachable = False
        for recipient in self._recipients:
            payload = json.dumps({"to": recipient, "message": message, "imagePath": image_path}).encode()
            req = urllib.request.Request(
                f"{self._base_url}/send",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    ok = resp.status == 200
            except urllib.error.HTTPError as e:
                logger.error("POST /send HTTP %d for %s: %s", e.code, recipient, e.read().decode(errors="replace"))
                ok = False
            except Exception as e:
                # Connection-level failure: process down, timeout, or network error
                logger.error("POST /send unreachable for %s: %s", recipient, e)
                gateway_unreachable = True
                ok = False
            results.append(ok)
            logger.info("send_alert → %s: %s", recipient, "ok" if ok else "failed")

        if gateway_unreachable and not any(results):
            logger.warning("wa-gateway unreachable — activating email fallback")
            attach = image_path if image_path and os.path.isfile(image_path) else ""
            return self.send_email_fallback(message, image_path=attach)
        return any(results)

    # ------------------------------------------------------------------
    # Inbox polling
    # ------------------------------------------------------------------

    def poll_inbox(self) -> list:
        """GET /inbox?consumer=ttlockalert — returns and empties the dedicated consumer queue.

        Auto-re-registers the consumer if wa-gateway was restarted and lost the registration.
        """
        return self._do_poll_inbox(retry=True)

    def _do_poll_inbox(self, retry: bool) -> list:
        try:
            with urllib.request.urlopen(
                f"{self._base_url}/inbox?consumer=ttlockalert", timeout=10
            ) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            logger.debug("poll_inbox error: %s", e)
            return []
        if not data.get("ok"):
            error = data.get("error", "")
            if retry and "registrado" in error.lower():
                logger.warning("Consumer 'ttlockalert' not registered — re-registering and retrying")
                self.register_consumer()
                return self._do_poll_inbox(retry=False)
            logger.debug("poll_inbox returned ok=false: %s", error)
            return []
        return data.get("messages", [])

    # ------------------------------------------------------------------
    # Email fallback (only when wa-gateway process is down)
    # ------------------------------------------------------------------

    def send_email_fallback(self, message: str, image_path: str = "") -> bool:
        """Send via Gmail SMTP with optional inline image attachment.

        Este método solo debe llamarse cuando wa-gateway no responde en HTTP
        (proceso caído). Las alertas de desconexión interna de WhatsApp las
        gestiona wa-gateway internamente.
        """
        cfg = self._email_cfg
        recipients = [r for r in cfg.get("recipients", []) if r.strip()]
        if not recipients:
            logger.warning("Email fallback: no recipients configured")
            return False
        try:
            if image_path and os.path.isfile(image_path):
                msg = MIMEMultipart()
                msg.attach(MIMEText(message, "plain", "utf-8"))
                with open(image_path, "rb") as img_file:
                    mime_img = MIMEImage(img_file.read())
                mime_img.add_header("Content-Disposition", "attachment", filename=os.path.basename(image_path))
                msg.attach(mime_img)
            else:
                msg = MIMEText(message, "plain", "utf-8")

            msg["Subject"] = "TTLock Alert (fallback)"
            msg["From"] = cfg["sender"]
            msg["To"] = ", ".join(recipients)

            with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"], timeout=15) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(cfg["sender"], cfg["password"])
                smtp.sendmail(cfg["sender"], recipients, msg.as_string())
            logger.info("Email fallback sent to %s", recipients)
            return True
        except Exception as e:
            logger.error("Email fallback failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Message builders
    # ------------------------------------------------------------------

    def build_open_message(
        self,
        username: str,
        keyboard_pwd: str,
        record_type_name: str,
        fecha: str,
        battery: int,
    ) -> str:
        lines = [
            "🔓 Puerta abierta",
            f"Método: {record_type_name}",
        ]
        if username:
            lines.append(f"Usuario: {username}")
        if keyboard_pwd:
            lines.append(f"Clave: {keyboard_pwd}")
        lines += [
            f"Hora: {fecha}",
            f"Batería: {battery}%",
        ]
        return "\n".join(lines)

    def build_failed_message(self, username: str, fecha: str) -> str:
        lines = [
            "⚠️ Intento fallido de acceso",
            f"Hora: {fecha}",
        ]
        if username:
            lines.append(f"Usuario: {username}")
        return "\n".join(lines)

    def build_forced_message(self, fecha: str, battery: int) -> str:
        return "\n".join([
            "🚨 ALERTA: Puerta forzada",
            f"Hora: {fecha}",
            f"Batería: {battery}%",
        ])

    def build_force_message(self, fecha: str, battery: int) -> str:
        return "\n".join([
            "🚨 FUERZA DETECTADA EN LA CERRADURA",
            f"Hora: {fecha}",
            f"Batería: {battery}%",
        ])

    def build_tamper_message(self, fecha: str, battery: int) -> str:
        return "\n".join([
            "🚨 ALERTA DE MANIPULACIÓN",
            f"Hora: {fecha}",
            f"Batería: {battery}%",
        ])

    def build_system_locked_message(self, fecha: str) -> str:
        return "\n".join([
            "🔒 Sistema bloqueado por múltiples intentos fallidos",
            f"Hora: {fecha}",
        ])

    def build_door_alarm_message(self, fecha: str) -> str:
        return "\n".join([
            "⚠️ Puerta sin cerrar — alarma activada",
            f"Hora: {fecha}",
        ])

    def build_failed_open_message(self, username: str, method_name: str, fecha: str) -> str:
        lines = [
            f"⚠️ Falló al abrir ({method_name})",
            f"Hora: {fecha}",
        ]
        if username:
            lines.append(f"Usuario: {username}")
        return "\n".join(lines)


# ------------------------------------------------------------------
# CLI test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import yaml

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if "--test" not in sys.argv and "--test-email" not in sys.argv:
        print("Usage: py wa_sender.py --test | --test-email")
        sys.exit(0)

    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if not os.path.exists(config_path):
        logger.error("config.yaml not found — copy config.yaml.example and fill in your values")
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    sender = WhatsAppSender(config)

    if "--test-email" in sys.argv:
        test_msg = "TTLock Alert — prueba de configuración SMTP"
        logger.info("Sending test email via send_email_fallback")
        ok = sender.send_email_fallback(test_msg)
        logger.info("Result: %s", "success" if ok else "failed")
        sys.exit(0 if ok else 1)

    # --test
    alive = sender.is_gateway_alive()
    logger.info("Gateway alive: %s", alive)

    if not alive:
        logger.error("wa-gateway is not reachable or WhatsApp is not connected — aborting test")
        sys.exit(1)

    recipients = [r for r in config["whatsapp"].get("recipients", []) if r.strip()]
    if not recipients:
        logger.error("No recipients in config.yaml")
        sys.exit(1)

    first = recipients[0]
    test_msg = "TTLock Alert — mensaje de prueba"
    logger.info("Sending test message to %s", first)
    ok = sender._send_one(first, test_msg)
    logger.info("Result: %s", "success" if ok else "failed")
    sys.exit(0 if ok else 1)
