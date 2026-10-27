/**
 * The « Décisions » workbench's contract: the two JSON editors' values, the
 * answers LiteLLM returns, and nothing else. Run with `npm test`.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_LEVELS,
  MAX_QUESTIONS,
  answerHeadline,
  answerOptions,
  answerScale,
  answerTypeLabel,
  formatProbability,
  parseState,
  prettyJson,
  validatePayload,
  type DecisionAnswer,
} from "../lib/decisions.ts";

const QUESTIONS = JSON.stringify({
  urgent: { type: "noul", instructions: "Est-ce urgent ?" },
  equipe: { type: "choice", instructions: "Quelle équipe ?", criteria: { facturation: null, livraison: null } },
  urgence: { type: "score", instructions: "À quel point ?", criteria: ["peut attendre", "cette semaine", "aujourd'hui"] },
});

test("un state JSON est analysé", () => {
  const out = parseState('{"commande": 4411}');
  assert.equal(out.ok, true);
  if (out.ok) assert.deepEqual(out.value, { commande: 4411 });
});

test("un state en texte libre est envoyé tel quel", () => {
  const out = parseState("Client mécontent, facturé deux fois");
  assert.equal(out.ok, true);
  if (out.ok) assert.equal(out.value, "Client mécontent, facturé deux fois");
});

test("un state vide est refusé", () => {
  assert.equal(parseState("   ").ok, false);
});

test("le bouton Formater réindente, et ne casse rien sur un JSON invalide", () => {
  assert.equal(prettyJson('{"a":1}'), '{\n  "a": 1\n}');
  assert.equal(prettyJson("{oops"), null);
});

test("les trois types passent, dans la forme System One", () => {
  const built = validatePayload("Client facturé deux fois.", QUESTIONS);
  assert.equal(built.ok, true);
  if (!built.ok) return;
  assert.deepEqual(built.body.questions, {
    urgent: { type: "noul", instructions: "Est-ce urgent ?" },
    equipe: { type: "choice", instructions: "Quelle équipe ?", criteria: { facturation: null, livraison: null } },
    urgence: { type: "score", instructions: "À quel point ?", criteria: ["peut attendre", "cette semaine", "aujourd'hui"] },
  });
  assert.equal(built.body.state, "Client facturé deux fois.");
});

test("un state JSON imbriqué est accepté", () => {
  assert.equal(validatePayload('{"commande": {"id": 4411, "double": true}}', QUESTIONS).ok, true);
});

test("un JSON de questions invalide est refusé et nommé", () => {
  const built = validatePayload("s", "{ pas du json");
  assert.equal(built.ok, false);
  if (!built.ok) assert.match(built.error, /JSON des questions/);
});

test("un type inconnu est refusé", () => {
  const built = validatePayload("s", JSON.stringify({ x: { type: "predicate", instructions: "?" } }));
  assert.equal(built.ok, false);
  if (!built.ok) assert.match(built.error, /predicate/);
});

test("une question sans énoncé est refusée et nommée", () => {
  const built = validatePayload("s", JSON.stringify({ x: { type: "noul", instructions: "  " } }));
  assert.equal(built.ok, false);
  if (!built.ok) assert.match(built.error, /x/);
});

test("un choix à une seule option est refusé", () => {
  const built = validatePayload("s", JSON.stringify({ c: { type: "choice", instructions: "?", criteria: { a: null } } }));
  assert.equal(built.ok, false);
  if (!built.ok) assert.match(built.error, /2 à 255/);
});

test("un score hors 2–10 niveaux est refusé", () => {
  const many = Array.from({ length: MAX_LEVELS + 1 }, (_, i) => `n${i}`);
  assert.equal(validatePayload("s", JSON.stringify({ n: { type: "score", instructions: "?", criteria: many } })).ok, false);
});

test("plus de questions que la limite est refusé", () => {
  const drafts = Object.fromEntries(Array.from({ length: MAX_QUESTIONS + 1 }, (_, i) => [`q${i}`, { type: "noul", instructions: "?" }]));
  assert.equal(validatePayload("s", JSON.stringify(drafts)).ok, false);
});

test("sans question, rien n'est envoyé", () => {
  assert.equal(validatePayload("s", "{}").ok, false);
});

test("les probabilités s'affichent en pourcentage", () => {
  assert.equal(formatProbability(0.7342), "73 %");
  assert.equal(formatProbability(undefined), "—");
});

test("la réponse en évidence suit le type", () => {
  assert.equal(answerHeadline({ type: "noul", noul: 0.73 }), "73 %");
  assert.equal(answerHeadline({ type: "choice", choice: "facturation" }), "facturation");
  assert.equal(answerHeadline({ type: "score", score: 1.2, legend: { "1": "cette semaine" } }), "1.20 — cette semaine");
});

test("les options d'une réponse sont triées par probabilité", () => {
  assert.deepEqual(answerOptions({ probabilities: { a: 0.1, b: 0.9 } }), [["b", 0.9], ["a", 0.1]]);
});

test("le type et l'échelle sont nommés comme dans la colonne de droite", () => {
  const score: DecisionAnswer = { type: "score", score: 0.62, legend: { "0": "a", "1": "b" }, probabilities: { "0": 0.38, "1": 0.62 } };
  assert.equal(answerTypeLabel(score), "Score");
  assert.equal(answerScale(score), "2 niveaux · 0~1");
  assert.equal(answerTypeLabel({ type: "choice", choice: "x" }), "Choice");
  assert.equal(answerScale({ type: "noul", noul: 0.5 }), null);
});
