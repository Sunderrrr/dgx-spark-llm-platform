/* Unit tests for the playground model-output parsers
 * (lib/playground-parsers.ts) — the model→UI contract.
 *
 * The shapes below are the REAL ones seen in production (mostly MiMo-V2.6
 * on 2026-10-01/02): proper ```ask fences, the ```json fallback, the
 * degenerate « `ask` » + bare JSON form, truncated JSON, trailing junk,
 * mismatched closers, the three resume shapes and the HTML full rewrite.
 * If a test here has to change, the model invented a new form — accommodate
 * it in the PARSER, never with a prompt instruction.
 *
 * Runs with the package's `npm test` (Node ≥ 23 executes TS directly,
 * no dependency added): `node --test tests/*.test.ts`. */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  MAX_ASK_QUESTIONS,
  MAX_ASK_OPTIONS,
  RE_CORPS_QUESTIONS,
  estBlocQuestions,
  parseAsk,
  parseEdits,
  escapeRawControlChars,
  firstJsonValue,
  balanceJson,
  reparerFermetures,
  contenuCloture,
  corpsDeSuite,
  recoller,
  decoupeLignes,
  openCodeFence,
  contientQuestions,
  reponseIncomplete,
} from "../lib/playground-parsers.ts";

/* ── parseAsk ─────────────────────────────────────────────────────────────── */

test("parseAsk: proper ```ask fence with {\"questions\":[…]}", () => {
  const r = parseAsk(
    "Avant de commencer :\n"
    + "```ask\n"
    + '{"questions": [{"question": "Quel format ?", "options": ["PDF", "Word"]}]}\n'
    + "```"
  );
  assert.ok(r);
  assert.equal(r.questions.length, 1);
  assert.equal(r.questions[0].question, "Quel format ?");
  assert.deepEqual(r.questions[0].options, ["PDF", "Word"]);
  assert.equal(r.prose, "Avant de commencer :");
});

test("parseAsk: ```json fence with {\"questions\":[…]} (MiMo 2026-10-01)", () => {
  const r = parseAsk(
    "```json\n"
    + '{"questions": [{"question": "Q1", "options": ["A", "B"]}, {"question": "Q2", "options": ["C"]}]}\n'
    + "```"
  );
  assert.ok(r);
  assert.equal(r.questions.length, 2);
  assert.deepEqual(r.questions[1], { question: "Q2", options: ["C"] });
  assert.equal(r.prose, "");
});

test("parseAsk: DEGENERATE form — `ask` inline code + bare JSON, no fence (MiMo 2026-10-02)", () => {
  const r = parseAsk(
    "Quelques questions avant de continuer :\n"
    + "`ask`\n"
    + '{"questions": [{"question": "Quelle langue ?", "options": ["Français", "Anglais"]}]}'
  );
  assert.ok(r);
  assert.equal(r.questions.length, 1);
  assert.equal(r.questions[0].question, "Quelle langue ?");
  assert.deepEqual(r.questions[0].options, ["Français", "Anglais"]);
  assert.equal(r.prose, "Quelques questions avant de continuer :");
});

test("parseAsk: bare « ask » + JSON, no fence", () => {
  const r = parseAsk(
    'ask\n{"questions": [{"question": "Q1", "options": ["A"]}]}'
  );
  assert.ok(r);
  assert.deepEqual(r.questions, [{ question: "Q1", options: ["A"] }]);
});

test("parseAsk: a normal message with no questions → null", () => {
  assert.equal(parseAsk("Bonjour ! Voici votre fichier."), null);
  // A prose line that merely contains the word is not a questionnaire…
  assert.equal(parseAsk("Voici `ask` suivi de texte normal."), null);
  // …and a JSON file that is not a questionnaire is not swallowed either.
  assert.equal(parseAsk('```json\n{"autre": 1}\n```'), null);
});

test("parseAsk: truncated JSON (unclosed bracket) → repaired by the repair chain", () => {
  const r = parseAsk(
    "```ask\n"
    + '{"questions": [{"question": "Q1 ?", "options": ["A", "B"'
  );
  assert.ok(r, "truncated JSON must be rebalanced, not dropped");
  assert.equal(r.questions.length, 1);
  assert.equal(r.questions[0].question, "Q1 ?");
  assert.deepEqual(r.questions[0].options, ["A", "B"]);
});

test("parseAsk: trailing garbage after a complete object → still parsed", () => {
  const r = parseAsk(
    "```ask\n"
    + '{"questions": [{"question": "Q1", "options": ["A"]}]}\n'
    + '"}\n'
    + "```"
  );
  assert.ok(r);
  assert.deepEqual(r.questions, [{ question: "Q1", options: ["A"] }]);
});

test("parseAsk: legacy {question, options} single object is normalized", () => {
  const r = parseAsk('```ask\n{"question": "Ancien format ?", "options": ["Oui", "Non"]}\n```');
  assert.ok(r);
  assert.deepEqual(r.questions, [{ question: "Ancien format ?", options: ["Oui", "Non"] }]);
});

test("parseAsk: options/questions caps and filtering", () => {
  const questions = Array.from({ length: 30 }, (_, i) => ({
    question: `Q${i}`,
    options: ["", "  ", ...Array.from({ length: 12 }, (_, j) => `o${j}`)],
  }));
  const r = parseAsk('```ask\n' + JSON.stringify({ questions }) + "\n```");
  assert.ok(r);
  assert.equal(r.questions.length, MAX_ASK_QUESTIONS);
  // empty/blank options are dropped, the rest capped
  assert.equal(r.questions[0].options.length, MAX_ASK_OPTIONS);
});

test("parseAsk: éparpillé après un bloc raté — les questions restent cliquables (2026-10-05)", () => {
  // Contenu RÉELLEMENT vu sur la plateforme le 2026-10-05 : une enveloppe
  // {"questions": []} vide refermée tout de suite, l'intro mêlée à des débris
  // JSON, un objet à moitié écrit (« "Qu "options" », sans virgule), puis les
  // vraies questions posées dans le texte. Avant : le parseur rendait les mains
  // et le questionnaire sortait en texte nu.
  const r = parseAsk(
    '{"questions": []}```\n'
    + 'Bien sûr ! Quelques Quel même Bien sûr ! quelques ["Firewall nftables", "SSH", "Pare-feu", "UE", "Deux SSH", "  - "Firewall nftables", " " {"question": "Qu "options": ["Phare", "SSH"],\n'
    + '{"question": "Quel rôle Ansible veux-tu ?", "options": ["Durcissement", "Kubernetes"]}, {"question": "Quelle rôle pour le durcissement", "options": ["Audit", "Sysctl"]}]}\n'
    + '{"question": "Quel(s) de configuration veux-tu personnaliser ?", "options": ["SSH", "Pare-feu (nftables)", "Tout appliquer"]}\n'
    + '{"question": "Comment veux-tu lancer le rôle ?", "options": ["Playbook complet (site.yml)", "Directement en production"]}\n'
    + '{"question": "Quel niveau de confirmation interactive ?", "options": ["Une seule confirmation globale", "Aucune confirmation"]}'
  );
  assert.ok(r, "les questions éparpillées doivent être ramassées");
  assert.equal(r.questions.length, 5);
  assert.equal(r.questions[0].question, "Quel rôle Ansible veux-tu ?");
  assert.equal(r.questions[4].question, "Quel niveau de confirmation interactive ?");
  // L'objet cassé n'est pas une question, et aucun débris JSON ne reste
  // dans la prose (le modèle avait mêlé son intro à ses questions).
  assert.ok(!r.questions.some((q) => q.question === "Qu"));
  assert.ok(!/[{[]/.test(r.prose), `débris JSON dans la prose : ${r.prose}`);
});

test("parseAsk: envelope vide puis objets nus — sans aucun repère « ask »", () => {
  const r = parseAsk(
    '{"questions": []}\n'
    + '{"question": "Q1", "options": ["A", "B"]}\n'
    + '{"question": "Q2", "options": ["C"]}'
  );
  assert.ok(r);
  assert.deepEqual(r.questions.map((q) => q.question), ["Q1", "Q2"]);
});

test("parseAsk: un vrai fichier qui contient des objets de question n'est pas mangé", () => {
  const r = parseAsk(
    "Voici le fichier demandé :\n"
    + "```html\n<script>\nconst q = {\"question\": \"Une FAQ ?\", \"options\": [\"Oui\", \"Non\"]};\n</script>\n```"
  );
  assert.equal(r, null);
});

test("reponseIncomplete: un questionnaire n'est JAMAIS un fichier à reprendre (2026-10-05)", () => {
  // Le mécanisme de reprise automatique (« reprends au caractère suivant »)
  // voyait un fichier « texte » non refermé derrière le marqueur « ``` »
  // orphelin d'un bloc ask raté : il relançait le modèle, qui RÉÉCRIVAIT son
  // questionnaire — les deux tentatives s'entremêlaient et les questions
  // sortaient en texte nu. C'est la cause de « les questions ne marchent plus ».
  const casse = '{"questions": []}```\nBien sûr ! quelques ["Firewall", "SSH"] {"question": "Q1", "options": ["A", "B"]}';
  assert.equal(contientQuestions(casse), true);
  assert.equal(reponseIncomplete(casse), false, "un questionnaire ne doit PAS déclencher de reprise");
  // Un bloc ask propre non plus.
  assert.equal(reponseIncomplete('```ask\n{"questions": [{"question": "Q1", "options": ["A"]}]'), false);
});

test("reponseIncomplete: un vrai fichier tronqué reste repris", () => {
  assert.equal(reponseIncomplete("```python\ndef carre(n):\n    return n *"), true);
  assert.equal(reponseIncomplete("```html\n<!DOCTYPE html><html><body>ok"), true);
  // …et un HTML fini, même sans clôture de bloc, ne l'est pas.
  assert.equal(reponseIncomplete("```html\n<!DOCTYPE html><html><body>ok</body></html>"), false);
  assert.equal(contientQuestions("```python\nq = {\"question\": \"x\", \"options\": [\"a\"]}\n```"), false);
});

/* ── estBlocQuestions / RE_CORPS_QUESTIONS ────────────────────────────────── */

test("estBlocQuestions: ask label accepts any body", () => {
  assert.equal(estBlocQuestions("ask", '{"questions": ['), true);
  assert.equal(estBlocQuestions("ASK", "n'importe quoi"), true);
});

test("estBlocQuestions: json and empty label only behind the {\"questions\": [ guard", () => {
  assert.equal(estBlocQuestions("json", '{"questions": ['), true);
  assert.equal(estBlocQuestions("", '  { "questions" : [\n…'), true);
  assert.equal(estBlocQuestions("json", '{"autre": 1}'), false);
  assert.equal(estBlocQuestions("", "du texte"), false);
  // the guard is about the BODY: another language is never a questionnaire
  assert.equal(estBlocQuestions("python", '{"questions": ['), false);
});

test("RE_CORPS_QUESTIONS: the guard matches the measured shape only", () => {
  assert.equal(RE_CORPS_QUESTIONS.test('{"questions": ['), true);
  assert.equal(RE_CORPS_QUESTIONS.test('\n  {\n  "questions" : [ '), true);
  assert.equal(RE_CORPS_QUESTIONS.test('{"question": "x"}'), false);
});

/* ── recoller / corpsDeSuite — the three resume shapes + HTML rewrite ─────── */

const L = [
  "function alpha() { return 1; }",
  "function beta() { return 22; }",
  "function gamma() { return 333; }",
  "function delta() { return 4444; }",
];

test("corpsDeSuite: reopened fence → the block body", () => {
  const suite = "```html\n<!DOCTYPE html>\n<html><body>x</body></html>\n";
  assert.equal(corpsDeSuite(suite), "<!DOCTYPE html>\n<html><body>x</body></html>\n");
});

test("corpsDeSuite: raw continuation that CLOSES the block → what precedes the closer", () => {
  const suite = "fin du code\nencore une ligne\n```";
  assert.equal(corpsDeSuite(suite), "fin du code\nencore une ligne");
});

test("corpsDeSuite: no fence at all → the whole message", () => {
  const suite = "voici la suite brute du fichier";
  assert.equal(corpsDeSuite(suite), suite);
});

test("recoller: the model re-emits the cut block → spliced once, no duplication", () => {
  const base = L.join("\n");                       // cut after « delta »
  const suite = L.slice(1).join("\n") + "\nfunction epsilon() { return 5; }";
  const r = recoller(base, suite);
  assert.equal(r, L[0] + "\n" + suite);
  for (const l of L) assert.equal(r.split(l).length - 1, 1, `duplicated: ${l}`);
});

test("recoller: cut mid-line + resume at the word (« p_del » / « del = … »)", () => {
  const base = "import argparse\np_del";
  const suite = "del = sub.add_parser('run')\nargs = p.parse_args()";
  assert.equal(recoller(base, suite), "import argparse\np_del = sub.add_parser('run')\nargs = p.parse_args()");
});

test("recoller: HTML full rewrite (≥ 50 % of base) REPLACES the file", () => {
  const base = "<html>\n" + "<!-- corps -->\n".repeat(12) + "</html>";
  const suite = "<!DOCTYPE html>\n<html><body>\n"
    + "<p>ligne de contenu</p>\n".repeat(5)
    + "</body></html>";
  assert.ok(suite.length >= base.length * 0.5, "test premise: suite carries its weight");
  assert.equal(recoller(base, suite), suite);
});

test("recoller: a small HTML-looking resume does NOT wipe the file", () => {
  const base = "<html>\n" + "<!-- corps -->\n".repeat(120) + "</html>";
  const suite = "<html>\n<p>trois lignes</p>\n</html>";
  assert.ok(suite.length < base.length * 0.5, "test premise: suite is small");
  const r = recoller(base, suite);
  assert.notEqual(r, suite);
  assert.ok(r.includes("<!-- corps -->"), "the original body must survive");
});

/* ── contenuCloture ───────────────────────────────────────────────────────── */

test("contenuCloture: odd fence count gets closed, even stays untouched", () => {
  const ouvert = "texte\n```js\nconst a = 1;";
  assert.equal(contenuCloture(ouvert), ouvert + "\n```");
  const ferme = "```js\nconst a = 1;\n```";
  assert.equal(contenuCloture(ferme), ferme);
  const sansFence = "aucune fence ici";
  assert.equal(contenuCloture(sansFence), sansFence);
});

/* ── firstJsonValue / balanceJson / reparerFermetures ─────────────────────── */

test("firstJsonValue: cuts at the end of the first complete value (orphan quote junk)", () => {
  assert.equal(firstJsonValue('{"questions": []}"'), '{"questions": []}');
  assert.equal(firstJsonValue('avant {"a": [1, 2]} après'), '{"a": [1, 2]}');
  assert.equal(firstJsonValue('{"a": [1, 2'), null);   // truncation → balanceJson
});

test("balanceJson: closes what the model left open", () => {
  assert.equal(balanceJson('{"a": "b'), '{"a": "b"}');
  const repare = balanceJson('{"questions": [{"question": "Q1 ?", "options": ["A", "B"');
  assert.deepEqual(JSON.parse(repare), {
    questions: [{ question: "Q1 ?", options: ["A", "B"] }],
  });
});

test("reparerFermetures: mismatched closer (« [\"a\", \"b\"} ») is repaired (MiMo 2026-10-01)", () => {
  const repare = reparerFermetures('{"questions": [{"question": "Q1", "options": ["a", "b"}]}]}');
  assert.deepEqual(JSON.parse(repare), {
    questions: [{ question: "Q1", options: ["a", "b"] }],
  });
});

/* ── escapeRawControlChars / openCodeFence / decoupeLignes / parseEdits ──── */

test("escapeRawControlChars: raw line breaks inside a JSON string are escaped", () => {
  const src = '{"replace": "ligne1\nligne2"}';   // real newline inside the string
  assert.deepEqual(JSON.parse(escapeRawControlChars(src)), { replace: "ligne1\nligne2" });
});

test("openCodeFence: only the never-closed, non-questions block is live", () => {
  const live = openCodeFence("```python\nprint(1)");
  assert.deepEqual(live && { lang: live.lang, body: live.body }, { lang: "python", body: "print(1)" });
  assert.equal(openCodeFence("```python\nprint(1)\n```"), null);       // closed
  assert.equal(openCodeFence("```ask\n{\"questions\": ["), null);       // protocol, not a file
});

test("decoupeLignes: positions match the source offsets", () => {
  const lignes = decoupeLignes("ab\ncd", 10);
  assert.deepEqual(lignes, [
    { texte: "ab", pos: 10 },
    { texte: "cd", pos: 13 },
  ]);
});

test("parseEdits: a ```edit block yields its edits (legacy conversations)", () => {
  const r = parseEdits(
    "```edit\n"
    + '{"edits": [{"file": "a.py", "find": "x", "replace": "y"}]}\n'
    + "```"
  );
  assert.deepEqual(r, [{ file: "a.py", find: "x", replace: "y" }]);
  assert.deepEqual(parseEdits("pas d'edit ici"), []);
});
