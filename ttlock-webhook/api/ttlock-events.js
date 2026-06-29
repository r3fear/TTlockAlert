/*
 * SETUP — leer antes de desplegar
 * ─────────────────────────────────────────────────────────────────────────────
 * Variables de entorno requeridas en Vercel Dashboard → Settings → Environment Variables:
 *   KV_REST_API_URL          → se inyecta automáticamente al conectar Upstash vía Vercel Marketplace
 *   KV_REST_API_TOKEN        → se inyecta automáticamente al conectar Upstash vía Vercel Marketplace
 *   TTLOCKALERT_API_KEY      → crear manualmente; usar el mismo valor que api_key en config.yaml
 *
 * Cómo crear la base de datos Redis:
 *   1. En Vercel Dashboard → Storage → Connect Store → Upstash KV (Marketplace)
 *   2. Crear la base de datos desde ahí; Vercel inyecta KV_REST_API_URL y KV_REST_API_TOKEN automáticamente
 *   - Alternativa: crear cuenta en https://upstash.com y agregar KV_REST_API_URL / KV_REST_API_TOKEN manualmente
 *
 * Consumido por el servidor local vía:
 *   GET https://<tu-proyecto>.vercel.app/api/ttlock-events
 *   Header: x-api-key: <valor de TTLOCKALERT_API_KEY>
 * ─────────────────────────────────────────────────────────────────────────────
 */

const { Redis } = require('@upstash/redis');

const redis = new Redis({
  url: process.env.KV_REST_API_URL,
  token: process.env.KV_REST_API_TOKEN,
});

module.exports = async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const apiKey = req.headers['x-api-key'];
  if (!apiKey || apiKey !== process.env.TTLOCKALERT_API_KEY) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  try {
    const pendingIds = await redis.smembers('ttlock:pending');

    if (!pendingIds || pendingIds.length === 0) {
      return res.status(200).json({ events: [] });
    }

    // Fetch all events in one round-trip, then delete the pending set
    const rawEvents = await redis.mget(...pendingIds);
    await redis.del('ttlock:pending');

    const events = rawEvents
      .filter(e => e !== null && e !== undefined)
      .map(e => typeof e === 'string' ? JSON.parse(e) : e);

    console.log(`ttlock-events: returned ${events.length} event(s), cleared pending list`);

    return res.status(200).json({ events });
  } catch (err) {
    console.error('ttlock-events error:', err);
    return res.status(500).json({ error: 'Internal server error' });
  }
};
