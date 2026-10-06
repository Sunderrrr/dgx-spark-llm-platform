# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, a **breaking change in the portal's HTTP contract, the
`.env` variable set, or the deployment layout bumps the minor number** (`0.1` →
`0.2`), not the major one: the platform is in production and self-hosted, so the
major number stays at `0` until the contract is declared frozen.

How a release is cut: the notes below are written **before** the tag, then
`scripts/release.sh X.Y.Z` bumps `dgx-portal-frontend/package.json`, commits,
creates the annotated tag `vX.Y.Z` and pushes it. `.github/workflows/release.yml`
publishes the GitHub Release from the section of this file — so the release body
and this file can never disagree. `scripts/release.sh --check` (also run by CI)
fails if `package.json` and the newest version here have drifted apart.

## [Unreleased]

### Changed

- **The home page shows the capability view only** (« enleve ça… et mets juste
  le modèle si il est actif »). The « Modèles disponibles maintenant » block is
  gone entirely; each capability card now carries THE MODEL NAME when that
  capability is up — `Chat · qwen38-27b · En ligne`, `Musique · à la demande`.
  A card naming a stopped service read as « it works »; name above, state below
  tells the whole story in one glance. A minimal error card keeps a failed
  load visible (it used to be inside the removed block).
- **Every code comment is in English** (« passe tous les commentaires en
  anglais »): 166 comments across 59 files, ~401 lines — Python docstrings, TS
  doc comments, shell comments, from `dgx-portal/` to `scripts/`. Comments
  only: the diff was verified to move no literal string, msgid, identifier or
  path (the UI stays French-first by design, English lives in `lib/i18n.tsx`).
  The French that remains is quoted VERBATIM text produced by the system or
  said by a user (UI labels, server messages, model outputs) — 157 lines, all
  inside quotation marks.

## [0.2.0] - 2026-10-06

A full penetration test of the platform (three parallel audits: auth and
access control, injection and unsafe input handling, SSRF / exposure /
secrets), with the fixes. No critical finding; two HIGH, both on the media
path, both closed.

### Security

- **HIGH — ComfyUI was reachable by every container on the docker bridge,
  without any authentication.** The relay installed earlier today exposed the
  engine (which executes workflow graphs as the runner user and holds the HF
  token in its home) to `litellm` — internet-exposed — postgres and the agents.
  The firewall now accepts 8188 from the PORTAL's address only, verified live:
  `litellm → 8188` is refused. The relay unit is the repository one again
  (`BindsTo`/`PartOf` the on-demand sidecar, not enabled at boot — the
  installed copy dragged ComfyUI (33 GB) in at every boot).
- **HIGH — the relay unit had drifted** from the repository copy and was
  `enabled`: at every boot it started the relay AND ComfyUI, keeping the above
  surface up 24/7. Reinstalled and disabled.
- `/internal/authcheck` (Traefik's forwardAuth) now only answers to the proxy
  network and loopback: the 200/503 difference was a KEY-VALIDITY oracle for
  any peer sharing a network with the portal (the OCR sidecar runs third-party
  model code on one).
- The directory is now a DEMOTION CEILING for the admin role: removing an
  LDAP/SSO account from `cn=adm_cronos` takes the rights away immediately
  instead of leaving the session admin for up to 12 h. It can only take away,
  never grant — a directory outage must not invent an admin.
- `_client_ip` takes the LAST `X-Forwarded-For` element (the one appended by
  our trusted hop), not the first, which was client-controlled: a caller could
  rotate a fake prefix to obtain a fresh lockout key on every attempt.
- The HTML preview now sends `default-src 'none'`: a generated page could make
  the viewer's browser fetch arbitrary URLs (blind browser-side SSRF).
- The math renderer no longer crashes on crafted markers: a literal
  `\uE000<n>\uE001` in model output (prompt-injectable through the web/OCR
  tools) threw twice and broke the whole conversation — persisted, so it
  re-crashed on every reload. Marker indices are bounds-checked and the two
  private-use characters are stripped from input.
- The Markdown export escapes HTML: `</details><img onerror=…>` in a reasoning
  block became live markup when the file was opened in a permissive viewer.
- Account purge no longer resets OTHER accounts' brute-force counters: `_` is a
  LIKE wildcard and `john_` used to delete the rows of `johnX` (escaped now).
- The video download no longer splices a ComfyUI-controlled filename into
  `Content-Disposition` (quote/CRLF injection).
- `npm audit`: 0 vulnerability (the `sharp` and `source-map-js` advisories are
  gone from the shipped frontend).

### Known, for the operator

Two secrets need ROTATION (the values are not repeated here): the Authentik
→ LiteLLM OAuth client secret appears verbatim in a public commit message, and
the SMTP mailbox identity sits in tracked files and the git history. Also open,
at the infrastructure level: the `/metrics` direct-to-origin bypass (an
IP allowlist on the Traefik routers), the IPv6 counterpart of the
DOCKER-USER allowlists, and `litellm` still running as root on the shared
bridge. None is reachable from the internet today.

## [0.1.18] - 2026-10-06

The playground generates videos, reads documents, and makes 4K images — with
one generation frame per image, like the Image page.

### Added

- **The playground can now generate videos and read documents** (« le playground
  peut générer une vidéo ou appeler de l'OCR si on lui demande ? »). Two new
  tools in the image tool's exact shape: `generer_video` (ComfyUI, MiniMax H3 —
  the same entry point as the Video page) and `lire_document` (Unlimited-OCR, on
  the attached file — the same extraction as the OCR page). Both arm on an
  EXPLICIT request, start their sidecar at first use behind a memory guard, and
  report `video_service_off` / `document_service_off` with the free memory when
  the service cannot start. 28 new tests.

### Fixed

- **`generer_image` answered « je ne peux pas générer » on the operator's exact
  request** (« généré en 4K 4 images pour halloween »). Two gaps in the
  detection: past participles (« généré », « créée ») matched nothing, and
  « 4 images » is not « une image » (a numeral is not an article) — the tool was
  never armed. Both forms accepted. The tool also gained `nombre` (1–4) and
  `sortie_largeur`/`sortie_hauteur`: the Image page has always upscaled to
  **3840 px** (the 4K), the tool clamped at 1536 and generated ONE image.
  Measured after: « Voici tes 4 images Halloween en 4K ! » with 4 images shown.
- **ComfyUI was unreachable from the portal.** The engine deliberately listens
  on `127.0.0.1:8188` (no auth, not network-reachable) while the container probes
  `172.19.0.1:8188` — and the `comfyui-relay.service` its own unit describes did
  not exist. Created: socat bridging the docker gateway to the engine. Before
  it, every video request died with « le service a démarré mais ne répond pas
  encore ».

### Changed

- **One generation placeholder per image**, not one: four grey frames while four
  images cook, exactly like the Image page. A single square read as « nothing is
  happening ». Same for video (16/9 frame, FilmIcon).

## [0.1.17] - 2026-10-06

Four sessions of 262 144, an honest memory budget, and the session count
displayed for vLLM.

### Changed

- **`qwen38-27b` runs 4 sessions of 262 144, with an honest memory budget**
  (« je ne vois pas le nombre de session dispo et il me reste 9 Gio de RAM, je
  veux 4 sessions de 256k »). `--max-num-seqs 4` + `--max-model-len 262144`
  were set, and `--gpu-memory-utilization` dropped **0.85 → 0.4**: vLLM
  PRE-ALLOCATES that budget, so 0.85 held ~103 GiB for a real need of ~40 —
  that is where the 9 GiB went. Measured after the change: **60 GiB free**,
  sessions 0 / 4, context 262 144.

### Fixed

- **The session count is displayed for vLLM too.** It was fed by llama.cpp's
  `/slots`, which vLLM does not publish: the line silently disappeared exactly
  when the reader wanted to know how many sessions were free. The engine
  counters (`vllm:num_requests_running` + the catalog's `--max-num-seqs`) now
  feed it — « 0 / 4 » on the home page, and the detailed line no longer hides
  when the engine is idle (a blank panel looked broken).

## [0.1.16] - 2026-10-06

A new model target — the one chosen by the operator — with speculative
decoding and vision, on an updated vLLM.

### Added

- **`qwen38-27b` — `orcarouter/Qwen3.8-27B-Uncensored-NVFP4`, served with the
  DFlash 2 drafter and VISION** (« pour le paramètre je veux qu'il prenne les
  images »). The target is the Uncensored 27B in NVFP4 (CutlassNvFp4 GEMM
  verified in the engine log), the draft is `z-lab/Qwen3.8-27B-DFlash2`
  (block-diffusion speculative decoding — its 48 CUDA graphs are captured at
  load), and `--limit-mm-per-prompt {"image":8,"video":2}` declares the vision
  the platform already knows how to use. Verified live: text answers in 0.8 s,
  and a real image is read correctly (« Quelle est la couleur dominante ? » →
  « Rouge »). The previously downloaded `Qwen/Qwen3.8-27B` (55.6 GB) was
  **deleted** — the wrong target.

### Changed

- **vLLM 0.24.0 → 0.31.0** (the default binary). DFlash support is in it
  (`@register_speculator("dflash")`), and the GB10 is properly recognised
  (sm_121).

### Fixed

- **FlashInfer's version gate no longer kills vLLM.** Its kernels live in a
  companion package (`flashinfer-cubin`) whose version trails the Python one
  (0.6.13 vs 0.7.0.post1 — there is no 0.7 cubin on PyPI): the check runs at
  IMPORT and aborted the engine outright. Aligning the packages is a trap —
  `flashinfer-python==0.6.13` drags **torch back to 2.10** and breaks vLLM
  entirely (measured). The runner now injects
  `FLASHINFER_DISABLE_VERSION_CHECK=1` into the model's environment (its spawn
  uses a minimal env, so a systemd variable never reached the process).

## [0.1.15] - 2026-10-06

The Skills tab shows every skill there is — the thirteen defaults included.

### Fixed

- **Settings → Skills now lists EVERY skill, defaults included** (« je veux
  que dans les settings quand l'onglet Compétence on voit les skills que il y a
  et meme par default »). The tab only showed the *server* skills (the
  assistant's `use_skill`) — the playground's, and above all the thirteen
  built-ins, were invisible: the empty state even claimed « Aucune compétence
  pour l'instant ». A « Playground skills (the « / » menu) » group now lists
  them all with their `/alias`, name and description — built-ins and the
  user's own — above the assistant's own group, which is now labelled as such.

## [0.1.14] - 2026-10-06

Five default skills — the operator's anti-AI SKILL.md verbatim, and four more
in the spirit of the public Claude skills.

### Added

- **Five default skills**, in the spirit of the public Claude skills and the
  one the operator supplied:
  - **anti-ia-detection** (`/anti-ia`) — the operator's SKILL.md, verbatim: spot
    and fix the tells of AI-generated French writing (25 patterns in 5 groups —
    content, vocabulary, style, chatbot artefacts — plus the part that matters
    as much as removing the tics: *adding a voice*). Built on
    Wikipedia:Signs of AI writing, adapted to French.
  - **email** (`/email`) — a professional email: sharp subject, useful body,
    right tone; two lengths, no hollow formulas.
  - **web-design** (`/webdesign`) — a complete, accessible, responsive page in
    a single HTML file (the playground already writes pages, it now designs
    them).
  - **slides** (`/slides`) — a deck where every slide asserts something, with
    the story first.
  - **meeting** (`/meeting`) — raw notes into decisions, open points, and an
    action table (what, who, when). Blanks become « à préciser », never a guess.

### Changed

- **The system prompt cap is 16 000 characters, not 4 000** — a skill's whole
  instructions live there (the anti-AI one alone is ~8 500), and cutting them
  in half silently produced half a skill. ~4 000 tokens against a 262k window;
  the bound stays as a guard against a runaway persona, not as a size budget.

## [0.1.13] - 2026-10-06

Three more essentials — two of them work without any key and fill real holes.

### Added

- **Three more essentials: Hugging Face, Exa, Discord** (« tu n'en a pas
  d'autre que tu pense essentiel ? »). Two of them work **without any key** and
  fill real holes:
  - **huggingface** (`huggingface.co/mcp`) — model and dataset search on the
    Hub: the very source of the platform's model catalog. « Which model for X »
    is now answered from the Hub, live.
  - **exa** (`mcp.exa.ai/mcp`) — web search + page reading: the one thing the
    assistant could not do at all, look *outside* the documentation (new
    releases, known incidents, announcements). Verified with real calls on
    both.
  - **discord** (waiting for its key, like the previous eight) — the platform
    already notifies a Discord channel for model and budget requests.

### Changed

- **`MAX_MCP_SERVERS` 10 → 16** — disabled servers cost the model nothing
  (their tools are never discovered), the cap only bounds the registry, and
  16 leaves room for an account's own additions on top of the pack.

## [0.1.12] - 2026-10-05

The MCP pack grows to the usual suspects — registered disabled, each waiting
for its own key before coming alive.

### Added

- **The usual MCP pack, disabled until its key arrives** (« rajoute plus de
  chose genre github gmail et plus… juste ne les active pas tant que les user
  n'ont pas set les api key »). Eight servers join the two that work straight
  away — **github**, **sentry**, **cloudflare**, **slack**, **notion**,
  **linear**, **figma**, **stripe** — all verified live (they answer 401, i.e.
  they exist and wait for a token). Each is registered **disabled**, with the
  token format it expects spelled out; **writing the key in Settings → MCP is
  what switches the server on**, so an incomplete configuration never becomes
  active by accident. Three tests lock that rule. Gmail has no public remote
  MCP endpoint (checked: no such host, aggregators redirect to their own auth)
  — see the README for the two ways around it.

### Changed

- **`scripts/mcp-essentiels.sh`** is now the single source of truth for the
  pack (enabled-while-keyless / waiting-for-a-key families), and the
  per-account cap stays at 10 servers.

## [0.1.11] - 2026-10-05

The Support assistant now answers integration questions from CURRENT
documentation: two essential MCP servers, registered per account.

### Added

- **Essential MCP servers, registered per account** (« conf des mcp de base
  que tu pense essentiel »). Two, chosen for what the Support assistant
  actually lacks: **context7** (current library/framework docs — so « how do I
  integrate X » is answered from documentation, not training data) and
  **deepwiki** (Q&A over GitHub repositories). Public endpoints, no key,
  validated with the platform's own SSRF check. `scripts/mcp-essentiels.sh`
  registers them for any account, so a rebuilt database or a new user does not
  quietly lose them. Verified live: the assistant calls them, and the
  third-party-output guardrail still applies to what they return.

## [0.1.10] - 2026-10-05

The Support starts clean on every visit, and its whole interface is now covered
by an automated check (19 controls against the real page).

### Changed

- **Support — it starts clear, every time** (« je veux arriver sur support et
  que la conv soit clear »). The page used to reload the last thread behind
  the reader's back; now arriving = a blank conversation, always. The previous
  thread is still kept server-side and offered behind a discreet « Reprendre »
  card — losing a whole conversation to an accidental reload would be the
  opposite defect — but it never imposes itself.

## [0.1.9] - 2026-10-05

Completes 0.1.8's log reading: LiteLLM's logs are covered too, so `read_logs`
spans every service the platform runs on the GPU side plus its gateway.

### Added

- **Support — LiteLLM's logs join `read_logs`** (« toutes les logs de tous les
  services »). The gateway now writes to the shared volume
  (`/run/cronos/litellm.log`, rotated once past 20 MB) instead of stdout only:
  the portal mounts it read-only and reads it **directly** — no runner
  involved, and above all no runner restart, which would kill the served
  model. The wrapper keeps the graceful shutdown (`sh` is PID 1 and would
  swallow SIGTERM, so the signal is trapped and forwarded). Verified live: the
  tool returns the real gateway traffic, and the assistant summarises it.
  Logs covered: model, litellm, ocr, video, voice, asr, image, music.

## [0.1.8] - 2026-10-05

The Support reads the services' logs the right way: pulled on demand through a
tool, never stuffed into the prompt.

### Added

- **Support — it reads the services' logs on demand** (« qu'il puisse appeler
  une sorte de MCP ou autre, pas que dans le system prompt »). A `read_logs`
  tool (admin-only, read-only) pulls the tail of whatever service matters —
  model runner, ocr, video, voice, asr, image, music — through the runner,
  which is the only process holding the scoped docker/journalctl rights. The
  blind 12-line log tail that used to be injected on every troubleshooting
  question is **gone from the system prompt**: the model now reads what it
  needs, once, and answers from the exact cause written there. 3 new tests,
  including one that pins the logs' ABSENCE from the context.

## [0.1.7] - 2026-10-05

The Support closes the gap between « it diagnoses » and « it repairs »: it now
sees every service's real state, and can relaunch a stopped one.

### Added

- **Support — it sees the platform's services, and can relaunch one** (« je
  suis chaud que il puisse regarder si tout fonctionne et que si un service est
  stop il puisse relancer »). Every service (ocr, video, voice, asr, image,
  music) is now listed in the assistant's context with its real state —
  `running` when it actually answers, `starting` while it loads, `stopped`,
  `failed` — read from the runner, never guessed. A new **`manage_service`**
  tool starts, restarts or stops one: admin-only, guarded by the same
  in-chat confirmation as launching a model, and it answers with the service's
  NEW state so the assistant can report « relancé, il démarre » instead of
  hoping. The unified-memory guard runs before any start (a sidecar that
  overflows takes the chat model with it). 8 new tests.

## [0.1.6] - 2026-10-05

Four additions that complete the Support's job — diagnosing — and bring it to
parity with the playground: attachments, export, dictation, and a thinking that
outlives the page.

### Added

- **Support — attachments.** A log, a config, a snippet to review: the composer
  takes text files (same 96 Ko ceiling and same named-code-block transport as
  the playground) and the drawer shows what goes out. Diagnosis from a pasted
  error was already possible; diagnosis from the *file* now is too. Text only —
  the running model has no guaranteed vision.
- **Support — export the thread.** One click keeps the whole exchange as .md:
  questions, answers, **and the thinking that led to them** (folded block, with
  its duration). What someone reporting a problem sends along with the report.
- **Support — dictation.** The composer's microphone, same Whisper sidecar as
  the playground: say what is wrong instead of typing it. Sending a message
  ends the dictation and goes out with what was transcribed.
- **Support — the thinking survives a reload.** It was streamed and displayed,
  then lost with the page: the thread now keeps it, so a reloaded conversation
  still opens the diagnosis instead of only its conclusion.

### Changed

- **Export shared** — `convAsMarkdown` & friends move to `lib/export.ts`, used
  by both chats; the reasoning is exported there too.

## [0.1.5] - 2026-10-05

Follow-up to 0.1.4, seen as soon as the new Support rendering met a real
reloaded thread: two defects in the very feature 0.1.4 introduced.

### Fixed

- **Support — the name above an answer is the model that WROTE it** (« dès que
  je refresh j'ai cette conv : qwen38-flash-next … »). The line introduced in
  0.1.4 read the *currently running* model, so a reloaded thread attributed old
  answers to whatever model runs today — and the welcome message, which is the
  UI's greeting and not an answer at all, carried a model name too. Each
  message now travels with its own model (kept in the server-side thread), and
  the name only appears when it is actually known.
- **Support — a reloaded thread opens at its end.** It greeted the reader with
  the scroll button already showing (« Scroll to bottom », untranslated at
  that). The page now scrolls to the last word on load, and uses the same
  « Descendre » stick-to-bottom mechanism as the playground.

## [0.1.4] - 2026-10-05

The Support assistant now reads exactly like the playground — same visual
registers, and its **thinking is visible** instead of being generated and thrown
away.

### Changed

- **Support — the playground's visual registers.** The question keeps its
  bubble (65 % of the column), the answer is plain text with the model named
  above it — the same three registers as the playground and the reference
  screenshots. One codebase, one look.
- **Support — the thinking is shown** (« appliquer … le thinking sur le
  support si tu pense que c'est utile » — it is). The Support already runs an
  adaptive reasoning pass, so the tokens were **generated and billed** and the
  relay dropped them: nothing let the operator see what the assistant weighed
  before answering. The reasoning is now streamed as `reasoning_content` (the
  playground's contract) and rendered in the same collapsible block — open
  while it runs — clock ticking, « Thinking… for 12 s » — then folded on the
  first answer word with its measured duration (« thought for 20 s », verified
  live). The handover is explicit: the moment the thinking starts, the block
  replaces the waiting spinner, so the reasoning → writing boundary is visible
  there too. The stray `think` tags some models leak
  *into their answer text* stay hidden: those are text, not a structured
  reasoning. `ReasoningBlock` moved to the shared components, and two backend
  tests lock both halves of that contract.

## [0.1.3] - 2026-10-05

Two things shipped together: the platform stack moves forward, and a reported
playground defect — « les questions ne marchent plus » — is fixed at its two
roots and re-verified on the whole playground.

### Fixed

- **Playground — the clarifying questionnaire works again** (« les questions ne
  marchent plus »). Two defects chained, both found on the very output that was
  reported. First, the model sometimes fails its `ask` block — an empty
  `{"questions": []}` envelope closed at once, then the real questions
  scattered through the text as separate `{"question": …}` objects, with JSON
  debris around them — and `parseAsk` gave up on the first empty block, so the
  questions came out as raw text with nothing to click. It now harvests every
  question-shaped object wherever it lands, each one parsed on its own through
  the repair chain: the reported message yields its 5 questions (the half-written
  one is skipped, a real file that happens to contain question objects stays a
  file). Second, the auto-resume (« reprends au caractère suivant ») mistook the
  stray ` ``` ` marker of that same broken block for an unfinished *file* and
  relaunched the model — which **rewrote** its questionnaire, the two attempts
  interleaving into the garbled text that was reported. A message carrying
  questions is never resumed now. Locked by 5 new parser tests, the reported
  content verbatim included.

### Changed

- **Web stack** — LiteLLM `1.102.1 → 1.104.0`, PostgreSQL `15.18 → 15.19`
  (both re-pinned by digest in `docker-compose.yml`; Postgres stays on major 15
  on purpose — the `postgres_data` volume holds the cluster, a major bump is a
  dump/restore). Verified on the running platform, not just the startup:
  the `cronos_inflight` callback is still **dispatched** on 1.104.0 — the
  synchronous hooks remain the ones LiteLLM calls (`log_pre_api_call` at
  `litellm_logging.py:1446`, `log_success_event` at `:3144`, and
  `async_log_pre_api_call` still has **zero** call site), a real request opens
  then closes its `en_vol` row in `runtime/inflight.db`, its SpendLogs row lands
  (152 273 → 152 274 rows, schema only gained columns), and the whole chain
  answers through the UI (5 tokens, TTFT 1.57 s). The compose comment now makes
  that callback check part of the update procedure: a silently unwired callback
  is invisible — table created, table empty, no error.

## [0.1.2] - 2026-10-04

Same-day follow-up to `0.1.1`, which had been tagged and pushed before these
fixes: re-releasing a published tag would rewrite history, so the corrections
ship as `0.1.2`. What `0.1.1` announced did not actually work — and each item
below is measured against the live playground, not read from the code.

### Fixed

- **Playground — the chat scrolls again** (« quand un fichier est ouvert sur le
  côté la scrollbar ne marche pas »). Root cause: `ChatLayout` was given a
  `scrollRef` that was never attached to any element, and that alone turns off
  its self-scrolling (`isSelfScrolling = !scrollRef`) and renders its root with
  `overflow: visible` — the chat column then had **no scroll container at all**
  (measured: `scrollHeight` 1278 for `clientHeight` 817, `scrollTop` frozen at
  0, the wheel changed nothing). The root is now the scroll container, and the
  stick-to-bottom hook is its callback ref: the wheel, the scrollbar, the
  auto-follow and the « Descendre » button all work, and scrolling up is no
  longer yanked back down.
- **Playground — the file panel is a single scroll container again.** The
  `CodeBlock` shrank to the panel height (both of its levels are `flex: 0 1
  auto` items, `min-height` resolving to 0 behind a non-visible `overflow`) and
  its *own* inner container became the only scroller (measured: panel body
  792/792, inner code box 5 716/720) — so the auto-follow and « Descendre »,
  both wired to the panel body, did nothing. `flexShrink: 0` on the block
  restores the body as the one place that scrolls (measured after: body
  3 408/792, inner box 3 336/3 336).
- **Playground — the reasoning box is visible.** `0.1.1` styled it with
  `--color-background-subtle` and `--radius-md`: **neither token exists** in
  Astryx 0.1.8, so the declarations were dropped and the « panel » had no fill
  and no radius (measured from `getComputedStyle`: transparent background, 0 px
  radius). Real tokens now (`--color-background-muted`, `--radius-container`),
  and every token used by the app is validated against the theme — two silent
  `radius` losses on the OCR page were found the same way.
- **Playground — the settings panel remembers your choices** (« quand je
  choisis le raisonnement à toujours ou à jamais, il n'enregistre pas, je dois
  le sélectionner à chaque fois »). Nothing was persisted: every page load — and
  every round trip through another page — reset the panel to its defaults, so
  the reasoning mode (Auto / Toujours / Jamais) was re-picked each session
  (measured: `Always` selected, reload, back to `Auto`). The panel now restores
  from the browser on mount and saves on every change, per browser like the
  pinned conversations and the snippets. The system prompt a *skill* injects is
  deliberately not saved as your persona — it belongs to the skill.
- **Playground — the side panel stays while a text is written** (« quand un
  texte est lancé je veux garder la barre latérale, là il la fait
  disparaître »). Writing a *document* showed only a card in the chat: the panel
  opened on the explicit click alone, so through the whole write there was
  either no panel or the previous file's. A document write now opens the panel
  on its first word, exactly like a code file, and the finished document stays
  there. Closing it by hand is still respected (nothing reopens behind the
  user); on a phone, where there is no panel, the text streams into the chat
  instead of hiding behind the card.

### Changed

- **Playground — the thinking → writing boundary is now explicit.** While the
  model thinks, the reasoning line carries a pulsing dot and a running clock
  (« Réflexion en cours… depuis 12 s »); on the first answer word the clock
  stops on the measured duration and the block folds itself (« pensé 7 s »),
  while the answer fades in — one short transition, no guessing where the
  thinking ended. The model name now sits above the assistant's content, as in
  the reference screenshots.

## [0.1.1] - 2026-10-04

Six weeks of production use after `0.1.0`, driven almost entirely by what the
playground users asked for out loud: « ça ne ressemble pas à LM Studio », « on ne
se rend pas bien compte de la fin de réflexion », « la scrollbar ne marche pas ».
The playground gets its three visual registers, the reasoning phase gets a
visible boundary, and the side panel scrolls again.

### Added

- **Playground — LM Studio visual registers** — the user question keeps its
  bubble (capped at 65 % of the column), the reasoning lives in its own
  collapsible panel (subtle fill, hairline border, rounded — header
  « Réflexion » with the measured « pensé N s » duration), and the answer is
  plain text on the page (the bubble's `ghost` variant). Question, thought and
  answer are now told apart at a glance.
- **Playground — the thinking phase ends visibly** — the reasoning panel is
  controlled: it opens while the model thinks and folds itself on the first
  answer text. That fold *is* the reasoning→writing boundary; the duration
  stays one click away.
- **Thinking is adaptive** — the reasoning effort follows what the user
  actually asks for (« active le en fonction de ce que les user demandent »)
  instead of a fixed setting.
- **Runner — an `exllamav3` engine (TabbyAPI)** serves EXL3 models, with the
  same launch/stop ownership as llama.cpp and vLLM.
- **Runner — `Prism`, a binary for ternary GGUF** (Ternary-Bonsai-2-27B).
- **ASR sidecar is on-demand** — the transcription service loads lazily like
  the other media backends, and the portal reports an honest empty state until
  an admin or the first user starts it.
- **Dashboard — live decode rate and prefill gauge** for engines that expose no
  `/metrics` (derived from their own logs), plus a precise live gauge and a
  single error-notice identity. The ASR reaper now runs at startup.
- **Admin** — deleting a model also erases its files from disk; freed space is
  shown in MiB below 1 GiB.
- **Chat** — the system prompt carries the current date and time.

### Changed

- **Security** — responses now carry a `Permissions-Policy` header (the last
  P1 of the 2026-10-01 scan; the remaining P2/P3 findings — the last 504, the
  ratio bug, docs and permissions — are closed too).
- **HTTP** — user-facing 502s are served as 503, because Cloudflare replaces
  502 bodies with its own HTML and the client never saw ours.
- **Audit** — the rows `log_audit` evicts are archived instead of dropped.
- **Web stack** — SearXNG 2026.8.22 → 2026.10.2, crawl4ai 0.9.2 → 0.9.4.
- **Dependencies** — the two vulnerable `brace-expansion` releases are fixed;
  Dependabot's automatic PRs are paused (its alerts are kept); the dependency
  audit no longer reddens the run; the lockfile is reviewable by line.
- **Codebase** — the French comments and docstrings of the portal, the frontend
  and the services are translated to English (the PR template too), so the code
  reads in one language.
- **CI** — the gate runs the frontend unit tests, and generates the Next.js
  types before type-checking.

### Fixed

- **Playground — scrolling** — « il me ramène en bas quoi que je fasse »: the
  follow-the-bottom listener was mounted at page mount, before `ChatLayout`
  exists (it only renders on the first conversation), so it was never attached
  and the pin dragged the view to the bottom on every render. It now rides the
  `useStickToBottom` hook already used elsewhere.
- **Playground — the side panel scrollbar** — the scroll ref dug through the
  DOM for the first `overflow: auto` element and therefore attached to
  `CodeBlock`'s *inner* container (the `maxHeight` one), leaving the real
  scroll container unwatched: no live follow of the file being written, no
  « Descendre » button. `CodeBlock` no longer nests inside a scrollable
  container (its own docs warn against it) and the panel body is the single
  scroll container, for code as for documents.
- **Playground — clarifying questions** — the clickable questionnaire now
  survives MiMo's unfenced form, and the JSON-fenced form is recognised at
  all; mismatched brackets in question blocks are repaired.
- **Playground — file continuations** — a continuation no longer loses the
  file's ending, and a turn that only names a file no longer shows the
  previous half-file as a deliverable.
- **Playground — images** — images are attached when the selected model can
  actually read them, and a blocked canvas no longer yields all-black images.
- **Playground — a stopped image service is said out loud** instead of letting
  the model deny it.
- **ASR** — a failed model load is retryable (« relance la dictée » becomes
  real).
- **Health** — a failed `/v1/models` probe keeps the last known model list, and
  the dashboard derives its figures from `SpendLogs` when `/metrics` is absent.
- **Tests** — the flake motifs are frozen, the uncovered user zones are
  covered, and the gate's intermittent failure is root-caused and fixed; one
  blocking lint warning is silenced with its reason.
- **Install** — the bootstrap survives a host without `needrestart`, and the
  clone error message is actionable.

## [0.1.0] - 2026-10-01

First tagged release. The platform has been in production on a single DGX Spark
since July 2026; this tag freezes the state that the README and the screenshots
describe, and starts the version line.

### Added

- **Portal (`dgx-portal/`, Flask + Gunicorn)** — one internal service behind the
  frontend: session and API-key management, token budgets, model lifecycle,
  conversation history, memory graph, media jobs, web search, ranking, users and
  admin. Shared SQLite state, 4 workers × 16 threads.
- **Frontend (`dgx-portal-frontend/`, Next.js 16 + Astryx Design)** — the
  user-facing application: `/playground`, `/support`, `/memory`, `/ocr`,
  `/voice`, `/video`, `/image`, `/music`, `/search`, `/request`, `/ranking`,
  `/admin`, `/users`, dark and light themes, French-first UI with an English
  translation, and a `proxy.ts` that forwards to Flask while adding a per-request
  nonce CSP.
- **Playground** — streaming chat through LiteLLM, reasoning trace, self-hosted
  dictation (no browser speech API), web search with page extraction, document
  panel, image generation, pinned and shared conversations (`/c/<token>`).
- **Cronos assistant (`/support`)** — a tool-using assistant that can create an
  API key, request budget or request a model. The three sensitive tools do not
  execute inside the chat loop: they park a single-use, account-bound, 10-minute
  confirmation token that only the UI button can spend.
- **Memory** — a subject/relation/object graph the assistant keeps per account,
  on by default, with a recursive traversal in the UI and Markdown import.
- **Media pages** — OCR, voice cloning, video (MiniMax H3), image (FLUX.2 Klein)
  and music generation, each backed by an **on-demand** sidecar that the admin or
  the first user starts; the portal reports an honest empty state when its
  backend is not loaded.
- **Authentication** — local accounts (hashed), LDAP, and OIDC SSO, all three
  cumulative per account; optional WebAuthn/passkey second factor for local and
  LDAP accounts; server-revocable sessions with IP and user agent; a persistent
  lockout shared by every password check.
- **Budgets** — the blocking envelope is a LiteLLM user object shared by all of an
  account's keys, so the quota applies whatever client the key is used from.
  Models are priced at 1 per token, which is what makes the spend counter read as
  tokens. Per-user overrides, per-group defaults, time-limited boosts.
- **Model lifecycle** — `vllm-runner`, a host systemd daemon that owns
  launch/stop for llama.cpp, vLLM and ds4 engines; a strict argument allowlist;
  one start log per attempt; auto-resume of the last model on boot; and an
  `auto-model` LiteLLM alias that always points at whatever is currently served,
  so clients configure their endpoint once.
- **Operations** — `install.sh` bootstrap, systemd units for the runner and the
  network restrictions, a monitoring timer with email alerts, encrypted-free but
  verified SQLite backups, and `scripts/pre-push-check.sh` (test suite + i18n
  coverage + secret scan) wired as the pre-push hook.
- **Screenshots pipeline** — `scripts/screenshots.py` captures the published
  gallery in the dark theme, in English, with a dedicated non-admin `demo`
  account, blurs anything sensitive before capture and re-reads every final PNG
  by OCR to prove that no email, API key or other account's name made it in.

### Fixed

Deployment-path audit of 2026-10-01 (fresh-install rehearsal, read-only on the live
box). Every item below was invisible on the running installation, whose state had
been repaired by hand; each one broke a *clone*, a *nightly job* or a *restore*.

- **`install.sh` deployed 4 of the 13 systemd units**, no media wrapper and not a
  single sudoers rule: on a fresh install the admin could launch a model but no
  media sidecar (`command not allowed`), and the `web_net`/`ocr` network isolation
  the README describes was simply absent. It now installs every unit (monitor and
  backup via their timers), the 7 `/usr/local/sbin` wrappers, the 4 scoped sudoers
  files (validated with `visudo -c`), and grants `vllmrunner` the traversal ACL it
  needs on `/root` — without it the unit could not even open `runner.py` and
  `Restart=always` looped in silence.
- **The orphan purge could not delete anything** (`monitoring/backup.py`): the unit
  runs without `CAP_DAC_OVERRIDE` over a `0700` volume, and `os.path.isdir()`
  swallowed the `EACCES`, so it reported *0 deleted* every night while videos of
  10–100 MB accumulated. It now runs inside the portal container, which owns the
  files, and prints failures instead of hiding them.
- **The `portal.db` integrity probe was dead in silence** (`monitoring/monitor.py`):
  it returned *db unreadable* **and** `up=True`, so the exact failure it exists to
  catch (a silent reset, as on 2026-09-04) would no longer alert. Counters are read
  inside the container.
- **A fresh install handed every account a 0.002-token budget**: `docker-compose.yml`
  still overrode `KEY_MAX_BUDGET`/`KEY_BUDGET_DURATION` with the values of the first
  test account, defeating the code default of 200 M tokens/week.
- **Web search was broken on a fresh install**: SearXNG's `settings.yml` is not
  versioned (it holds a generated secret), so the image wrote its own — *without*
  the `json` format the portal requires (403 otherwise). `setup.sh` now generates it
  from `searxng/settings.yml.example`, and the template also fixes a latent YAML bug:
  two `server:` blocks in one document do not merge, so `image_proxy: true` was
  silently dropped by the second one.
- **The crawl4ai service token was created by nothing**: the missing bind mount
  became a *directory* in place of the token and the service refused to listen
  outside loopback. `setup.sh` generates it (0644 — the container is unprivileged
  and cannot read a root-owned 0600 file).
- **`dgx-portal/run-tests.sh` failed outside a directory named `ai-platform`**, so
  the command the README documents did not work on a fresh clone. The compose
  project name is now pinned, as the CI already did.
- **Deployed artifacts had drifted from the repository**: two recreate wrappers
  (a lost `--cap-drop ALL`/`no-new-privileges`, a lost image digest), a sudoers file
  that only existed on the machine, and a needrestart policy stored where a
  `cp systemd/*` would have made it inert. All four are now tracked, installed by
  `install.sh`, and identical on disk.
- **README corrections**: the public API path (`http://dgx.cronos.lan:4001` answers
  nothing — LiteLLM publishes no host port), `install.sh`'s scope, the
  `litellm/config.yaml` description, the repository tree, three modules missing from
  the backend map, and the `app.py` line count.
- **Log rotation was half-applied**: `searxng` and `crawl4ai` had been left out of
  the change that gave every other service a 20 MB × 5 cap; both were recreated.

### Security

- Hardened containers (`cap_drop: ALL`, `no-new-privileges`, non-root portal),
  digest-pinned images, restricted Docker networks, and host firewall rules that
  keep the model runner, LiteLLM and ComfyUI off the LAN.
- The portal's route inventory is locked by a test: a new route without an
  explicit guard fails the suite.
- Session state is re-read on every guarded request, CSRF tokens are compared in
  constant time, and account offboarding revokes sessions, API keys and the
  LiteLLM envelope.
- Full security audit of 2026-10-01 (code, services, network exposure,
  dependencies, sidecars): findings, fixes and the items left to the operator are
  recorded in `SECURITY.md` §3.4.

[Unreleased]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.18...v0.2.0
[0.1.18]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.17...v0.1.18
[0.1.17]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.16...v0.1.17
[0.1.16]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.15...v0.1.16
[0.1.15]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Sunderrrr/dgx-spark-llm-platform/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Sunderrrr/dgx-spark-llm-platform/releases/tag/v0.1.0
