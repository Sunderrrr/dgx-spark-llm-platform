# Contributing

Thanks for looking. This is a **self-hosted, single-box platform** — one DGX
Spark serving models to a small group of people — so the rules below are less
about process and more about not breaking a live service while improving it.

The architecture, the feature tour and the deployment steps live in
[`README.md`](README.md); the security posture lives in
[`SECURITY.md`](SECURITY.md). Read the README once for the map, then come back
here for how we work.

---

## The one rule that matters most

**A running chat model is production traffic.** Never stop, restart or
reconfigure the served model to test something: on this box a restart kills
whatever is being generated with no warning, and the model is the product.
If your change touches `vllm-runner/runner.py`, the units under `systemd/`, or
the model catalogue, say so explicitly in your pull request — the change only
takes effect at the next legitimate launch, and **nobody should trigger one just
to see the diff land**.

Two more that follow from the same principle:

- **Never add a `--memory` limit to a sidecar container.** Memory is unified
  (system RAM and GPU memory are one pool), so a container limit also caps GPU
  memory and makes CUDA model loading fail.
- **Deploy by rebuilding the image** (`docker compose build <svc> &&
  docker compose up -d <svc>`). `docker compose restart` does **not** pick up new
  code — it restarts the same container with the same image — and it hides the
  mistake behind a service that looks like it reloaded.

---

## Set up

```bash
git clone https://github.com/Sunderrrr/dgx-spark-llm-platform.git
cd dgx-spark-llm-platform
./scripts/install-git-hooks.sh     # wires the pre-push gate into .git/hooks
cp .env.example .env               # then fill in the secrets by hand
./install.sh                       # Docker, Python/pipx, vLLM, units, stack up
```

`.env` is git-ignored and holds real credentials: never commit it, never paste
its contents into an issue, a log or a commit message.

## Run the checks

```bash
./scripts/pre-push-check.sh        # everything the hook runs: tests + i18n + secret scan
./dgx-portal/run-tests.sh          # backend suite only (unittest, throwaway container)
./dgx-portal/run-tests.sh test_app # one module
```

Frontend:

```bash
cd dgx-portal-frontend
npm ci
npx tsc --noEmit
npx eslint .
```

The backend suite runs in a container built from the portal image, with a fresh
SQLite database and no mounted volume — it can never touch real data. The
pre-push gate is the same thing plus an i18n coverage check and a secret scan of
the commits about to be pushed. **A red gate is a stop, not an obstacle**: fix
the cause, or say in the pull request why the check itself is wrong.

## Conventions

- **Commits** — [Conventional Commits](https://www.conventionalcommits.org/), in
  French, scoped to the area: `fix(portail): …`, `fix(frontend): …`,
  `fix(runner): …`, `docs(securite): …`, `chore(infra): …`. The subject line says
  what changed; the body says **why**, and includes the measurement that proves it
  when the change is a fix. Small, reviewable commits beat one large one.
- **Comments and code identifiers** — French, because the operators and the
  users are French-speaking. Comments explain the *why* and the non-obvious
  measurement, not the *what*.
- **UI strings** — French first, then the English translation. A user-visible
  string goes through the translation layer; `scripts/check-i18n.py` (run by the
  CI) fails when a `t()` key has no English entry, because a missing key silently
  falls back to French for English users.
- **Components** — the frontend is built on Astryx Design. Use its components and
  design tokens; a raw `<div>` with hand-written styles where a component exists,
  or a hard-coded colour where a token exists, will be asked to change.
- **Measure, don't guess** — for anything non-obvious (a performance fix, a
  rendering bug, a memory tradeoff), run the experiment and put the number in the
  commit message or the pull request. Several comments in this repository exist
  only to record a measurement that contradicts the intuitive fix; please do not
  "simplify" those away without re-measuring.

## Repository layout

Top level: `dgx-portal/` (Flask API), `dgx-portal-frontend/` (Next.js app),
`vllm-runner/` (host model-lifecycle daemon), `systemd/` (units),
`monitoring/` (host-side checks and backups), one directory per media sidecar
(`ocr/`, `voice/`, `voice-qwen/`, `asr/`, `image-gen/`, `music/`),
`scripts/` (developer tooling), `assets/` (README screenshots).
The per-file module map is in [`README.md`](README.md#repository-layout).

## Screenshots

The images in `assets/` are **generated, never hand-captured**:

```bash
python scripts/screenshots.py            # capture (dark theme, English, demo account)
python scripts/screenshots.py --verify   # size, dark theme, expected content, privacy
python scripts/screenshots.py --install  # copy the validated files into assets/
```

If your change alters a page's layout, refresh the gallery in the same pull
request. `--verify` reads the text back out of each PNG and fails on an email
address, an API key or another account's name; that check is the only thing
standing between a careless capture and a public repository, so never install a
file it rejected.

## Reporting a problem

Bugs and feature requests: use the issue templates. Anything that looks like a
vulnerability: [`SECURITY.md`](SECURITY.md) first — please do not open a public
issue for it.

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
