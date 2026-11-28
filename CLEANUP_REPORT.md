# Cleanup, simplification, dependency update and security — final report

Six stacked branches, one per phase. **Five pull requests are open (#19–#23)**; the sixth
is this branch, `cleanup/06-report`, which carries this report and is opened last. Nothing
has been merged by the agent: the five PRs wait for you. The chain is
`master → cleanup/01-dead-code → … → cleanup/06-report`, and every PR targets the previous
phase, so they must be merged **in order**.

The repository has no `main` branch; `master` is the protected one, and every branch starts
from (or stacks on) it.

## 1. Size, before and after

| | before (`master`, `c0c190c`) | after (this branch) | delta |
|---|---:|---:|---:|
| tracked files | 497 | 519 | **+22** (29 added, 7 deleted, 1 renamed) |
| lines | 152 049 | 152 928 | **+879** |
| files changed | — | 132 | +5 294 / −4 420 |

The 29 additions are the 18 modules of `lib/i18n/`, the 7 modules extracted from
`PlaygroundPage.tsx`, `lib/shared-types.ts`, `portal-rs/src/clock.rs`,
`docs/security-review-webapp.md`, `CLEANUP_REPORT.md` and the relocated TabbyAPI patch.

The line count went **up** overall and that is expected: phase 1 deletes 1 053 lines and adds 468
(net −585) of dead code, while phases 2 and 6 *add* structure (module boundaries, comments,
this report). The
number that matters for maintenance is per component:

| component | files changed | added | removed | what moved |
|---|---:|---:|---:|---|
| `portal-rs` | 47 | 404 | **582** | 1 dead function, 1 dead field, 12 duplicated helpers collapsed, 4 crates dropped, blanket lint exemptions removed |
| `vllm-runner-rs` | 11 | 111 | **404** | 8 unused crates, 7 dead items, one route removed, `CRITICAL_FREE_GIB` wired |
| `dgx-portal-frontend` | 46 | 4 157 | 2 914 | `lib/i18n.tsx` 2 313 → 16 lines + 18 modules; `PlaygroundPage.tsx` 2 570 → 2 193; 2 npm packages dropped, 7 bumped |
| `docs` | 11 | 210 | 368 | one dead doc deleted, stale paths fixed, one new security note |
| `scripts` | 3 | 49 | 101 | the one-shot favicon generator deleted |
| sidecars + patch | 8 | 79 | 40 | `image-gen` / `voice-qwen` / `voice` fixes, the patch moved out of `vllm-runner/` |
| root files | 5 | 95 | 11 | `README`, `CHANGELOG`, `.dockerignore`, `.github` (`ci.yml`, `dependabot.yml`) |

Largest files after the work: `PlaygroundPage.tsx` 2 193, `portal-rs/src/ranking.rs` 1 011
(unchanged), `dgx-portal-frontend/app/(app)/admin/_components/ModelsTab.tsx` 926 — all under
the frozen ceilings (`scripts/check-structure.py`).

## 2. Dependencies

| ecosystem | removed | updated | deliberately not touched |
|---|---:|---:|---|
| npm (frontend) | **2** (`sharp`, `@types/katex`) | **7** (the three `@astryxdesign` packages 0.7.17, `@types/node`, `@types/react`, `eslint`, `eslint-config-next` 16.4.6) | `next`, `react`/`react-dom` (modified Next.js), `katex` 0.19 (0.x = breaking), `typescript` 7 (major), `voice-glow` 0.5 (0.x visual) |
| Cargo | **11 crates** (4 in `portal-rs`, 7 in `vllm-runner-rs`) — `tower` stayed, as a `[dev-dependencies]` entry a test uses | **69 packages** moved by `cargo update` (45 + 24), one direct one: `zip` 9.1.3 → 9.9.0 | every newer **major** (`cargo upgrade`), because each needs its own migration |
| pip (sidecars) | none | none | `torch`, `transformers`, `accelerate`, `fastapi`, `onnx`… — a bump means rebuilding a CUDA image and re-running a GPU smoke test (see §4) |
| Docker images | none | none | every base is digest-pinned; CUDA 13.0.3 / vLLM / node 20 must not move without a hardware check. `dependabot.yml` now also watches `decisions-d1/Dockerfile` |
| GitHub Actions | none | **1** (`actions/setup-node` 7.0.0 → 7.1.0, same major, SHA+comment kept) | `actions/checkout` is already the newest release of its major |

`@astryxdesign/cli` moved from `dependencies` to `devDependencies` (a build-time tool was
shipping in the production list).

## 3. Security

Full audit artifacts: `~/security-audit-skill/dgx-spark-llm-platform/run-4/`
(`findings.json` and `coverage-ledger.json` both pass the skill's validators).

### Found and fixed (code)

| finding | severity | fix |
|---|---|---|
| `/api/decisions/run` left the gateway whenever an attachment was present: no key, no spend row, no budget | **medium** | attachment refused with a 400 before any GPU work; the direct arm deleted |
| `import-git` / `sync` run the same 25 MiB download + 96 MiB deflate as the zip route, without its per-account bucket | low | they share the `rl-skills` bucket |
| the delete protection named the retired decision model, not `d1-3b`: a chat-catalog delete could `rm -rf` the mounted weights | low | `d1-3b` / `d1-*` protected |
| the OCR filter unit failed whenever the on-demand sidecar was stopped, and a final `echo` hid a real `ebtables` failure | informational | absent ≠ failure; a container without an address or a failed insertion now fails |

### Found on the live host and fixed during the run

These were the most consequential results of the audit: **the previous security PR was on
disk but not in effect**.

1. the internet-facing **LiteLLM container still held `RUNNER_TOKEN`** — recreated; only
   `MEDIA_TOKEN` remains;
2. **`vllm-restrict.service` was inactive** after a netbird restart (`PartOf=` without start
   propagation): no 8000/8001 drop, no raw perimeter chain — restarted, rules restored;
3. **`runtime/validation` was 0777** on the host — now 1777, with the repo's unit installed;
4. the **OCR→portal filter was absent** and the installed script was the IP-pinned version —
   current script installed, unit green.

### Still open (needs a decision or a window)

| item | why it is not done |
|---|---|
| **rotate `RUNNER_TOKEN`** | it was readable in the gateway's environment; rotation needs a `portal-rs` recreate **and** a `vllm-runner` restart, which kills the served model |
| **`vllmrunner` is in the `docker` group** | the socket is root-equivalent, so the 28 scoped sudoers rules are advisory, not containment. Either drop the `vllm-docker` engine and the group, or document that a runner compromise equals host root |
| **`vllm-restrict` does not survive a netbird restart** | restarted by hand; the durable fix (drop `PartOf=`, or add a netbird drop-in that starts it) is an operations choice |
| **`onnx==1.16.0`** in the voice image (2 critical advisories) | needs a pin bump **and** an image rebuild + GPU smoke test — target 1.22.0 |
| **`torch==2.10.0`** in `voice-qwen` (1 high) | the TTS model may not support 2.14; a bump needs a rebuild and a voice test |
| **`decisions-d1` has no Python pins at all** | the image is not reproducible or auditable; pin what the image actually contains, then rebuild |
| **Docker OS packages** | `trivy`/`grype`/`docker scout` are absent and cannot be installed offline: **unknown**, recorded as a gap |
| npm dev-only `minimatch` chain | 7 moderate, dev-only, production bundle clean; `npm audit fix` on a transitive lint dependency |
| `portal-rs` cargo advisories (`libsqlite3-sys`, `rsa`, `crypto-bigint`) | already in the repository's documented exception list; `rsa` is verification-only |

Secrets: **no real credential in the tree or in the 1 654 commits reachable at the audit's base `c317131`** (the 26 `.env` values were
matched against every commit, zero hits). The only real site value ever committed is an
SMTP hostname in a deleted Flask test — not a credential, and rewriting history would not
make readers forget it.

## 4. Not done — risky (all phases, gathered)

1. **pip packages** (`torchaudio` ×2, `torchvision`, `accelerate` ×3, `libsndfile1`/`git`/
   `python3-dev`/`build-essential` in `decisions-d1`): certified unused by AST analysis, but
   removing them means rebuilding CUDA images and re-running a GPU smoke test per service.
2. **`hardware.yml`**: no self-hosted runner on this machine, and its job is already
   reachable from the admin panel. Deleting or keeping it is an operator decision.
3. **`allowScripts`** still pins `@astryxdesign/*@0.1.8`: npm 12 therefore blocks their
   postinstall (a setup nudge; `astryx doctor` passes). Updating it would silence a warning
   and **re-enable install scripts** — deliberately not done.
4. **`@types/node` 26 against a `node:20` image**: pre-existing divergence; fixing it means
   moving the runtime to Node 26.
5. **Playground page at 2 198 lines**: reaching ~1 200 needs state/effects/handlers moved
   into hooks (21 effects, 6 network calls at mount). Effect and network ordering cannot be
   proven unchanged without replaying the browser journeys, so it was not attempted; the
   frozen ceiling follows the file down.
6. **No regression test for the dictionary identity, the decisions refusal, or the two
   throttles**: the first needs a resolver in the Node test harness; the other two need a
   database and a simulated network. The i18n checker was tightened instead.
7. **`py_round0`** and three `py_str` paraphrases remain duplicated: safe to fold, out of
   the phase's scope.
8. **CSP nonce fresh per request** (phase 5): it defeats page caching. A design choice with
   a performance cost — yours to confirm.

## 5. Manual test checklist on the real DGX Spark

Run these **after merging the six PRs**, on the machine, in this order.

### Installation and units

- [ ] `./install.sh` on a copy first — it rewrites units and installs scripts. Then check
      that `/usr/local/bin/vllm-runner` is the rebuilt binary, that the `/usr/local/sbin`
      wrappers (`ocr-restrict.sh`, `model-files-rm.sh`, `asr-recreate.sh`,
      `image-recreate.sh`, `dgx-docker-restrict.sh`) are installed, and that
      `nvidia-cdi-refresh.path.d/dgx-driver-libs.conf` exists.
- [ ] `systemctl status vllm-restrict` → **active**, and `iptables -S INPUT` shows the
      8000/8001 drops; `iptables -t raw -S` shows `DGX-PERIMETER`.
- [ ] `systemctl restart netbird` then `systemctl status vllm-restrict` → **the unit must
      come back**. If it does not, the `PartOf=` gap is still open (see §3).
- [ ] `ls -ld runtime/validation` → `drwxrwxrwt` (1777, sticky).

### Model serving

- [ ] `sudo systemctl restart vllm-runner` **in a window**: it loads the rebuilt binary
      (runner fixes from phase 1 and the previous security PR). This kills the served model
      and reloads it — do it when nobody is chatting.
- [ ] After the reload: Admin → Modèles → « Lancer » on the fine-tuned voice → the engine
      must come up, `/api/speak` answer, and the card show `custom-qwen-tts`.
- [ ] The 0.6B cloned voice: « Lancer » → reference clip mounted, warm-up under 5 s,
      `/api/speak` returns audio.
- [ ] `POST /models/download/clear` now 404s: nothing in the portal calls it.

### Media sidecars

- [ ] OCR: launch from the admin, then `ebtables -L FORWARD` must contain the 5100 rule; the
      unit must be green **with the sidecar stopped too**.
- [ ] ASR: a dictation round trip; the decode semaphore must not change latency noticeably.
- [ ] Image: `/health` must carry `esrgan_error`; upscaling must still work.
- [ ] Voice: `/tts` and `/clone` on the 0.6B, and `/api/speak` on both engines.
- [ ] `model-files-rm.sh local d1-3b` → exit 4, folder intact.

### Portal, frontend, gateway

- [ ] Login, logout, a second account, an admin account: `/api/admin` must answer 401 then
      403 for the non-admin.
- [ ] Attach a file on the **Décisions** page: the refusal message must appear immediately
      (no round trip), in French **and** in English.
- [ ] Skills: a git import and a zip import, then six in a minute → « Trop d'imports ».
- [ ] Playground: send a message, stop a generation, open the history and filter, the
      document panel, the composer with voice. These are the paths whose JSX moved.
- [ ] French/English on Playground, Admin, Réglages — the dictionary is byte-identical but
      the split is the one thing only your eyes can confirm.
- [ ] Visually spot-check dense pages (Admin, Playground, Support, the OCR/video/image/voice
      skeletons): `@astryxdesign` 0.7.17 changes text leading (14 px: 21 → 20), badge width
      and the skeleton colour.
- [ ] `docker inspect litellm` must show **no `RUNNER_TOKEN`**, and LiteLLM must still serve
      the models list.

### Security follow-ups

- [ ] Rotate `RUNNER_TOKEN` (portal-rs recreate + runner restart in the same window).
- [ ] Decide the `docker` group question (§3).
- [ ] Install `trivy` or `grype` in a window with network, then scan the six local images.
- [ ] Pin `decisions-d1`'s Python stack and rebuild; bump `onnx` to 1.22.0 in `voice`.

## 6. The six pull requests, in merge order

| # | branch | title | one line |
|---|---|---|---|
| 1 | `cleanup/01-dead-code` | remove dead code, unused dependencies and stale docs | 62 files, +468 / −1 053: dead functions, **14** unused crates/packages, a Flask-era doc, and the `.dockerignore` that was shipping secrets to the builder |
| 2 | `cleanup/02-simplify` | remove the real duplications and split the two giant files | 14 epoch helpers → 1, `i18n.tsx` 2 313 → 16 + modules, `PlaygroundPage` 2 570 → 2 193 without moving state |
| 3 | `cleanup/03-deps-update` | dependency updates within their major versions | 7 npm packages, 69 cargo packages, setup-node 7.1.0, `decisions-d1` added to dependabot |
| 4 | `cleanup/04-security-code` | security audit of the code | 4 findings closed **and** 4 live-host holes repaired (gateway token, perimeter firewall, log dir, OCR filter) |
| 5 | `cleanup/05-security-webapp` | security posture of the running application | measured on an isolated instance: headers, cookies, CSRF, access control, CORS — all in order |
| 6 | `cleanup/06-report` | this report, the docs update and the checklist | — |

Each PR body carries its own objective, proofs, before/after numbers, reviewer summary and
"not done" list; this report gathers the cross-cutting parts.
