# Dead code & over-complicated code — inventory (read-only audit, nothing modified)

Method: `vulture` 2.16 + `ruff` F401/F841/F811 + AST cross-reference over every
tracked file (.py/.ts/.tsx/.js/.json/.sh/.yml/.md/.service), `tsc --noUnusedLocals
--noUnusedParameters`, `eslint` (@typescript-eslint/no-unused-vars), a TS export
cross-reference, a CSS-selector cross-reference, and `scripts/check-i18n.py`
completed by a repo-wide string search. Every candidate was cross-checked against
`tests/`, `scripts/`, `docker-compose.yml`, `systemd/`, the Dockerfiles and the CI
workflows before being reported. Dynamic registrations (Flask/Next route
decorators, `patch.object`, design-system class names, tServeur templates) were
verified case by case and excluded.

---

## PASS 1 — dead code (prioritized)

### P1 — real orphans, safe to delete

| # | Location | What | Proof it is orphan |
|---|----------|------|--------------------|
| 1 | `dgx-portal/local_users.py:222-232` | function `passkey_possible(username)` | zero callers repo-wide (name appears only at its def + two *comments* in webauthn_routes.py:365/393); the code that needs the answer uses `gestion["passkey"]` (`webauthn_routes.py:367`, `settings_routes.py:371`, `app.py:1053`). Tests reference only the JSON key string `"passkey_possible"` (test_reglages_utilisateur.py:542), not the function. |
| 2 | `dgx-portal/webauthn_routes.py:45` | import `passkey_possible` | same as #1; never referenced in the file (only in comments). |
| 3 | `dgx-portal/settings_routes.py:18` | import `check_password_hash` | used 0× in the file (only a comment at :450); `local_users.py:12` has its own import; no `from settings_routes import check_password_hash` anywhere. |
| 4 | `dgx-portal/support.py:102-103` | `_LOG_HINT_RE` | compiled regex referenced 0× anywhere — leftover of the removed "logs only when the question is technical" heuristic (`_support_context` no longer filters on the message). |
| 5 | `dgx-portal/support.py:106` | parameter `user_msg` of `_support_context` | assigned, never read (vulture 100 %). Vestigial, not unreachable: `chat_routes.py:170` and tests (test_app.py:1809, 1912) still *pass* it. Deleting it is behaviour-identical but requires updating those 3 call sites. |
| 6 | `dgx-portal-frontend/app/(app)/playground/_components/SettingsPanel.tsx:10` | import `Switch` | tsc `--noUnusedLocals` + eslint `no-unused-vars` both flag it; no JSX use. |
| 7 | `dgx-portal/tests/test_asr_sidecar.py:26` | `from unittest import mock` | never used in the file (the stubs use `types.ModuleType`). |
| 8 | `dgx-portal/tests/test_gardes_media.py:27` | local `db = portal.get_db()` | never read; the next 3 lines call `portal.get_db()` again. |
| 9 | `dgx-portal/app.py:3` | `g` in the `from flask import …` line | unused; it is **not** part of the documented re-export surface (that covers the `from <module> import` blocks, app.py:100-101). |
| 10 | `dgx-portal-frontend/lib/i18n.tsx` | **86 unreachable EN msgids** (list below) | see §i18n. |

### §i18n — 86 unreachable EN entries (exact keys + `lib/i18n.tsx` line)

Proof for each: (a) no literal `t("…")` reference (`scripts/check-i18n.py` agrees:
"415 clés EN jamais vues"), (b) the msgid string appears in **no tracked file at
all** (backend .py included — i.e. no server sentence can reach `tServeur` with
it), (c) re-checked with apostrophe/ellipsis normalization (`'`/`'`/`…`/`...`) —
still nothing. A `t(variable)` cannot rescue a key whose text exists nowhere; the
only residual risk is a string composed at runtime, which was checked by grep for
representative samples. Note `tServeur`'s placeholder-template matcher is why
keys containing `{…}` were treated separately (below).

```
150 Effacer le system prompt · 159 Contexte injecté · 216 Clés API, serveurs MCP, compétences et personnalisation.
358 Demande le lancement d'un modèle. · 361 Disponible depuis l'application, non exposé par l'API.
372 Qui utilise le modèle · 2 dernières min · visible admin uniquement · 379 Crée des clés personnelles pour accéder aux modèles via l'API OpenAI-compatible.
383 Gérer mes clés · 388 Tu connais un modèle que tu veux tester ? Envoie une demande à l'admin. · 390 Faire une demande
399 Ce que l'assistant retient de toi, et que tu contrôles entièrement. · 513 Le fichier s'arrête avant sa fin — le modèle s'est interrompu tout seul. Reprends la suite.
568 Rien à exporter. · 583 Écris ton message… (Entrée pour envoyer, Maj+Entrée = saut de ligne) · 591 Activer la réflexion du modèle (plus lent, plus coûteux)
628 Code Python · 630 Écris une fonction Python qui vérifie si un nombre est premier. · 634 Explique la mémoire unifiée du DGX Spark en termes simples.
637 Trouve la cause d'une erreur dans un extrait de logs · 675 Résume ce texte en 3 points :  · 712 Le serveur n'a pas répondu. Réessaie dans un instant.
718 Discuter avec le modèle actif · 719 Accès rapide · 838 État vivant du moteur et de la machine — rien à actionner ici. · 840 Le moteur ne publie pas l'état de ses sessions.
852 Modèles vLLM · 863 OCR & Vidéo · 864 Catalogue OCR · 888 Débit décodé · 907 Entrée traitée · 912 Consommation par utilisateur
913 Utilisation OCR par utilisateur · 914 Ne passe pas par une clé API — jamais visible dans la conso LiteLLM ci-dessus. · 916 Extractions
917 Utilisation vidéo par utilisateur · 918 Générations · 920 Chat & complétions — API OpenAI-compatible · 921 Extraction de texte et de tableaux depuis images et PDF
922 Génération de vidéos courtes (texte ou image → vidéo) · 923 Clonage de voix zéro-shot à partir d'un court échantillon
942 Ouvrir en document · 943 Redimensionner le document · 1054 Budget / jour · 1086 Supprimer cette conversation · 1087 Exporter en Markdown
1101 Sur quoi veux-tu travailler ? · 1130 détecté(e) · 1171 WAV ou MP3 — 15 Mo max, plus de 5 secondes de voix claire. · 1180 Catalogue voix
1193 Nom (ex: chatterbox-turbo) · 1195 Utilisation voix par utilisateur · 1214 Trop court : le modèle exige plus de 5 secondes de voix.
1221 Échec de la génération — l'échantillon doit contenir plus de 5 secondes de voix. · 1255 Moins → Plus · 1258 Maximum 20 messages par minute.
1283 Géré à l'extérieur · 1294 Créer un utilisateur · 1298 hérité · 1309 Nouveau mot de passe (8 caractères min.) : · 1310 Supprimer cet utilisateur ?
1363 Mot de passe : 8 caractères minimum. · 1368 Le quota doit être un entier positif.
1382 Double authentification par clé de sécurité (passkey, YubiKey, 1Password) — pas de TOTP.
1420 Génération d'images (texte → image) · 1421 Génération musicale (texte → chanson)
1461 Compte LDAP/SSO : le mot de passe est géré dans l'annuaire, pas ici.
1517 L'entrée sera retirée du catalogue et du routage LiteLLM (ça n'arrête pas un modèle en cours).
1664 Compte SSO : aucun mot de passe n'est géré par le portail, la vérification est impossible.
1666 Compte géré par l'annuaire (LDAP/SSO) : le mot de passe se change là-bas.
1673 La clé n'a pas pu être créée : le service de clés (LiteLLM) n'a pas répondu. Réessaie dans un instant.
1675 La clé n'a PAS pu être révoquée : LiteLLM n'a pas répondu. Elle est encore valide, réessaie.
1677 Le nom n'a PAS pu être changé : LiteLLM n'a pas répondu. La clé est intacte, réessaie.
1679 Le nom doit contenir au moins un caractère (lettres, chiffres, - ou _).
1706 Budget : nombre strictement positif attendu (tokens). Pour couper l'accès d'un compte, utilise le blocage du compte — réversible et tracé ; un budget de 0 le plafonne réellement à zéro token.
1717 Durée de la subvention : nombre de jours entre 1 et 365 (vide = permanent).
1730 Dernier administrateur local : nomme un autre administrateur avant de supprimer celui-ci.
1732 Tu es le dernier administrateur local : nomme un autre administrateur avant de supprimer ton compte.
1734 Ce compte est local : utilise la suppression, qui retire aussi l'accès.
1736 Confirmation requise : cette suppression emporte les accès ET les données du compte.
1738 Confirmation requise : cette opération efface définitivement les données du compte.
1740 Compte créé, mais son quota n'a PAS pu être appliqué sur LiteLLM : le compte est sans plafond tant que le budget n'est pas redéfini.
1762 Trop de générations d'images en cours pour ce compte — attends la fin des précédentes.
1764 La génération d'image a échoué (sidecar indisponible ou surchargé). Dis-le tel quel à l'utilisateur.
1783 Dernier administrateur local : nomme un autre administrateur avant de bloquer celui-ci.
1785 Mode maintenance en cours — l'API est temporairement indisponible, réessaie plus tard.
1787 SMTP non configuré (renseigne SMTP_HOST / SMTP_USER / SMTP_PASSWORD / ADMIN_EMAIL).
```

**Placeholder keys (`tServeur` templates) — 6 with no producer anywhere:**
`906 dont {n} depuis ce lancement`, `908 prefill {n} tok/s`, `1022 Astuce : appelle
« {model} » …`, `1517-ish Supprimer {name} du catalogue ?`, `1753 Enregistrement
trop long ({dur}s, maximum {max}s).`, `Génération en cours… {d}/{n}` (around 905).
They are only reachable if a server sentence matches the pattern; no producer
exists in any .py or .tsx (the UI builds `prefill`/`dont … depuis ce lancement`
differently today: `app/(app)/page.tsx:491` uses `${tps_prefill} tok/s`).
The 3 *reachable* placeholder keys verified as live: `Trop de tentatives.
Réessaie dans {wait} min.` (webauthn_routes.py:196), `Demande envoyée pour
« {name} » !` (app.py:1494), `Action inconnue : {action}` (conversation_routes.py:197,
support.py:348).

### Verified NOT dead (do not touch — false positives of the tools)

- **`dgx-portal/app.py:100-335` re-export surface** (~128 "unused imports" from
  ruff/vulture): explicitly documented (`# ── Re-export surface, do NOT "clean
  up" ──`, app.py:100) for `from app import <name>` consumers and
  `scripts/create-demo-account.py` (`import app as portal`). The same pattern
  continues at app.py:333-348 (`asr_is_up`, `websearch_tools.*`) — treat as part
  of the surface.
- `app.py:79 _security_headers` (`@app.after_request`), `vllm-runner/runner.py:144
  _check_auth` (`@app.before_request`), `asr/server.py:110` + `voice-qwen/server.py:68
  _load` (`@app.on_event("startup")`), and every Flask/route handler vulture lists
  (decorator registration = dynamic).
- `dgx-portal/support.py:27 runner_logs` import: the only reference is
  `tests/test_app.py:1907 patch.object(assistance, "runner_logs", …)` — tests are
  callers; deleting it breaks that test.
- `litellm_inflight.py:132-147` `response_obj`/`start_time`/`end_time`: parameters
  of the `CustomLogger` hook signature (the sync hooks are the only ones LiteLLM
  calls — CLAUDE.md locks this in).
- `app/internal/[...path]/route.ts` exported `GET/PUT/OPTIONS`: Next.js route
  handlers, dispatched by the framework.
- CSS class `.astryx-chat-composer` (`app/globals.css:193`): emitted by
  `@astryxdesign/core` (ChatComposer) — selector targets a library class.
- `monitoring/backup.py:34 PORTAL_VOLUME`, `monitoring/monitor.py:66 PORTAL_DB`:
  adjacent comments say "Kept for documentation" / "Path UNUSABLE by the unit".
- `lib/notices.ts` `case "model_error"`: documented legacy alias (CLAUDE.md).

No orphan test helpers, no orphan lib modules, no leftover tracked files
(git tree clean; `assets/*.png` are README screenshots; `dgx-portal/workflows/*.json`
are ComfyUI templates).

---

## PASS 2 — over-complicated code (prioritized)

| # | Location | Problem | Proposed simplification | Risk |
|---|----------|---------|-------------------------|------|
| 1 | `dgx-portal/chat_routes.py:128-510` (`support_chat`, 382 ln) vs `:1053-1541` (`playground_chat`, 488 ln) | two chat pipelines with twin SSE choreography: 15 byte-identical non-trivial lines (the `use_tools=False` retry, the `_sse_notice` error paths, the `data: [DONE]` emission, the `untrusted_seen` guard shape) and two copies of the "open the POST in a thread + `: ping` while waiting" opener (`_run_turn` :230-247) | extract module-level `_ouvrir_flux_avec_pings()` + `_erreur_tour(status)` helpers used by both; leave each pipeline's threading model alone | low |
| 2 | `dgx-portal/image_tools.py:123-218` `_exec_image_tool` vs `dgx-portal/video_tools.py:148-238` `_exec_video_tool` | the job-wait loop is verbatim in both (18 identical lines: `sqlite3.connect(DB_PATH, timeout=5)`, `status != 'running'`, `: battement` heartbeat every `_HEARTBEAT_EVERY_S`, `media_job_done`) | extract `_attendre_job(table, prompt_id, username, timeout_s)` generator yielding the heartbeats, called by both tools | low |
| 3 | `app/(app)/playground/page.tsx:1170` `PlaygroundPage` (3026 ln), `_components/SettingsDialog.tsx:168` (1012), `admin/_components/UsersSection.tsx:130` (830), `support/page.tsx:157` (743), `page.tsx:222 HomePage` (594) | single components far past the point where their JSX can be reviewed | extract cohesive blocks as child components taking plain props (Settings tabs, user table rows, home cards) | medium (re-render identity / memo semantics must be preserved) |
| 4 | `dgx-portal/chat_routes.py:1341-1439` (nesting to depth **7** inside `playground_chat`'s tool phase), `ocr_routes.py:135-138` (depth 7), `websearch_tools.py:418-429`, `stats.py:510-514` | deep conditional pyramids | pull each `if` level into a `_xxx_etape()` function or invert with early `continue`/`return`; structure moves, values don't | low |
| 5 | `dgx-portal/chat_routes.py:1053` `playground_chat` (488 ln) and `:1221` nested `gen` (317 ln) | one function holds framing, notices, tool phase, title/summary, usage, inflight bookkeeping | split at the existing comment banners (`── outils ──`, `── usage ──`) into nested helpers returning what `gen` yields | medium |
| 6 | `dgx-portal/db.py:164` `init_db` (633 ln) | schema + migration tail in one function | optional: move the post-`executescript` migration steps into `_migrations(db)` called at the end, order preserved (identical SQL, identical order) | low |
| 7 | `dgx-portal/vllm_health.py:222` `_vllm_health_uncached` (140 ln) | three engine dialects (llamacpp `/metrics`, vLLM, TabbyAPI/SpendLogs) in one body | one `_compteurs_<engine>()` per branch, dict merged at the end | low |
| 8 | `dgx-portal/stats.py:700` `ranking_full` (147 ln) + `:421 _active_users` (139 ln) | SQL and row shaping interleaved | split the shaping (share/delta/trend) into `_forme_classement(rows)` — pure, byte-identical | low |
| 9 | `vllm-runner/runner.py:889` `_start_process` (118 ln) | log-capture wiring + spawn + failure bookkeeping in one block | extract the `_logs` capture setup into `_capturer_logs(proc)` | low |
| 10 | `dgx-portal/settings_routes.py:164` `mcp_servers_route` (111 ln), `app.py:1177` `keys` (119 ln), `support.py:292 _exec_support_tool` (135 ln), `websearch_tools.py:310 _phase_outils` (137 ln) | borderline length; mixed validation + IO | split the validation prelude from the IO; no behaviour change | low |

No `if False` / impossible-state branches found anywhere (grep + AST), and only
one mild boolean-parameter pair (`chat_routes._chat(with_tools, stream)`) — the
"boolean parameter soup" bucket is effectively clean.
