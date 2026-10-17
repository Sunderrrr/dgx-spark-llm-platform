// Which backend serves a path — Flask, or the Rust portal during the Rust
// migration (docs/rust-migration.md).
//
// The ONE list of migrated prefixes is migration/rust-routes.txt (the route
// inventory reads it too), mounted read-only into this container; the Rust
// portal's address is DGX_RUST_URL. Both are read once, at start: moving a
// prefix is editing the file and restarting the frontend. Without
// DGX_RUST_URL every path stays on Flask, whatever the file says.
import { readFileSync } from "node:fs";

// The default backend: what no migrated prefix covers. Flask is retired, so
// docker-compose.yml sets BACKEND_URL to the Rust portal (docs/rust-migration.md).
// Built from the port so the default is not a literal copy of a site value (scripts/pre-push-check.sh).
export const FLASK_URL = process.env.BACKEND_URL || `http://portal-rs:${process.env.PORTAL_RUST_PORT || "5100"}`;
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
 *  never `/api/rankings`. An entry may name ONE method first (`POST /keys`):
 *  it then covers that method only — a path that is also a page (GET) can
 *  move its writes without its page. A trailing `$` (`/api/admin$`) covers
 *  that exact path only, not its sub-paths; a `*` segment
 *  (`/admin/users/* /unblock`, without the space) matches any one segment. */
export function routeBackend(pathname: string, rustUrl: string, prefixes: string[], flaskUrl: string, method = "GET"): string {
  if (!rustUrl) return flaskUrl;
  const hit = prefixes.some((entry) => {
    const [only, p] = entry.includes(" ") ? entry.split(/\s+/, 2) : ["", entry];
    if (only && only.toUpperCase() !== method.toUpperCase()) return false;
    const exact = p.endsWith("$");
    const want = (exact ? p.slice(0, -1) : p).replace(/\/$/, "").split("/");
    const got = pathname.split("/");
    if (exact ? got.length !== want.length : got.length < want.length) return false;
    return want.every((seg, i) => seg === got[i] || (seg === "*" && got[i] !== ""));
  });
  return hit ? rustUrl : flaskUrl;
}

/** The backend base URL for a request path (and method). */
export function backendFor(pathname: string, method = "GET"): string {
  return routeBackend(pathname, RUST_URL, PREFIXES, FLASK_URL, method);
}

/** Is this path (with this method) served by the Rust portal? */
export function onRust(pathname: string, method = "GET"): boolean {
  return backendFor(pathname, method) !== FLASK_URL;
}
