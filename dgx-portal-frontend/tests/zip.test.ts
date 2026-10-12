/* Unit tests for the playground's « Tout télécharger (.zip) » writer
 * (lib/zip.ts). Runs with `npm test` (node --test). The archive is read back
 * by Python's zipfile — an independent unzipper, not our own parser. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { crc32, zipFiles } from "../lib/zip.ts";

test("crc32 matches the standard check value", () => {
  assert.equal(crc32(new TextEncoder().encode("123456789")), 0xcbf43926);
});

test("an independent unzipper reads every file back, accents included", () => {
  const files = [
    { name: "docker-compose.yml", content: "services:\n  garage:\n    image: dxflrs/garage\n" },
    { name: "notes/résumé.md", content: "# Résumé\n\nÉté 2026 — ✓\n" },
    { name: "empty.txt", content: "" },
  ];
  const bytes = zipFiles(files);
  const out = execFileSync("python3", ["-I", "-c", [
    "import io, json, sys, zipfile",
    "z = zipfile.ZipFile(io.BytesIO(sys.stdin.buffer.read()))",
    "assert z.testzip() is None",
    "print(json.dumps({n: z.read(n).decode() for n in z.namelist()}))",
  ].join("\n")], { input: Buffer.from(bytes) });
  assert.deepEqual(JSON.parse(out.toString()), Object.fromEntries(files.map((f) => [f.name, f.content])));
});
