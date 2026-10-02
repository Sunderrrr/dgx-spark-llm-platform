<!--
Thank you. A few reminders before opening the PR — they come from CONTRIBUTING.md.

- A served chat model is production: do not restart it to test your change. If your
  PR touches `vllm-runner/runner.py`, `systemd/` or the model catalog, say so
  explicitly below.
- Deploying = rebuilding the image, then `docker compose up -d <service>`.
  `docker compose restart` does NOT pick up the new code.
- Never put `--memory` on a sidecar (memory is unified, the cap also breaks CUDA
  loading).
- No secret, no key, no personal data in the PR or in the screenshots (the
  repository is public).
-->

## What this PR does

<!-- In one or two sentences: the real problem, who hit it, and what the change
     alters. -->

## Why this way

<!-- The options considered, the one chosen, and what would make you revisit it.
     For a non-obvious fix, paste the MEASUREMENT that proves it (command +
     output): several comments in this repo exist only to record a measurement
     that contradicts the intuitive fix. -->

## Checks

- [ ] `./scripts/pre-push-check.sh` is green (tests + i18n + secret scan)
- [ ] If the frontend changes: `npx tsc --noEmit` and `npx eslint .` are green
- [ ] If a page changes appearance: the `assets/` gallery has been redone
      (`scripts/screenshots.py` then `--verify` then `--install`)
- [ ] If the HTTP contract, `.env` or deployment changes: `CHANGELOG.md` has an
      entry under `## [Unreleased]`
- [ ] No secret, no API key, no personal data added
- [ ] The served model was **not** restarted

## Operational impact

<!-- Fill in if applicable: the change only takes effect at the next legitimate
     runner launch (say so explicitly!), a systemd unit must be reinstalled, an
     `.env` variable must be added by hand, a database migration is needed, an
     image must be rebuilt. -->

None.
