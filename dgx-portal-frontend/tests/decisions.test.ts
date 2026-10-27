/**
 * The « Décisions » request format and answer rendering helpers.
 *
 * Run with `npm test` (node --test, like the other frontend unit tests): these
 * cases are the screen's contract with the API, and the backend validates the
 * same shape again — a drift between the two would only surface as a 400 far
 * from the form.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_LEVELS,
  MAX_QUESTIONS,
  answerHeadline,
  answerOptions,
  buildDecisionRequest,
  formatProbability,
  parseState,
  splitOptions,
  type QuestionDraft,
} from "../lib/decisions.ts";

const noul = (id: string): QuestionDraft => ({ id, type: "noul", instructions: "Est-ce urgent ?", options: "" });

test("un state texte est envoyé tel quel", () => {
  assert.deepEqual(parseState("  Client mécontent  ", false), { ok: true, value: "  Client mécontent  " });
});

test("un state JSON est analysé", () => {
  const out = parseState('{"commande": 4411}', true);
  assert.equal(out.ok, true);
  if (out.ok) assert.deepEqual(out.value, { commande: 4411 });
});

test("un état vide ou un JSON invalide est refusé", () => {
  assert.equal(parseState("   ", false).ok, false);
  assert.equal(parseState("{oops", true).ok, false);
});

test("les options se séparent par virgule ou retour à la ligne", () => {
  assert.deepEqual(splitOptions("a, b\nc ,, a"), ["a", "b", "c", "a"]);
});

test("noul, choice et score produisent le corps System One", () => {
  const drafts: QuestionDraft[] = [
    noul("urgent"),
    { id: "equipe", type: "choice", instructions: "Quelle équipe ?", options: "facturation, livraison" },
    { id: "urgence", type: "score", instructions: "À quel point ?", options: "peut attendre, cette semaine, aujourd'hui" },
  ];
  const built = buildDecisionRequest("Client facturé deux fois.", false, drafts);
  assert.equal(built.ok, true);
  if (!built.ok) return;
  assert.deepEqual(built.body.questions, {
    urgent: { type: "noul", instructions: "Est-ce urgent ?" },
    equipe: { type: "choice", instructions: "Quelle équipe ?", criteria: { facturation: null, livraison: null } },
    urgence: { type: "score", instructions: "À quel point ?", criteria: ["peut attendre", "cette semaine", "aujourd'hui"] },
  });
  assert.equal(built.body.state, "Client facturé deux fois.");
});

test("une question sans énoncé est refusée et nommée", () => {
  const built = buildDecisionRequest("s", false, [{ id: "x", type: "noul", instructions: "  ", options: "" }]);
  assert.equal(built.ok, false);
  if (!built.ok) assert.match(built.error, /x/);
});

test("un choix à une seule option est refusé", () => {
  const built = buildDecisionRequest("s", false, [{ id: "c", type: "choice", instructions: "?", options: "seul" }]);
  assert.equal(built.ok, false);
  if (!built.ok) assert.match(built.error, /2 à 255/);
});

test("un score hors 2–10 niveaux est refusé", () => {
  const many = Array.from({ length: MAX_LEVELS + 1 }, (_, i) => `n${i}`).join(", ");
  const built = buildDecisionRequest("s", false, [{ id: "n", type: "score", instructions: "?", options: many }]);
  assert.equal(built.ok, false);
});

test("un identifiant en double est refusé", () => {
  const built = buildDecisionRequest("s", false, [noul("a"), noul("a")]);
  assert.equal(built.ok, false);
  if (!built.ok) assert.match(built.error, /double/);
});

test("plus de questions que la limite est refusé", () => {
  const drafts = Array.from({ length: MAX_QUESTIONS + 1 }, (_, i) => noul(`q${i}`));
  assert.equal(buildDecisionRequest("s", false, drafts).ok, false);
});

test("sans question, rien n'est envoyé", () => {
  assert.equal(buildDecisionRequest("s", false, []).ok, false);
});

test("les lignes vides ne sont pas des questions", () => {
  const drafts: QuestionDraft[] = [
    { id: "", type: "noul", instructions: "", options: "" },
    noul("vraie"),
  ];
  const built = buildDecisionRequest("s", false, drafts);
  assert.equal(built.ok, true);
  if (built.ok) assert.deepEqual(Object.keys(built.body.questions), ["vraie"]);
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
  const rows = answerOptions({ probabilities: { a: 0.1, b: 0.9 } });
  assert.deepEqual(rows, [["b", 0.9], ["a", 0.1]]);
});
