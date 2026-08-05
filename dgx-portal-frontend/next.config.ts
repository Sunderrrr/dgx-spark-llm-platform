import type { NextConfig } from "next";
import path from "node:path";

const BACKEND_URL = process.env.BACKEND_URL || "http://dgx-portal:5000";

// Content-Security-Policy is set per-request in proxy.ts instead of here: it
// needs a fresh nonce on script-src for every render, which a static header
// value can't provide.

const nextConfig: NextConfig = {
  output: "standalone",
  // Retire l'en-tête X-Powered-By: Next.js — aucun intérêt fonctionnel,
  // juste du fingerprinting gratuit offert à un attaquant.
  poweredByHeader: false,
  // /root/package-lock.json (bac à sable Astryx, hors de ce projet) fait sinon
  // dériver Next vers la mauvaise racine de workspace.
  turbopack: {
    root: path.join(__dirname),
  },
  experimental: {
    // Le proxy.ts middleware (route TOUT le non-GET/HEAD vers Flask) tronque
    // silencieusement le corps des requêtes au-delà de 10 Mo par défaut — cassait
    // les uploads image de /api/video/generate et /api/ocr/extract (limite
    // affichée : 15 Mo). Relevé pour matcher + marge. Vu en prod le 04/08 :
    // un screenshot de ~11 Mo faisait planter le proxy en ECONNRESET.
    proxyClientMaxBodySize: "20mb",
  },
  async rewrites() {
    // Le navigateur ne parle qu'à ce serveur Next.js (même origine, pas de CORS
    // à gérer côté cookies/CSRF). Tout ce qui n'est pas une page Next connue
    // (API JSON, /login POST, /playground/chat SSE, /admin/*, etc.) est transmis
    // tel quel à Flask, joignable en interne via le réseau docker-compose.
    return {
      fallback: [{ source: "/:path*", destination: `${BACKEND_URL}/:path*` }],
    };
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
