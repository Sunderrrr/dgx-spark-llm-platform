/* Unit tests for the voice mode's text side (lib/voice-text.ts). `npm test`. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { isPhantomTranscript, nextSentences, speakable } from "../lib/voice-text.ts";

test("speakable drops what cannot be read aloud", () => {
  const md = "## Garage\n\nAjoute **le réseau** `npm` :\n\n```yaml\nnetworks:\n  - npm\n```\n\n- voir [la doc](https://garagehq.deuxfleurs.fr)\n- puis relance";
  assert.equal(speakable(md), "Garage\nAjoute le réseau npm :\nvoir la doc\npuis relance");
});

test("sentences are released only when complete, the tail at the end", () => {
  const a = "Salut Alex ! Ça va bien, merci. Et toi, quoi de";
  const r1 = nextSentences(a, 0, false);
  assert.deepEqual(r1.sentences.map((s) => s.text), ["Salut Alex ! Ça va bien, merci."]);
  const b = a + " neuf ?";
  const r2 = nextSentences(b, r1.end, true);
  assert.deepEqual(r2.sentences.map((s) => s.text), ["Et toi, quoi de neuf ?"]);
  assert.equal(r2.end, b.length);
  // Offsets index the displayed text (the grey → white highlight).
  assert.equal(b.slice(r2.sentences[0].start, r2.sentences[0].end).trim(), "Et toi, quoi de neuf ?");
});

test("a number's dot at the growing edge is not a sentence end", () => {
  assert.deepEqual(nextSentences("La version 3.", 0, false).sentences, []);
  assert.deepEqual(nextSentences("La version 3.5 est sortie hier soir.", 0, true).sentences.map((s) => s.text),
    ["La version 3.5 est sortie hier soir."]);
});

test("phantom transcripts are ignored", () => {
  assert.equal(isPhantomTranscript("Sous-titres réalisés par la communauté d'Amara.org"), true);
  assert.equal(isPhantomTranscript(" … "), true);
  assert.equal(isPhantomTranscript("Thank you."), true);
  assert.equal(isPhantomTranscript("Salut, comment tu vas ?"), false);
});
