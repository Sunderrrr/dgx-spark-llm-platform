/* Unit tests for the line diff of the playground's file edits (lib/diff.ts).
 * Runs with `npm test` (node --test). */
import { test } from "node:test";
import assert from "node:assert/strict";
import { diffLines, diffStats, hunks } from "../lib/diff.ts";

test("one changed line is one removal and one addition", () => {
  const before = "function greet(name) {\n  return 'Hello, ' + name;\n}\n";
  const after = "function greet(name) {\n  return `Hello, ${name}!`;\n}\n";
  const lines = diffLines(before, after);
  assert.deepEqual(diffStats(lines), { added: 1, removed: 1 });
  const del = lines.find((l) => l.kind === "del")!;
  const add = lines.find((l) => l.kind === "add")!;
  assert.equal(del.oldNo, 2);
  assert.equal(add.newNo, 2);
});

test("an insertion keeps the line numbers of what follows", () => {
  const lines = diffLines("a\nb\nc", "a\nb\nx\nc");
  assert.deepEqual(diffStats(lines), { added: 1, removed: 0 });
  const last = lines[lines.length - 1];
  assert.deepEqual([last.text, last.oldNo, last.newNo], ["c", 3, 4]);
});

test("identical files have no change", () => {
  assert.deepEqual(diffStats(diffLines("x\ny", "x\ny")), { added: 0, removed: 0 });
});

test("a created file is all additions", () => {
  assert.deepEqual(diffStats(diffLines("", "a\nb")), { added: 2, removed: 1 });
});

test("hunks keep context around changes and elide the rest", () => {
  const before = Array.from({ length: 30 }, (_, i) => `line ${i}`).join("\n");
  const after = before.replace("line 15", "LINE 15");
  const h = hunks(diffLines(before, after), 2);
  assert.equal(h[0], null);
  assert.equal(h.filter((l) => l !== null).length, 6);
  assert.equal(h[h.length - 1], null);
});
