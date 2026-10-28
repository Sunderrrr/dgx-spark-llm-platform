#!/usr/bin/env python3
"""Known-vulnerable dependencies — blocking only when there is something to do.

Lesson of 2026-10-01 (see ci.yml, « Dependency audit »): an upstream advisory
often has NO fix yet, and a gate that fails on it stays red forever — and a
permanent red is no longer read. So this check FAILS on a high or critical
advisory that HAS a fix (upgrade and move on: Next.js 16.3.6 → 16.4.0 on
2026-10-13, SSRF in image optimization among others), and only REPORTS the
ones without a fix.

Covers what ships:
  - the frontend's runtime packages (`npm audit --omit=dev`);
  - BOTH Rust crates (`portal-rs/Cargo.lock`, `vllm-runner-rs/Cargo.lock`) via
    `cargo audit`, whose JSON tells whether a patched version exists.

The Rust side was a documented blind spot until 2026-10-28: portal-rs carried
7 advisories (zip, rsa, rustls-webpki, webpki-roots, crypto-bigint) that nothing
looked at, in CI or on the box. All of them have no fix today, so they are
reported — but the day one gets a patched version, this gate reddens.

Two things this file learned the hard way (2026-10-28), both worth keeping:

  - the patched range lives at `vulnerability.versions.patched`, NOT under
    `advisory`: reading the wrong key marked everything "no fix", i.e. a gate
    that could never fail. `crypto-bigint`, `rustls-webpki`, `webpki-roots` and
    `zip` all had published fixes at the time.
  - "a fix exists" is not "a fix is reachable". `p256 0.13` pins
    `elliptic-curve 0.13` which pins `crypto-bigint ^0.5`, so the >=0.7.6 fix
    cannot be installed without a major bump of the JWT stack; `zip` is fixed
    only in >=9.0.0, a different API. Those two families are therefore listed in
    IGNORES below, WITH a reason and a re-check trigger — and a stale entry (an
    advisory that no longer appears) is itself a failure, so the list cannot rot
    silently.

`cargo audit` is an optional tool: when it is absent the Rust layer is SKIPPED
and said so, unless CHECK_DEPS_REQUIRE_AUDIT=1 (set by CI, where the tool is
installed on purpose — a check that quietly disappears is how a blind spot
survives).

Usage: python3 scripts/check-deps.py
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKING = {'high', 'critical'}
CRATES = ('portal-rs', 'vllm-runner-rs')

# Advisories that CANNOT be fixed in this tree today, each with the reason and
# what would lift it. Anything not listed here fails when a patch exists.
IGNORES = {
    'RUSTSEC-2026-0387': "crypto-bigint >=0.7.6 needs p256 >=0.14 / elliptic-curve >=0.14 "
                        "(jsonwebtoken stack); re-check when those bump",
    'RUSTSEC-2023-0071': "rsa: no fixed release published upstream (Marvin attack, needs decryption)",
    'RUSTSEC-2026-0367': "zip >=9.0.0 only (major API); portal uses 2.x — and never calls extract()",
    'RUSTSEC-2026-0369': "zip >=9.0.0 only (major API); portal extracts in memory via by_index_raw",
    'RUSTSEC-2026-0368': "zip >=9.0.0 only (major API); no ZipArchive::extract() call in the tree",
}
seen_ignores = set()


def npm_findings():
    r = subprocess.run(['npm', 'audit', '--omit=dev', '--json'], cwd=os.path.join(ROOT, 'dgx-portal-frontend'),
                       capture_output=True, text=True)
    try:
        vulns = json.loads(r.stdout or '{}').get('vulnerabilities', {})
    except json.JSONDecodeError:
        return [('error', f"npm audit: unreadable output ({r.stderr.strip()[:200]})")]
    out = []
    for name, v in sorted(vulns.items()):
        if v.get('severity') not in BLOCKING:
            continue
        fixable = v.get('fixAvailable') not in (False, None)
        titles = sorted({x.get('title', '') for x in v.get('via', []) if isinstance(x, dict)})
        out.append(('fix' if fixable else 'nofix',
                    f"npm {name} ({v.get('severity')}, {v.get('range', '?')}): " + '; '.join(titles)[:300]))
    return out


def cargo_audit_path():
    """`cargo audit` needs the cargo-audit binary on PATH (cargo subcommand)."""
    if shutil.which('cargo-audit'):
        return True
    found = shutil.which('cargo')
    if not found:
        return False
    home = os.path.expanduser('~/.cargo/bin')
    return os.path.exists(os.path.join(home, 'cargo-audit'))


def rust_findings():
    if not shutil.which('cargo') or not cargo_audit_path():
        if os.environ.get('CHECK_DEPS_REQUIRE_AUDIT') == '1':
            return [('error', 'cargo-audit is missing but CHECK_DEPS_REQUIRE_AUDIT=1 — install it (cargo install cargo-audit)')]
        return [('skip', 'cargo-audit not installed: the Rust advisories were NOT checked')]
    env = dict(os.environ)
    # cargo-audit lives next to cargo; make sure the subcommand is found even
    # when ~/.cargo/bin is not on PATH (CI installs it there).
    env['PATH'] = os.path.expanduser('~/.cargo/bin') + os.pathsep + env.get('PATH', '')
    out = []
    for crate in CRATES:
        cwd = os.path.join(ROOT, crate)
        r = subprocess.run(['cargo', 'audit', '--json'], cwd=cwd, capture_output=True, text=True, env=env)
        try:
            data = json.loads(r.stdout or '{}')
        except json.JSONDecodeError:
            # Not "no advisory": a tool that cannot answer must not look green.
            detail = (r.stderr or r.stdout or '').strip().splitlines()[-1:] or ['no output']
            out.append(('error', f"cargo audit ({crate}): unreadable output — {detail[0][:200]}"))
            continue
        for v in data.get('vulnerabilities', {}).get('list', []):
            adv, pkg = v.get('advisory', {}), v.get('package', {})
            # The patched range is a SIBLING of `advisory`, not inside it.
            patched = v.get('versions', {}).get('patched') or []
            where = f"{pkg.get('name')} {pkg.get('version')} ({crate})"
            text = f"{adv.get('id')} {where}: {adv.get('title', '')[:200]}"
            ident = adv.get('id', '')
            if ident in IGNORES:
                seen_ignores.add(ident)
                out.append(('ignored', f"{text} — ignored: {IGNORES[ident]}"))
            elif patched:
                out.append(('fix', f"{text} — patched in {', '.join(patched)}"))
            else:
                out.append(('nofix', text))
        for w in data.get('warnings', {}).get('unmaintained', []):
            pkg, adv = w.get('package', {}), w.get('advisory', {})
            ident = adv.get('id', '')
            label = f"{ident} {pkg.get('name')} {pkg.get('version')} ({crate}) is unmaintained"
            if ident in IGNORES:
                seen_ignores.add(ident)
                out.append(('ignored', f"{label} — ignored: {IGNORES[ident]}"))
            else:
                out.append(('nofix', label))
    stale = sorted(set(IGNORES) - seen_ignores)
    for ident in stale:
        out.append(('stale', f"{ident} is in IGNORES but no longer reported — remove the entry"))
    return out


def main():
    findings = npm_findings() + rust_findings()
    for kind, text in findings:
        mark = {'fix': '✗', 'error': '✗', 'stale': '✗', 'skip': '·', 'nofix': '·', 'ignored': '⊘'}[kind]
        suffix = '  [no fix published yet — reported, not blocking]' if kind == 'nofix' else ''
        print(f"{mark} {text}{suffix}")
    if any(k in ('fix', 'error', 'stale') for k, _ in findings):
        print("✗ blocking finding(s) above: a vulnerability WITH a published fix, or a check that could not run")
        return 1
    if any(k == 'skip' for k, _ in findings):
        print("· the Rust advisories were not checked (cargo-audit absent)")
    print("✓ no fixable high/critical vulnerability in the shipped dependencies")
    return 0


if __name__ == '__main__':
    sys.exit(main())
