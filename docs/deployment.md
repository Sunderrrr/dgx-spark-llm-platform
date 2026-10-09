# Deployment

How to put the platform on a box and how to ship a change to it. For what the
pieces are, see [architecture](architecture.md); for the keys you fill in,
[configuration](configuration.md).

## Prerequisites

Prerequisites: a DGX Spark (or any CUDA host), a reachable LDAP directory
(Authentik's LDAP outpost here) plus an OIDC provider, and outbound internet for
pulling images and model weights.

## One-shot bootstrap

```bash
# One-shot bootstrap: installs Docker, Python/pipx, vLLM, clones the repo,
# generates .env, installs the systemd units + firewall rules + media sidecar
# wrappers, then PRINTS the next steps — it does not start the stack itself,
# because the secrets below the bootstrap line still have to be filled in.
curl -fsSL https://raw.githubusercontent.com/Sunderrrr/dgx-spark-llm-platform/master/install.sh | sudo bash
```

## Manual install

Or manually:

```bash
git clone https://github.com/Sunderrrr/dgx-spark-llm-platform.git
cd dgx-spark-llm-platform
sudo ./install.sh          # installs packages + systemd units, generates .env
#   → then fill the remaining secrets in .env (LDAP/OIDC/SMTP/Discord)
docker compose up -d       # frontend + backend + gateway + database
```

Then open the portal (`http://<host>:5000`, or your HTTPS domain behind Traefik),
go to **Admin**, and launch a model from the catalog.

`install.sh` (via `setup.sh`) generates the random secrets; the rest of `.env` is
filled in by hand — see [configuration](configuration.md). The install scripts are
**tested, not just syntax-checked** (2026-10-02): `setup.sh` runs for real in a
throwaway copy (fresh / idempotent rerun / artifact repair) and `install.sh` runs
end to end in a throwaway Ubuntu container (real apt + get.docker.com + visudo,
stubbed `systemctl`/`vllm`); the recreate scripts are A/B'd old-vs-new with
logging stubs for `docker`/`iptables` — commands and generated files must match.

## Shipping a change to a running box

```bash
./scripts/deploy.sh                # portal + frontend
./scripts/deploy.sh litellm searxng
```

**Deploy = build + replace + VERIFY — never the first two alone.** The gap this
closes is simple and it bit us: `docker compose up -d` prints « Started » whether
the platform works or not. `deploy.sh` rebuilds the named services, replaces
them, waits for the remount, then runs [`scripts/smoke.sh`](../scripts/smoke.sh)
against the **running** service; on failure it prints the rollback line
(`git checkout <commit> && ./scripts/deploy.sh <services>`) and exits non-zero.

Two rules that are not obvious:

- **Deploying = rebuilding the image**: the portal code is not mounted, so
  `build` then `up -d`, never `restart`. The same holds for the frontend.
- **`docker compose up -d <svc>` does NOT recreate a container whose definition
  has not changed**: a modified mounted file stays invisible, the module having
  been imported at startup. To reload a mounted file, `docker compose restart <svc>`.

Any heavy build is memory-capped while the model is loaded (the frontend image's
`next build` runs while the chat model occupies ~105 GiB of the 121, and an
uncapped build can push the kernel into swapping — the OOM-killer then picking
the biggest RSS, i.e. the SERVED model). The Dockerfile therefore caps the heap
(`ARG NODE_HEAP_MB`, default 2560): a cap turns an unbounded risk into a bounded
build failure. Raise it via `--build-arg NODE_HEAP_MB=…`.

## First launch

Once the stack is up: **Admin → Launch**, pick a model from the catalog. The
runner downloads the weights on first launch and starts the engine — see
[operations](operations.md) for engines, auto-resume and the model notes.

## Where the host state lives

Installed outside the repository by `install.sh` (see
[architecture → Repository layout](architecture.md#repository-layout) for the
tracked sources): the systemd units (`vllm-runner.service`, the firewall units,
the sidecar recreate wrappers in `/usr/local/sbin/`, the `sudoers` fragments in
`/etc/sudoers.d/vllmrunner-*`), and the firewall rules. Upgrading the box means
re-running `install.sh`; the units are idempotent.
