/*
 * SETUP — leer antes de desplegar
 * ─────────────────────────────────────────────────────────────────────────────
 * Variables de entorno requeridas en Vercel Dashboard → Settings → Environment Variables:
 *   KV_REST_API_URL          → se inyecta automáticamente al conectar Upstash vía Vercel Marketplace
 *   KV_REST_API_TOKEN        → se inyecta automáticamente al conectar Upstash vía Vercel Marketplace
 *
 * Cómo crear la base de datos Redis:
 *   1. En Vercel Dashboard → Storage → Connect Store → Upstash KV (Marketplace)
 *   2. Crear la base de datos desde ahí; Vercel inyecta KV_REST_API_URL y KV_REST_API_TOKEN automáticamente
 *   - Alternativa: crear cuenta en https://upstash.com y agregar KV_REST_API_URL / KV_REST_API_TOKEN manualmente
 *
 * URL del webhook a registrar en TTLock Open Platform Management Center:
 *   https://<tu-proyecto>.vercel.app/api/ttlock-webhook
 *   Método: POST  |  Formato: application/x-www-form-urlencoded
 * ─────────────────────────────────────────────────────────────────────────────
 */

const { Redis } = require('@upstash/redis');

const redis = new Redis({
  url: process.env.KV_REST_API_URL,
  token: process.env.KV_REST_API_TOKEN,
});

module.exports = async function handler(req, res) {
  // TTLock siempre hace POST; responder 200 con "success" a todo lo demás
  // para evitar reintentos en caso de que el validador de TTLock haga GET.
  if (req.method !== 'POST') {
    return res.status(200).send('success');
  }

  try {
    const { notifyType, lockId, lockMac, records } = req.body || {};

    const parsedRecords = JSON.parse(records || '[]');

    if (!Array.isArray(parsedRecords) || parsedRecords.length === 0) {
      console.log(`TTLock webhook: lockId=${lockId} notifyType=${notifyType} — no records`);
      return res.status(200).send('success');
    }

    const eventIds = [];

    for (let i = 0; i < parsedRecords.length; i++) {
      const record = parsedRecords[i];
      const ts = record.lockDate || Date.now();
      const eventId = `ttlock:event:${lockId}:${ts}:${i}`;

      // Enrich with outer-envelope fields in case they're useful
      const storedEvent = {
        ...record,
        lockId: record.lockId ?? lockId,
        lockMac: lockMac,
        notifyType: notifyType,
      };

      await redis.set(eventId, JSON.stringify(storedEvent), { ex: 3600 });
      eventIds.push(eventId);

      console.log(
        `TTLock event stored: eventId=${eventId} lockId=${lockId} ` +
        `recordType=${record.recordType} success=${record.success} ` +
        `username=${record.username || ''}`
      );
    }

    if (eventIds.length > 0) {
      await redis.rpush('ttlock:pending', ...eventIds);
    }
  } catch (err) {
    // Log but never let an error change the response — TTLock must always get "success"
    console.error('TTLock webhook processing error:', err);
  }

  // TTLock Cloud requiere exactamente este body para considerar la entrega exitosa.
  // Cualquier otra respuesta provoca reintentos.
  res.status(200).send('success');
};
