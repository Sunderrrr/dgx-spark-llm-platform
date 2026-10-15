/* Rust migration switch (lib/backend.ts): which backend serves a path. */
import { test } from "node:test";
import assert from "node:assert/strict";

import { parsePrefixes, routeBackend } from "../lib/backend.ts";

const FLASK = "http://flask";
const RUST = "http://rust";

test("everything stays on Flask without a Rust address", () => {
  assert.equal(routeBackend("/api/ranking", "", ["/api/ranking"], FLASK), FLASK);
});

test("a prefix covers itself and its sub-paths, not a longer name", () => {
  const p = ["/api/ranking"];
  assert.equal(routeBackend("/api/ranking", RUST, p, FLASK), RUST);
  assert.equal(routeBackend("/api/ranking/activity", RUST, p, FLASK), RUST);
  assert.equal(routeBackend("/api/rankings", RUST, p, FLASK), FLASK);
  assert.equal(routeBackend("/api/home", RUST, p, FLASK), FLASK);
});

test("a trailing slash in the list changes nothing", () => {
  assert.equal(routeBackend("/healthz", RUST, ["/healthz/"], FLASK), RUST);
});

test("the list file: one prefix per line, comments and blanks ignored", () => {
  assert.deepEqual(parsePrefixes("# header\n\n/api/ranking\n  /healthz  \n# /admin\n"), ["/api/ranking", "/healthz"]);
});

test("an entry naming a method covers that method only", () => {
  const p = ["POST /keys", "/api/memory"];
  assert.equal(routeBackend("/keys", RUST, p, FLASK, "POST"), RUST);
  assert.equal(routeBackend("/keys", RUST, p, FLASK, "GET"), FLASK);
  assert.equal(routeBackend("/keys", RUST, p, FLASK), FLASK);
  assert.equal(routeBackend("/keys/x", RUST, p, FLASK, "post"), RUST);
  assert.equal(routeBackend("/api/memory", RUST, p, FLASK, "DELETE"), RUST);
});
