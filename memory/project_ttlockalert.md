---
name: project-ttlockalert
description: Context on the TTLock Alert project — smart lock monitoring system with WhatsApp notifications and RTSP camera capture
metadata:
  type: project
---

Sistema de monitoreo de cerradura inteligente TTLock.

**What it does:** Monitorea eventos de una cerradura TTLock (apertura de puerta, intentos fallidos), captura fotos de cámara SWANN vía RTSP, y envía notificaciones por WhatsApp.

**Why:** Seguridad residencial/comercial automatizada.

**External integrations:**
- `wa-gateway` — servicio HTTP local (puerto 3000) para WhatsApp. POST /send, GET /status, GET /inbox, GET /health
- `Vercel Relay` — intermediario HTTP para webhooks TTLock. GET /api/ttlock-events con header x-api-key. Retorna y vacía eventos pendientes con campos: lockId, recordType, success, username, keyboardPwd, lockDate, electricQuantity, serverDate

**Stack:** Python, pyyaml, requests, ffmpeg (RTSP capture)

**Root:** c:\Projects\TTLockAlert

**How to apply:** Al sugerir código, usar Python puro con las librerías del requirements.txt. La config se carga desde config.yaml (excluido del repo, basado en config.yaml.example).
