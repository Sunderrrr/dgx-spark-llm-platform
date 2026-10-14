// Which backend serves a path — Flask, or the Rust portal during the Rust
// migration (docs/rust-migration.md).
//
// The ONE list of migrated prefixes is migration/rust-routes.txt (the route
// inventory reads it too), mounted read-only into this container; the Rust
// portal's address is DGX_RUST_URL. Both are read once, at start: moving a
// prefix is editing the file and restarting the frontend. Without
// DGX_RUST_URL every path stays on Flask, whatever the file says.
import { readFileSync } from "node:fs";

export const FLASK_URL = process.env.BACKEND_URL || "http://dgx-portal:5000";
const RUST_URL = process.env.DGX_RUST_URL || "";
const PREFIX_FILE = process.env.DGX_RUST_PREFIX_FILE || "/app/migration/rust-routes.txt";

/** The prefixes of a rust-routes.txt text: one per line, `#` comments. */
export function parsePrefixes(text: string): string[] {
  return text.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#"));
}

function loadPrefixes(): string[] {
  try {
    return parsePrefixes(readFileSync(PREFIX_FILE, "utf8"));
  } catch {
    return [];
  }
}

const PREFIXES = loadPrefixes();

/** Pure decision: `/api/ranking` covers `/api/ranking` and `/api/ranking/…`,
 *  never `/api/rankings`. */
export function routeBackend(pathname: string, rustUrl: string, prefixes: string[], flaskUrl: string): string {
  if (!rustUrl) return flaskUrl;
  const hit = prefixes.some((p) => {
    const base = p.endsWith("/") ? p.slice(0, -1) : p;
    return pathname === base || pathname.startsWith(`${base}/`);
  });
  return hit ? rustUrl : flaskUrl;
}

/** The backend base URL for a request path. */
export function backendFor(pathname: string): string {
  return routeBackend(pathname, RUST_URL, PREFIXES, FLASK_URL);
}

/** Is this path served by the Rust portal? */
export function onRust(pathname: string): boolean {
  return backendFor(pathname) !== FLASK_URL;
}
