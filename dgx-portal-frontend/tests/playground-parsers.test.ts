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
  askedFileNames,
  isExcerptOf,
  splitInlineThink,
  MAX_ASK_QUESTIONS,
  MAX_ASK_OPTIONS,
  RE_QUESTIONS_BODY,
  isQuestionsBlock,
  parseAsk,
  parseEdits,
  escapeRawControlChars,
  firstJsonValue,
  balanceJson,
  repairClosings,
  closedContent,
  continuationBody,
  rejoin,
  splitLines,
  openCodeFence,
  containsQuestions,
  incompleteResponse,
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
  // Content REALLY seen on the platform on 2026-10-05: an empty
  // {"questions": []} envelope closed right away, the intro mixed with JSON
  // debris, a half-written object (« "Qu "options" », no comma), then the
  // real questions set in the text. Before: the parser threw up its hands
  // and the questionnaire came out as raw text.
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
  // The broken object is not a question, and no JSON debris remains in the
  // prose (the model had mixed its intro with its questions).
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
  // The automatic resume mechanism (« reprends au caractère suivant ») saw an
  // unclosed « text » file behind the orphan « ``` » marker of a failed ask
  // block: it relaunched the model, which REWROTE its questionnaire — the
  // two attempts tangled and the questions came out as raw text. This is
  // the cause of « les questions ne marchent plus ».
  const broken = '{"questions": []}```\nBien sûr ! quelques ["Firewall", "SSH"] {"question": "Q1", "options": ["A", "B"]}';
  assert.equal(containsQuestions(broken), true);
  assert.equal(incompleteResponse(broken), false, "un questionnaire ne doit PAS déclencher de reprise");
  // A clean ask block does not count either.
  assert.equal(incompleteResponse('```ask\n{"questions": [{"question": "Q1", "options": ["A"]}]'), false);
});

test("reponseIncomplete: un vrai fichier tronqué reste repris", () => {
  assert.equal(incompleteResponse("```python\ndef carre(n):\n    return n *"), true);
  assert.equal(incompleteResponse("```html\n<!DOCTYPE html><html><body>ok"), true);
  // …and a finished HTML file, even without a closing block fence, is not.
  assert.equal(incompleteResponse("```html\n<!DOCTYPE html><html><body>ok</body></html>"), false);
  assert.equal(containsQuestions("```python\nq = {\"question\": \"x\", \"options\": [\"a\"]}\n```"), false);
});

/* ── estBlocQuestions / RE_CORPS_QUESTIONS ────────────────────────────────── */

test("estBlocQuestions: ask label accepts any body", () => {
  assert.equal(isQuestionsBlock("ask", '{"questions": ['), true);
  assert.equal(isQuestionsBlock("ASK", "n'importe quoi"), true);
});

test("estBlocQuestions: json and empty label only behind the {\"questions\": [ guard", () => {
  assert.equal(isQuestionsBlock("json", '{"questions": ['), true);
  assert.equal(isQuestionsBlock("", '  { "questions" : [\n…'), true);
  assert.equal(isQuestionsBlock("json", '{"autre": 1}'), false);
  assert.equal(isQuestionsBlock("", "du texte"), false);
  // the guard is about the BODY: another language is never a questionnaire
  assert.equal(isQuestionsBlock("python", '{"questions": ['), false);
});

test("RE_CORPS_QUESTIONS: the guard matches the measured shape only", () => {
  assert.equal(RE_QUESTIONS_BODY.test('{"questions": ['), true);
  assert.equal(RE_QUESTIONS_BODY.test('\n  {\n  "questions" : [ '), true);
  assert.equal(RE_QUESTIONS_BODY.test('{"question": "x"}'), false);
});

/* ── recoller / corpsDeSuite — the three resume shapes + HTML rewrite ─────── */

const L = [
  "function alpha() { return 1; }",
  "function beta() { return 22; }",
  "function gamma() { return 333; }",
  "function delta() { return 4444; }",
];

test("corpsDeSuite: reopened fence → the block body", () => {
  const continuation = "```html\n<!DOCTYPE html>\n<html><body>x</body></html>\n";
  assert.equal(continuationBody(continuation), "<!DOCTYPE html>\n<html><body>x</body></html>\n");
});

test("corpsDeSuite: raw continuation that CLOSES the block → what precedes the closer", () => {
  const continuation = "fin du code\nencore une ligne\n```";
  assert.equal(continuationBody(continuation), "fin du code\nencore une ligne");
});

test("corpsDeSuite: no fence at all → the whole message", () => {
  const continuation = "voici la suite brute du fichier";
  assert.equal(continuationBody(continuation), continuation);
});

test("recoller: the model re-emits the cut block → spliced once, no duplication", () => {
  const base = L.join("\n");                       // cut after « delta »
  const continuation = L.slice(1).join("\n") + "\nfunction epsilon() { return 5; }";
  const r = rejoin(base, continuation);
  assert.equal(r, L[0] + "\n" + continuation);
  for (const l of L) assert.equal(r.split(l).length - 1, 1, `duplicated: ${l}`);
});

test("recoller: cut mid-line + resume at the word (« p_del » / « del = … »)", () => {
  const base = "import argparse\np_del";
  const continuation = "del = sub.add_parser('run')\nargs = p.parse_args()";
  assert.equal(rejoin(base, continuation), "import argparse\np_del = sub.add_parser('run')\nargs = p.parse_args()");
});

test("recoller: HTML full rewrite (≥ 50 % of base) REPLACES the file", () => {
  const base = "<html>\n" + "<!-- corps -->\n".repeat(12) + "</html>";
  const continuation = "<!DOCTYPE html>\n<html><body>\n"
    + "<p>ligne de contenu</p>\n".repeat(5)
    + "</body></html>";
  assert.ok(continuation.length >= base.length * 0.5, "test premise: suite carries its weight");
  assert.equal(rejoin(base, continuation), continuation);
});

test("recoller: a small HTML-looking resume does NOT wipe the file", () => {
  const base = "<html>\n" + "<!-- corps -->\n".repeat(120) + "</html>";
  const continuation = "<html>\n<p>trois lignes</p>\n</html>";
  assert.ok(continuation.length < base.length * 0.5, "test premise: suite is small");
  const r = rejoin(base, continuation);
  assert.notEqual(r, continuation);
  assert.ok(r.includes("<!-- corps -->"), "the original body must survive");
});

/* ── contenuCloture ───────────────────────────────────────────────────────── */

test("contenuCloture: odd fence count gets closed, even stays untouched", () => {
  const open = "texte\n```js\nconst a = 1;";
  assert.equal(closedContent(open), open + "\n```");
  const closed = "```js\nconst a = 1;\n```";
  assert.equal(closedContent(closed), closed);
  const withoutFence = "aucune fence ici";
  assert.equal(closedContent(withoutFence), withoutFence);
});

/* ── firstJsonValue / balanceJson / reparerFermetures ─────────────────────── */

test("firstJsonValue: cuts at the end of the first complete value (orphan quote junk)", () => {
  assert.equal(firstJsonValue('{"questions": []}"'), '{"questions": []}');
  assert.equal(firstJsonValue('avant {"a": [1, 2]} après'), '{"a": [1, 2]}');
  assert.equal(firstJsonValue('{"a": [1, 2'), null);   // truncation → balanceJson
});

test("balanceJson: closes what the model left open", () => {
  assert.equal(balanceJson('{"a": "b'), '{"a": "b"}');
  const repaired = balanceJson('{"questions": [{"question": "Q1 ?", "options": ["A", "B"');
  assert.deepEqual(JSON.parse(repaired), {
    questions: [{ question: "Q1 ?", options: ["A", "B"] }],
  });
});

test("reparerFermetures: mismatched closer (« [\"a\", \"b\"} ») is repaired (MiMo 2026-10-01)", () => {
  const repaired = repairClosings('{"questions": [{"question": "Q1", "options": ["a", "b"}]}]}');
  assert.deepEqual(JSON.parse(repaired), {
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
  const lines = splitLines("ab\ncd", 10);
  assert.deepEqual(lines, [
    { text: "ab", pos: 10 },
    { text: "cd", pos: 13 },
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

// ── File names asked for in the request (2026-10-12) ────────────────────────
// « Écris un script Python hello.py » answered by a bare 3-line ```python block:
// the block stayed a snippet, and the follow-up edit had no file to work on.
test("askedFileNames picks the file names a request spells out", () => {
  assert.deepEqual(askedFileNames("Écris un script Python hello.py qui affiche Bonjour."), ["hello.py"]);
  assert.deepEqual(askedFileNames("fais un `docker-compose.yml` et un Dockerfile stp"),
                   ["docker-compose.yml", "Dockerfile"]);
  assert.deepEqual(askedFileNames("modifie roles/web/tasks/main.yml."), ["roles/web/tasks/main.yml"]);
  assert.deepEqual(askedFileNames("utilise ansible.builtin.reboot et os.path"), []);
  assert.deepEqual(askedFileNames(""), []);
});

test("isExcerptOf: a short block that prose places INTO an existing file is an excerpt", () => {
  const toml = "metadata_dir = \"/var/lib/garage/meta\"\n".repeat(30);
  const snippet = 'rpc_secret = "<hex>"\nadmin_token = "<hex>"';
  // « Remplace dans garage.toml : » after the full garage.toml (2026-10-12).
  assert.equal(isExcerptOf("garage.toml", snippet, "Remplace dans garage.toml :\n", toml.length), true);
  assert.equal(isExcerptOf("nginx.conf", "a\nb\nc", "Add this to the `nginx.conf` file:\n", 9), false,
    "« to » is not a where-preposition: « save this to x » announces a whole file");
  assert.equal(isExcerptOf("nginx.conf", "a\nb\nc", "Add this into your nginx.conf:\n", 9), true);
  // Much shorter than the known file, whatever the prose says.
  assert.equal(isExcerptOf("app.py", "x = 1", "Voici app.py :\n", 3000), true);
});

test("isExcerptOf: without an existing file, a named block is the whole file", () => {
  assert.equal(isExcerptOf("docker-compose.yml", "services:\n  a: {}", "Mets ceci dans docker-compose.yml :\n", 0), false);
  // A long block is a version, not an excerpt.
  const long = Array.from({ length: 20 }, (_, k) => `line ${k}`).join("\n");
  assert.equal(isExcerptOf("a.txt", long, "dans a.txt :\n", 10_000), false);
});

test("splitInlineThink: a thought written into the answer goes to the reasoning", () => {
  assert.deepEqual(splitInlineThink("Bonjour !"), { thought: "", answer: "Bonjour !" });
  assert.deepEqual(splitInlineThink("<thi"), { thought: "", answer: "" });
  assert.deepEqual(splitInlineThink("<think>\nThe user is playful"), { thought: "The user is playful", answer: "" });
  assert.deepEqual(splitInlineThink("<think>\nShort.\n</think>\n\nHa, non !"), { thought: "Short.", answer: "Ha, non !" });
  // A « <think> » further in the text is the answer's own words.
  assert.equal(splitInlineThink("Le tag <think> sert à…").answer, "Le tag <think> sert à…");
});
