'use strict';

const path = require('path');
// Load .env from FLOODSAFE root (one level above /backend)
require('dotenv').config({ path: path.join(__dirname, '..', '.env') });

const express = require('express');
const helmet = require('helmet');
const cors = require('cors');
const sosRoutes = require('./routes/sos.routes');

const app = express();
const PORT = process.env.PORT || 3001;

// ── Security headers ──────────────────────────────────────────────────
app.use(
  helmet({
    contentSecurityPolicy: {
      directives: {
        defaultSrc: ["'self'"],
        scriptSrc: ["'self'"],
        styleSrc: ["'self'", "'unsafe-inline'"],
        imgSrc: ["'self'", 'data:'],
        connectSrc: ["'self'"],
      },
    },
  })
);

// ── CORS — allow Vite dev server/preview plus any deployed frontend(s) ──
// CORS_ORIGINS is a comma-separated list of extra allowed origins (e.g. the
// deployed dashboard's URL), added on top of the local dev defaults below.
const extraOrigins = (process.env.CORS_ORIGINS || '')
  .split(',')
  .map((o) => o.trim())
  .filter(Boolean);

app.use(
  cors({
    origin: [
      'http://localhost:5173', // vite dev
      'http://localhost:4173', // vite preview
      ...extraOrigins,
    ],
    methods: ['POST', 'OPTIONS'],
    allowedHeaders: ['Content-Type'],
  })
);

// ── Parse JSON request bodies (tiny limit — SOS payloads are small) ───
app.use(express.json({ limit: '2kb' }));

// ── API routes ────────────────────────────────────────────────────────
app.use('/api', sosRoutes);

// ── Health check ──────────────────────────────────────────────────────
app.get('/health', (_req, res) => {
  res.json({ status: 'ok', service: 'floodsafe-sos-backend' });
});

// ── Start ─────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`[FLOODSAFE SOS Backend] Running at http://localhost:${PORT}`);
  console.log(
    `[FLOODSAFE SOS Backend] Test mode: ${
      process.env.ENABLE_TEST_MODE === 'true' ? 'ENABLED' : 'DISABLED'
    }`
  );
});
