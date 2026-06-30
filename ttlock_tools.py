"""CLI helper for TTLock Alert setup and diagnostics.

Called by setup-ttlockalert.bat. Uses config.yaml for all credentials.

Usage:
  py ttlock_tools.py --list-locks       list all locks linked to the account
  py ttlock_tools.py --check-vercel     verify Vercel Relay status
  py ttlock_tools.py --test-wa          check wa-gateway and send a test message
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    if not os.path.isfile(config_path):
        print("  ERROR: config.yaml no encontrado.")
        print("         Copia config.yaml.example, renombralo a config.yaml y completa los datos.")
        sys.exit(1)
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _require(section: dict, *keys: str) -> None:
    """Exit with a clear error if any key is missing or empty in the given config section."""
    for key in keys:
        if not section.get(key):
            print(f"  ERROR: '{key}' no configurado en config.yaml.")
            sys.exit(1)


def _get_token(tt: dict) -> str:
    """Obtain a fresh access token via password grant."""
    _require(tt, "api_url", "client_id", "client_secret", "username", "password_md5")
    body = urllib.parse.urlencode({
        "clientId":     tt["client_id"],
        "clientSecret": tt["client_secret"],
        "username":     tt["username"],
        "password":     tt["password_md5"],
    }).encode()
    req = urllib.request.Request(
        tt["api_url"].rstrip("/") + "/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:
        print(f"  ERROR de conexión con TTLock API: {e}")
        sys.exit(1)
    if "access_token" not in data:
        print(f"  ERROR de autenticación: {data}")
        print("  Verifica client_id, client_secret, username y password_md5 en config.yaml.")
        sys.exit(1)
    return data["access_token"]


# ------------------------------------------------------------------
# --list-locks
# ------------------------------------------------------------------

def cmd_list_locks(tt: dict) -> None:
    print("  Obteniendo token desde TTLock API...")
    token = _get_token(tt)
    print("  Autenticado correctamente.")
    print()

    api_url = tt["api_url"].rstrip("/")
    params = urllib.parse.urlencode({
        "clientId":    tt["client_id"],
        "accessToken": token,
        "pageNo":      1,
        "pageSize":    50,
        "date":        int(time.time() * 1000),
    })
    try:
        data = json.loads(
            urllib.request.urlopen(f"{api_url}/v3/lock/list?{params}", timeout=10).read()
        )
    except Exception as e:
        print(f"  ERROR al consultar cerraduras: {e}")
        sys.exit(1)

    if data.get("errcode") and int(data.get("errcode", 0)) != 0:
        print(f"  API respondió con error: {data}")
        sys.exit(1)

    locks = data.get("list", [])
    if not locks:
        print("  Sin cerraduras vinculadas a esta cuenta.")
        return

    configured_id = int(tt.get("lock_id", 0))

    print(f"  {'lockId':<10} {'Nombre':<28} {'Alias':<22} {'Batería':>8}  Gateway  Config")
    print("  " + "─" * 82)
    for lk in locks:
        lock_id  = lk.get("lockId", "?")
        name     = (lk.get("lockName") or "—")[:27]
        alias    = (lk.get("lockAlias") or "—")[:21]
        battery  = f"{lk.get('electricQuantity', '?')}%"
        gateway  = "Sí" if lk.get("hasGateway") == 1 else "No"
        marker   = "← activo" if lock_id == configured_id else ""
        print(f"  {lock_id:<10} {name:<28} {alias:<22} {battery:>8}  {gateway:<7}  {marker}")

    print(f"\n  Total: {len(locks)} cerradura(s)")
    if configured_id:
        print(f"  lock_id configurado en config.yaml: {configured_id}")


# ------------------------------------------------------------------
# --check-vercel
# ------------------------------------------------------------------

def cmd_check_vercel(tt: dict) -> None:
    _require(tt, "vercel_url", "api_key")
    vercel_url = tt["vercel_url"].rstrip("/")
    api_key    = tt["api_key"]

    url = f"{vercel_url}/api/ttlock-events"
    print(f"  URL:  {url}")
    print()

    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  ERROR HTTP {e.code}: {body}")
        if e.code == 401:
            print("  Verifica que ttlock.api_key en config.yaml coincida con")
            print("  TTLOCKALERT_API_KEY en las variables de entorno de Vercel.")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR de conexión: {e}")
        print("  Verifica que vercel_url sea correcto y el proyecto esté desplegado.")
        sys.exit(1)

    events = data.get("events", [])
    print(f"  Estado:               OK")
    print(f"  Eventos en cola:      {len(events)}")
    if events:
        types = [e.get("recordType", "?") for e in events]
        print(f"  recordType(s):        {types}")
    else:
        print("  Cola vacía (normal si no ha habido actividad reciente).")


# ------------------------------------------------------------------
# --test-webhook
# ------------------------------------------------------------------

def cmd_test_webhook(tt: dict) -> None:
    _require(tt, "vercel_url", "api_key", "lock_id")
    vercel_url = tt["vercel_url"].rstrip("/")
    api_key    = tt["api_key"]
    lock_id    = int(tt["lock_id"])

    now_ms  = int(time.time() * 1000)
    records = json.dumps([{
        "electricQuantity": 85,
        "lockDate":         now_ms,
        "username":         "prueba-setup",
        "serverDate":       now_ms,
        "recordType":       1,
        "success":          1,
    }])

    body = urllib.parse.urlencode({
        "notifyType": "1",
        "lockId":     str(lock_id),
        "lockMac":    "AA:BB:CC:DD:EE:FF",
        "records":    records,
    }).encode()

    webhook_url = f"{vercel_url}/api/ttlock-webhook"
    print(f"  Webhook:  {webhook_url}")
    print(f"  lockId:   {lock_id}  (de config.yaml)")
    print()

    # Paso 1 — POST al webhook
    print("  Enviando evento simulado (recordType=1, username=prueba-setup)...")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            response_body = resp.read().decode(errors="replace").strip()
    except urllib.error.HTTPError as e:
        print(f"  ERROR HTTP {e.code}: {e.read().decode(errors='replace')}")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR de conexion: {e}")
        sys.exit(1)

    if response_body == "success":
        print("  Webhook:  OK — respondio 'success'")
    else:
        print(f"  Webhook respondio: {response_body!r}  (esperado: 'success')")
        return

    # Paso 2 — esperar un ciclo de polling para que el servicio consuma el evento
    print()
    print("  Esperando 7s para que el servicio procese el evento...")
    for i in range(7, 0, -1):
        print(f"  {i}...", end="\r", flush=True)
        time.sleep(1)
    print("                ")

    # Paso 3 — verificar si el servicio consumio el evento
    print("  Consultando cola Upstash Redis...")

    events_url = f"{vercel_url}/api/ttlock-events"
    req2 = urllib.request.Request(events_url, headers={"x-api-key": api_key})
    try:
        with urllib.request.urlopen(req2, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  ERROR al consultar eventos: {e}")
        return

    events = data.get("events", [])
    found  = any(e.get("username") == "prueba-setup" for e in events)

    print()
    if not found and len(events) == 0:
        print("  Cola vacia: el servicio consumio el evento correctamente.")
        print()
        print("  Flujo completo OK:  TTLock Cloud → Vercel → Redis → TTLock Alert")
        print("  Revisa WhatsApp — el mensaje de prueba debio haber llegado.")
    elif not found:
        print(f"  Evento de prueba no encontrado ({len(events)} otro(s) en cola).")
        print("  El servicio puede haberlo consumido ya. Revisa WhatsApp.")
    else:
        print(f"  [!] Evento aun en cola tras 7s ({len(events)} evento(s)).")
        print()
        print("  Posibles causas:")
        print("    - El servicio TTLock Alert no esta corriendo")
        print("    - El servicio no puede contactar Vercel (revisa el log)")
        print("    - El intervalo de polling es mayor a 7s (revisa polling_interval)")


# ------------------------------------------------------------------
# --test-wa
# ------------------------------------------------------------------

def cmd_test_wa(wa_cfg: dict) -> None:
    _require(wa_cfg, "gateway_url")
    gateway_url = wa_cfg["gateway_url"].rstrip("/")
    print(f"  Gateway URL: {gateway_url}")
    print()

    # Status
    try:
        data = json.loads(urllib.request.urlopen(f"{gateway_url}/status", timeout=5).read())
    except Exception as e:
        print(f"  ERROR: wa-gateway no responde.")
        print(f"  Detalle: {e}")
        print()
        print("  Verifica que wa-gateway esté corriendo como servicio.")
        sys.exit(1)

    ok        = data.get("ok")
    connected = data.get("connected")
    print(f"  HTTP:       OK")
    print(f"  ok:         {ok}")
    print(f"  connected:  {connected}")
    if ok and connected:
        print(f"  WhatsApp:   CONECTADO ✓")
    elif ok and not connected:
        print(f"  WhatsApp:   DESCONECTADO (proceso activo, sin sesión WhatsApp)")
        print("  Usa el panel de wa-gateway para reconectar la sesión.")
    else:
        print("  wa-gateway responde pero reporta error interno.")

    # Test message
    recipients = [r for r in wa_cfg.get("recipients", []) if r.strip()]
    if not recipients:
        print()
        print("  Sin destinatarios configurados en whatsapp.recipients — omitiendo envío.")
        return

    first = recipients[0]
    print()
    print(f"  Enviando mensaje de prueba a {first}...")
    payload = json.dumps({
        "to":        first,
        "message":   "TTLock Alert — mensaje de prueba de configuración ✓",
        "imagePath": "",
    }).encode()
    req = urllib.request.Request(
        f"{gateway_url}/send",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                print(f"  Enviado correctamente a {first}")
            else:
                print(f"  Error HTTP {resp.status}")
    except Exception as e:
        print(f"  ERROR al enviar: {e}")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="TTLock Alert — herramientas de diagnóstico")
    parser.add_argument("--list-locks",    action="store_true", help="Listar cerraduras de la cuenta TTLock")
    parser.add_argument("--check-vercel",  action="store_true", help="Verificar estado del Vercel Relay")
    parser.add_argument("--test-webhook",  action="store_true", help="Simular evento y verificar flujo Webhook → Redis")
    parser.add_argument("--test-wa",       action="store_true", help="Verificar wa-gateway y enviar mensaje de prueba")
    args = parser.parse_args()

    config = load_config()
    tt = config["ttlock"]
    wa = config.get("whatsapp", {})

    if args.list_locks:
        cmd_list_locks(tt)
    elif args.check_vercel:
        cmd_check_vercel(tt)
    elif args.test_webhook:
        cmd_test_webhook(tt)
    elif args.test_wa:
        cmd_test_wa(wa)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
