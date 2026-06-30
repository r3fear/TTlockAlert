<div align="center">

![TTLock Alert](https://img.shields.io/badge/TTLock_Alert-Monitoreo_de_Cerradura-1A1A2E?style=for-the-badge&logoColor=white)

*Sistema de monitoreo de cerradura inteligente TTLock con notificaciones por WhatsApp y captura RTSP en tiempo real*

</div>

---

## Descripción general

Cuando alguien abre la puerta, se intenta un acceso fallido, se fuerza la cerradura, o la batería baja del umbral configurado, el sistema:

1. Detecta el evento vía polling al **Vercel Relay**, que recibe webhooks de TTLock Cloud
2. Captura un frame en tiempo real de la cámara SWANN vía RTSP usando ffmpeg
3. Envía mensaje de texto y foto por WhatsApp a través de **[wa-gateway](https://github.com/r3fear/wa-gateway)**
4. Guarda la foto en historial local organizado por fecha con retención configurable
5. Monitorea la salud del sistema, verifica batería y envía reporte diario

---

## Stack tecnológico

| Componente | Tecnología | Versión |
|---|---|---|
| Orquestador | Python | 3.11+ |
| Configuración | PyYAML | 6.0+ |
| HTTP cliente | requests | 2.31+ |
| Captura cámara | ffmpeg | cualquiera con RTSP |
| Gateway WhatsApp | [wa-gateway](https://github.com/r3fear/wa-gateway) (Node.js + whatsapp-web.js) | servicio externo |
| Relay webhooks | Vercel (Node.js serverless) | — |
| Almacenamiento KV | Upstash Redis (vía Vercel Marketplace) | — |

---

## Arquitectura y flujo

```
[TTLock Cloud]
      │
      │  POST /api/ttlock-webhook (form-urlencoded)
      ▼
[Vercel Relay — ttlock-webhook]
      │  Eventos guardados en Upstash Redis con TTL de 3600s
      │
      │  GET /api/ttlock-events (polling cada polling_interval segundos)
      ▼
[ttlock_monitor.py]
      ├──► [camera.py] ──► ffmpeg ──► [SWANN RTSP] ──► foto.jpg
      └──► [wa_sender.py]
                ├─── is_gateway_alive() → POST /send ──► [wa-gateway :3000] ──► WhatsApp
                └─── si proceso caído → send_email_fallback() ──► Gmail SMTP

[health_monitor.py] (loop cada 60s)
      ├── check_gateway_health() → solo loguea (emails los gestiona wa-gateway)
      ├── check_battery()        → alerta si batería < umbral, máx 1 vez/24h
      └── check_daily_report()   → reporte diario + limpieza de fotos antiguas

[main.py — poll_inbox()] (loop cada 5s)
      └── GET /inbox ──► [wa-gateway] ──► health_monitor.process_command()
```

---

## Estructura del proyecto

```
ttlock-alert/
├── config.yaml              ← credenciales reales (NO en GitHub)
├── config.yaml.example      ← plantilla sin credenciales
├── requirements.txt
├── ttlock_token.cache        ← token OAuth2 TTLock (NO en GitHub)
│
├── main.py                  ← orquestador principal
├── ttlock_monitor.py        ← token TTLock + polling Vercel Relay
├── camera.py                ← captura de frame RTSP vía ffmpeg
├── wa_sender.py             ← cliente HTTP para wa-gateway + email fallback
├── health_monitor.py        ← reporte diario, batería, comandos WhatsApp
│
├── logs/
│   └── ttlock-alert.log     ← log principal (NO en GitHub)
│
├── fotos/                   ← historial de fotos (NO en GitHub)
│   └── YYYY/MM/DD/
│       └── open_HHMMSS.jpg
│
└── ttlock-webhook/          ← Vercel Relay (proyecto Node.js separado)
    ├── api/
    │   ├── ttlock-webhook.js  ← recibe POST de TTLock Cloud
    │   └── ttlock-events.js   ← servidor local hace GET aquí
    ├── package.json
    └── vercel.json
```

---

## Módulos — descripción detallada

### `main.py`

Punto de entrada. Orquesta todos los módulos con `asyncio.gather`. Contiene:

- `setup_logging()`: configura log a archivo `logs/ttlock-alert.log` y a consola simultáneamente. Formato: `YYYY-MM-DD HH:MM:SS - LEVEL - mensaje`, encoding UTF-8. Crea la carpeta `logs/` si no existe.
- `on_ttlock_event(message, image_path, priority, event)`: callback que recibe cada evento de `TTLockMonitor`. Prioridades: `critical` siempre envía (nunca silenciable); `high` y `normal` respetan `is_silenced()`. Llama a `health_monitor.register_event(event)` en todos los casos.
- `poll_inbox()`: loop cada 5 segundos que hace `GET /inbox` a wa-gateway y pasa cada mensaje a `health_monitor.process_command()`.
- Al arrancar: verifica disponibilidad de wa-gateway y loguea el resultado.

### `ttlock_monitor.py`

Clase `TTLockMonitor`:

**Gestión de token OAuth2:**
- `_fetch_token()`: obtiene nuevo `access_token` via `POST /oauth2/token` con `grant_type=password`. El password se envía como MD5 tal como se configura en `config.yaml`.
- `_do_refresh()`: refresca via `grant_type=refresh_token`; si falla, cae automáticamente a password grant.
- `_ensure_token()`: llamado antes de cada ciclo de polling. Refresca si quedan menos de 24 horas para expirar. El token TTLock dura 90 días (7776000 segundos).
- Token guardado en el archivo definido por `token_file` (por defecto `ttlock_token.cache`).

**Polling de eventos:**
- `_poll_events()`: `GET {vercel_url}/api/ttlock-events` con header `x-api-key`. Retorna la lista de eventos y vacía la cola en Upstash Redis.
- `_process_event(event)`: despacha según `recordType`. Captura foto si aplica y llama al callback.

**recordType soportados:**

| recordType | Descripción | Prioridad |
|---|---|---|
| 1 | App Bluetooth | normal |
| 4 | Código numérico | normal |
| 7 | Huella digital | normal |
| 8 | Tarjeta IC | normal |
| 9 | Código incorrecto | high |
| 10 | Puerta forzada | critical |
| 11 | Batería baja | normal |

Todos los demás `recordType` se ignoran silenciosamente.

- `get_battery()`: retorna el último nivel de batería conocido (`electricQuantity` del último evento recibido), o `-1` si aún no se ha recibido ningún evento.
- La foto se captura únicamente en aperturas exitosas: `success == 1` y `recordType` en `{1, 4, 7, 8}`. No se captura en intentos fallidos, puerta forzada ni batería baja.

### `wa_sender.py`

Clase `WhatsAppSender`. Usa exclusivamente `urllib.request` de la stdlib (sin dependencias externas).

- `is_gateway_alive()`: `GET /status`; retorna `True` solo si la respuesta contiene `ok: true` **y** `connected: true`.
- `register_consumer()`: `POST /register-consumer` con `{"consumer": "ttlockalert"}` — registra el proyecto en el sistema de consumers de wa-gateway para tener una cola de inbox dedicada e independiente de otros proyectos. Retorna `True` si ok. Loguea `INFO` si exitoso, `WARNING` si falla (no es crítico).
- `send_alert(message, image_path)`: broadcast a todos los recipients configurados que no estén vacíos. Si la conexión falla a nivel HTTP (proceso caído, timeout, error de red) y ningún recipient tuvo éxito, activa `send_email_fallback`. **No activa fallback por errores HTTP 4xx/5xx del gateway.**
- `send_direct(to, message, image_path)`: envía a un destinatario específico (número o JID). Usado para responder comandos de WhatsApp — no hace broadcast ni activa fallback de email.
- `send_email_fallback(message, image_path)`: SMTP Gmail con `starttls`. **Este método solo debe llamarse cuando wa-gateway no responde en HTTP (proceso caído). Las alertas de desconexión interna de WhatsApp las gestiona wa-gateway internamente.** Adjunta imagen como `MIMEImage` si el archivo existe en disco.
- `poll_inbox()`: `GET /inbox?consumer=ttlockalert` — retorna y vacía la cola de mensajes del consumer `ttlockalert`. Si wa-gateway fue reiniciado y perdió el registro, detecta el error `ok: false` y llama automáticamente a `register_consumer()` reintentando una vez.
- `build_open_message(username, keyboard_pwd, record_type_name, fecha, battery)`: formatea mensaje de apertura con emoji.
- `build_failed_message(username, fecha)`: mensaje de intento fallido.
- `build_forced_message(fecha, battery)`: mensaje de puerta forzada (alerta crítica).

CLI: `py wa_sender.py --test` verifica gateway y envía mensaje al primer recipient. `--test-email` prueba la configuración SMTP directamente.

### `health_monitor.py`

Clase `HealthMonitor`. Loop cada 60 segundos.

- `check_gateway_health()`: verifica si wa-gateway responde en HTTP y loguea las transiciones de estado (conectado → desconectado, desconectado → reconectado). **Los emails de alerta por desconexión de WhatsApp los gestiona wa-gateway. Este método solo loguea el estado.**
- `check_battery()`: consulta `ttlock_monitor.get_battery()`. Si el nivel está por debajo de `battery_alert_threshold` y no se ha enviado alerta en las últimas 24 horas, envía alerta vía `send_alert`.
- `check_daily_report()`: envía reporte a `daily_report_time` una vez por día si `daily_report_enabled: true`. El reporte incluye batería actual, aperturas del día e intentos fallidos del día. Ejecuta limpieza de fotos antiguas según `retention_days`.
- `register_event(event)`: registra cada evento entrante. Para aperturas exitosas (`recordType` en `{1,4,7,8}` con `success==1`): incrementa contador diario y actualiza el historial de las últimas 3 aperturas. Para códigos incorrectos (`recordType 9`): incrementa contador de fallos diarios. Los contadores se resetean automáticamente al cambiar el día.
- `is_silenced()` / `silence(hours)` / `_silence_until`: gestión de silencio temporal.
- `_is_authorized_sender(sender)`: extrae dígitos del sender y compara contra los `recipients` configurados. Rechaza silenciosamente mensajes de números no autorizados.
- `process_command(message, sender)`: procesa comandos entrantes de WhatsApp. **Ignora completamente mensajes que no empiecen con `"TT "` — diseño intencional para coexistir con otros servicios (como RingAlert) que compartan el mismo inbox de wa-gateway.** Responde solo al remitente via `send_direct`.

### `camera.py`

- `capture_frame(rtsp_url, output_path, ffmpeg_path)`: ejecuta ffmpeg como subprocess con timeout de 30s. Flags: `-rtsp_transport tcp -vframes 1 -update 1 -y`. Retorna `True/False`. Maneja `FileNotFoundError` (ffmpeg no instalado), `TimeoutExpired` y cualquier otra excepción sin lanzarla.
- `get_photo_path(photos_dir, event_type)`: construye ruta `fotos/YYYY/MM/DD/tipo_HHMMSS.jpg` y crea las carpetas necesarias con `os.makedirs`.

CLI: `py camera.py` captura un frame de prueba con la configuración de `config.yaml`.

---

## Vercel Relay — ttlock-webhook

### ¿Qué hace y por qué existe?

TTLock Cloud necesita una URL pública accesible desde internet para enviar webhooks. El servidor local que ejecuta TTLock Alert está en una red privada sin IP pública fija. El Vercel Relay actúa como intermediario: recibe los webhooks de TTLock, los guarda temporalmente en Upstash Redis, y el servidor local los consume via polling.

### Estructura

```
ttlock-webhook/
├── api/
│   ├── ttlock-webhook.js  ← POST de TTLock Cloud (guarda en KV)
│   └── ttlock-events.js   ← GET del servidor local (retorna y vacía KV)
├── package.json
└── vercel.json
```

### Endpoints

**`POST /api/ttlock-webhook`** — recibe webhooks de TTLock Cloud

- Content-Type: `application/x-www-form-urlencoded`
- Campos: `notifyType`, `lockId`, `lockMac`, `records` (JSON string con array de eventos)
- Parsea `records`, guarda cada evento en Upstash Redis con TTL de 3600 segundos
- Agrega los IDs a la lista `ttlock:pending` en Redis
- **Siempre responde HTTP 200 con body exacto `success`** — TTLock reintentará el POST si recibe cualquier otra respuesta

**`GET /api/ttlock-events`** — consumido por el servidor local

- Header requerido: `x-api-key: <TTLOCKALERT_API_KEY>`
- Responde 401 si la clave no coincide, 405 si no es GET
- Recupera todos los eventos pendientes de Redis en un solo round-trip (`mget`), elimina la lista `ttlock:pending`
- Responde `{ "events": [...] }` o `{ "events": [] }` si no hay pendientes

### Setup paso a paso

**1. Crear proyecto en Vercel**

```bash
cd ttlock-webhook
npm install
vercel deploy
```

O conectar el repositorio directamente desde Vercel Dashboard → New Project.

**2. Crear base de datos Redis con Upstash**

El almacenamiento usa **Upstash Redis**. Hay dos formas de conectarlo:

**Opción A — Vercel Marketplace (recomendado):**

1. Vercel Dashboard → **Storage** → **Connect Store** → buscar **Upstash KV**
2. Seguir el flujo de instalación; Vercel crea la base y conecta el proyecto automáticamente
3. Las variables `KV_REST_API_URL` y `KV_REST_API_TOKEN` se inyectan solas en el proyecto

**Opción B — Cuenta directa en Upstash:**

1. Crear cuenta en [upstash.com](https://upstash.com) (plan gratuito disponible)
2. Console → **Create Database** → **Redis**
3. Nombre: `ttlock-kv`, región: la más cercana al servidor
4. Abrir la base de datos → pestaña **REST API** → copiar `UPSTASH_REDIS_REST_URL` y `UPSTASH_REDIS_REST_TOKEN`
5. Agregar en Vercel como `KV_REST_API_URL` y `KV_REST_API_TOKEN` respectivamente

**3. Agregar variables de entorno en Vercel**

Vercel Dashboard → proyecto `ttlock-webhook` → **Settings** → **Environment Variables**:

| Variable | Valor | Origen |
|---|---|---|
| `KV_REST_API_URL` | URL de Upstash REST API | Auto (Marketplace) o Manual |
| `KV_REST_API_TOKEN` | Token de Upstash REST API | Auto (Marketplace) o Manual |
| `TTLOCKALERT_API_KEY` | cadena secreta aleatoria | Manual |

El valor de `TTLOCKALERT_API_KEY` debe coincidir con `ttlock.api_key` en `config.yaml`.

**4. Registrar URL del webhook en TTLock**

TTLock Open Platform Management Center → tu aplicación → Callback URL:

```
https://<tu-proyecto>.vercel.app/api/ttlock-webhook
```

---

## Instalación

### Requisitos previos

- Python 3.11 o superior
- ffmpeg instalado y en PATH (`winget install --id Gyan.FFmpeg`)
- [wa-gateway](https://github.com/r3fear/wa-gateway) corriendo como servicio antes de iniciar TTLock Alert
- Cuenta Vercel con el Relay desplegado (ver sección anterior)
- Cuenta TTLock Open Platform con aplicación creada y callback registrado

### Pasos

**1. Clonar el repositorio**

```bash
git clone https://github.com/r3fear/TTlockAlert.git TTLockAlert
cd TTLockAlert
```

**2. Instalar dependencias Python**

```powershell
py -m pip install -r requirements.txt
```

**3. Crear y editar configuración**

```powershell
copy config.yaml.example config.yaml
```

Editar `config.yaml` con los valores reales (ver sección [Configuración](#configuración)).

**4. Generar MD5 del password TTLock**

El campo `password_md5` debe contener el hash MD5 de tu contraseña TTLock (en minúsculas).
Herramienta online: [md5.cz](https://www.md5.cz/) u otra de tu preferencia.

**5. Desplegar el Vercel Relay**

Ver sección [Vercel Relay — ttlock-webhook](#vercel-relay--ttlock-webhook).

**6. Registrar el callback en TTLock**

TTLock Open Platform Management Center → tu aplicación → Callback URL:
```
https://<tu-proyecto>.vercel.app/api/ttlock-webhook
```

**7. Instalar como servicio Windows con NSSM**

Descargar NSSM desde [nssm.cc](https://nssm.cc/download) y colocar en PATH.

```powershell
nssm install TTLockAlert "C:\ruta\a\python.exe"
nssm set TTLockAlert AppParameters "C:\ruta\ttlock-alert\main.py"
nssm set TTLockAlert AppDirectory "C:\ruta\ttlock-alert"
nssm set TTLockAlert AppStdout "C:\ruta\ttlock-alert\logs\ttlock-alert.log"
nssm set TTLockAlert AppStderr "C:\ruta\ttlock-alert\logs\ttlock-alert.log"
nssm set TTLockAlert Start SERVICE_AUTO_START
nssm start TTLockAlert
```

---

## Configuración — `config.yaml`

```yaml
ttlock:
  client_id: "tu_client_id"          # App credentials de TTLock Open Platform
  client_secret: "tu_client_secret"
  username: "tu_email_ttlock"         # Email de la cuenta TTLock
  password_md5: "md5_de_tu_password"  # MD5 en minúsculas del password TTLock
  lock_id: 0                          # ID numérico de la cerradura
  lock_name: "Puerta Principal"       # Nombre para mostrar en mensajes
  api_url: "https://euapi.ttlock.com" # URL base de la API (euapi para cuentas EU)
  vercel_url: "https://tu-proyecto.vercel.app"
  api_key: "tu_api_key_vercel"        # Debe coincidir con TTLOCKALERT_API_KEY en Vercel
  polling_interval: 5                 # Segundos entre consultas al Vercel Relay
  token_file: "ttlock_token.cache"    # Ruta del archivo de caché del token (no subir a GitHub)
  battery_alert_threshold: 30         # % de batería por debajo del cual se envía alerta

camera:
  rtsp_url: "rtsp://usuario:password@IP:554/ch02/0"  # URL RTSP de la cámara
  ffmpeg_path: "ffmpeg"              # Ruta a ffmpeg o "ffmpeg" si está en PATH
  capture_on_open: true              # Capturar foto en cada apertura exitosa

whatsapp:
  gateway_url: "http://127.0.0.1:3000"  # URL del servicio wa-gateway
  recipients:
    - "521XXXXXXXXXX"   # Número 1 con código de país, sin +
    - ""                # Número 2 (opcional, dejar vacío si no aplica)
    - ""                # Número 3 (opcional)

storage:
  photos_dir: "C:\\ruta\\para\\fotos"  # Carpeta de almacenamiento de fotos
  retention_days: 90                   # Días de retención; 0 = nunca eliminar

monitoring:
  daily_report_time: "08:00"     # Hora de envío del reporte diario (HH:MM)
  daily_report_enabled: true     # false para deshabilitar el reporte diario completamente

email:
  smtp_server: "smtp.gmail.com"
  smtp_port: 587
  sender: "tucorreo@gmail.com"
  password: "app_password_aqui"  # Contraseña de aplicación Gmail (no la contraseña normal)
  recipients:
    - "tucorreo@gmail.com"
```

> **Nota sobre `api_url`**: usar `https://euapi.ttlock.com` para cuentas europeas. Si tu cuenta TTLock fue creada en otra región, consulta la documentación de TTLock Open Platform para la URL correcta.

> **Nota sobre `battery_alert_threshold`**: la alerta de batería se envía máximo una vez cada 24 horas para evitar spam, incluso si la batería sigue baja.

---

## Comandos WhatsApp disponibles

Enviar desde un número autorizado (debe estar en `recipients`). El bot responde **solo al número que envió el comando**. Mensajes de números no autorizados se ignoran sin respuesta. Mensajes que no comiencen con `TT ` se ignoran completamente.

| Comando | Efecto |
|---|---|
| `TT ESTADO` | Reporte inmediato del sistema |
| `TT HISTORIAL` | Últimas 3 aperturas registradas |
| `TT SILENCIAR 2h` | Silencia alertas por 2 horas |
| `TT SILENCIAR 0.5h` | Silencia alertas por 30 minutos |
| `TT REACTIVAR` | Cancela el silencio activo |

**Ejemplos de respuesta:**

`TT ESTADO`
```
📍 Estado — Puerta Principal
WhatsApp gateway: conectado ✅
Batería cerradura: 85%
Último acceso: Juan (Código numérico) — 28/06/2025 14:32
```

`TT HISTORIAL`
```
🔓 Historial — Puerta Principal
━━━━━━━━━━━━━━━━━━━━━
1. Juan (Código numérico) — 28/06/2025 14:32
2. Maria (App Bluetooth) — 28/06/2025 09:15
3. — (Tarjeta IC) — 27/06/2025 20:41
```

`TT SILENCIAR 2h`
```
🔕 Alertas de Puerta Principal silenciadas por 2h (hasta las 16:32)
```

`TT REACTIVAR`
```
🔔 Alertas de Puerta Principal reactivadas.
```

> **Nota**: Los eventos `recordType 10` (puerta forzada) son de prioridad crítica y **nunca son silenciables**, independientemente del estado de silencio.

---

## Monitoreo automático

`HealthMonitor` corre un loop cada 60 segundos que ejecuta tres verificaciones independientes: salud del gateway, nivel de batería y reporte diario.

### Reporte diario automático

Todos los días a la hora configurada en `monitoring.daily_report_time` (por defecto `08:00`), el sistema envía automáticamente a **todos los recipients** un resumen del día anterior:

```
📊 Reporte diario — 29/06/2026
━━━━━━━━━━━━━━━━━━━━━
Batería cerradura: 78%
Aperturas hoy: 5
Intentos fallidos hoy: 1
```

Si las alertas están silenciadas en el momento del envío, se añade una línea adicional:

```
⚠️ Alertas silenciadas hasta las 10:30
```

**Comportamiento:**
- Se envía exactamente una vez por día — si el servicio no estaba corriendo a la hora exacta, el reporte no se recupera; se enviará al día siguiente
- La comparación de hora es `HH:MM` exacto contra el reloj del sistema — si el loop de 60s se salta el minuto exacto (carga del sistema), el reporte espera al siguiente día
- Tras enviar el reporte, se ejecuta automáticamente la limpieza de fotos antiguas

**Deshabilitar:** establecer `daily_report_enabled: false` en `config.yaml`. El loop de monitoreo continúa corriendo normalmente (batería, gateway) pero no envía el reporte ni limpia fotos.

### Limpieza automática de fotos

Se ejecuta inmediatamente después de enviar el reporte diario:

- Elimina todos los archivos `.jpg` en `storage.photos_dir` cuya fecha de modificación sea anterior a `retention_days` días
- Elimina también los directorios vacíos que quedan tras la limpieza (estructura `YYYY/MM/DD/`)
- Si `retention_days: 0`, la limpieza se omite completamente (retención indefinida)
- Los errores al eliminar archivos individuales se loguean como `WARNING` y no detienen la limpieza del resto

### Alerta de batería baja

Cuando el nivel de batería cae por debajo de `battery_alert_threshold` (por defecto `30%`):

```
🔋 Puerta Principal — Batería baja: 22%
Nivel por debajo del umbral configurado (30%).
```

- Se envía a todos los recipients vía `send_alert`
- Máximo una alerta cada 24 horas aunque la batería siga baja
- El nivel de batería se obtiene del campo `electricQuantity` del último evento recibido de TTLock — si el servicio acaba de arrancar y no ha llegado ningún evento aún, la verificación se omite hasta recibir el primero

### Seguimiento de contadores diarios

`register_event(event)` se llama desde `main.py` para **cada evento** recibido, sin importar el tipo:

| Condición | Acción |
|---|---|
| `recordType` en `{1,4,7,8}` y `success == 1` | Suma 1 a `aperturas_hoy`, guarda en historial |
| `recordType == 9` (código incorrecto) | Suma 1 a `intentos_fallidos_hoy` |
| Cualquier otro | Se ignora para contadores |

Los contadores se resetean automáticamente a `0` cuando cambia la fecha del sistema (no al medianoche exacto, sino en el siguiente ciclo del loop que detecte el cambio de día).

El historial guarda las **últimas 3 aperturas exitosas** con usuario, método de acceso y fecha/hora, accesibles vía `TT HISTORIAL` o `TT ESTADO`.

---

## Gestión del servicio NSSM

```powershell
# Iniciar
nssm start TTLockAlert

# Detener
nssm stop TTLockAlert

# Reiniciar
nssm restart TTLockAlert

# Ver estado
nssm status TTLockAlert

# Eliminar servicio (detener primero)
nssm stop TTLockAlert
nssm remove TTLockAlert confirm

# Ver log en tiempo real
Get-Content "C:\ruta\ttlock-alert\logs\ttlock-alert.log" -Wait -Tail 50
```

---

## Archivos que NO están en GitHub (.gitignore)

- `config.yaml` — credenciales reales
- `ttlock_token*.cache` — token de sesión OAuth2 TTLock
- `fotos/` — historial de fotos
- `logs/` — archivos de log
- `__pycache__/` — caché Python

---

## Dependencias

### Python (`requirements.txt`)

```
pyyaml>=6.0
requests>=2.31
```

Instalar: `py -m pip install -r requirements.txt`

### Sistema

| Dependencia | Uso | Instalación |
|---|---|---|
| ffmpeg | Captura frames RTSP | `winget install --id Gyan.FFmpeg` |
| [wa-gateway](https://github.com/r3fear/wa-gateway) | Envío de WhatsApp | Ver repositorio wa-gateway |
| NSSM | Servicio Windows | [nssm.cc](https://nssm.cc/download) |

---

## Notas para IA

- `api_url` es configurable — `https://euapi.ttlock.com` para cuentas EU; consultar TTLock Open Platform para otras regiones
- El campo `password_md5` ya viene en MD5 en la config — se envía directamente a la API sin transformar
- El token OAuth2 de TTLock dura 90 días (7776000 segundos); `_ensure_token()` lo refresca automáticamente cuando quedan menos de 24 horas para expirar; si `refresh_token` falla, reautentica con password grant
- TTLock Cloud **requiere recibir exactamente el string `success`** (HTTP 200) al webhook o reintentará el POST indefinidamente
- Upstash Redis guarda cada evento con TTL de 3600 segundos — si el servidor local no los consume dentro de 1 hora, se pierden permanentemente
- Las variables de Upstash que consume el código son `KV_REST_API_URL` y `KV_REST_API_TOKEN` — estos nombres los inyecta Vercel Marketplace automáticamente; si se conecta Upstash manualmente, mapear los valores de Upstash a estos nombres en Vercel
- `capture_on_open` solo aplica a aperturas exitosas: `success == 1` y `recordType` en `{1, 4, 7, 8}` — nunca captura en intentos fallidos (recordType 9), puerta forzada (10) ni batería baja (11)
- `recordType 10` (puerta forzada) tiene prioridad `critical` y es **nunca silenciable** — el callback en `main.py` ignora `is_silenced()` para esta prioridad
- `process_command()` ignora silenciosamente mensajes sin prefijo `"TT "` — diseño intencional para coexistir con RingAlert u otros servicios que compartan el mismo inbox de wa-gateway
- `poll_inbox()` usa `GET /inbox?consumer=ttlockalert` (no la cola global) — el consumer name es fijo en el código; si wa-gateway se reinicia y pierde el registro, `poll_inbox()` lo detecta (`ok: false` con error de "no registrado") y llama a `register_consumer()` automáticamente antes de reintentar
- `register_consumer()` se llama al arrancar `main.py` solo si wa-gateway está disponible; si falla no es fatal — el auto-retry en `poll_inbox()` lo recupera en el siguiente ciclo de 5s
- `check_gateway_health()` **solo loguea** las transiciones de estado — wa-gateway gestiona sus propios emails de alerta por desconexión/reconexión interna de WhatsApp
- `send_email_fallback()` solo se activa cuando wa-gateway no responde en HTTP (proceso caído); los errores internos de WhatsApp (sesión caída, desconexión) los gestiona wa-gateway con su propio mecanismo de email
- `battery_alert_threshold` es configurable en `config.yaml`; la alerta se envía máximo una vez cada 24 horas aunque la batería siga baja
- wa-gateway debe estar corriendo **antes** de iniciar `main.py` — es un servicio independiente
- Los números de WhatsApp deben incluir código de país sin `+` (ejemplo: `5215512345678` para México)
- El callback de `TTLockMonitor` tiene firma `(message, image_path, priority, event)` donde `event` es el dict raw del relay — necesario para que `health_monitor.register_event()` acceda a `recordType` y `lockDate`
- `register_event()` reemplaza a `keep_last_openings()` — maneja aperturas y fallos en un solo método; los contadores diarios se resetean solos al cambiar la fecha
- `daily_report_enabled: false` en config desactiva el reporte diario sin afectar ninguna otra funcionalidad
