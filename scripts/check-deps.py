#!/usr/bin/env python3
"""Known-vulnerable dependencies — blocking only when there is something to do.

Lesson of 2026-10-01 (see ci.yml, « Dependency audit »): an upstream advisory
often has NO fix yet, and a gate that fails on it stays red forever — and a
permanent red is no longer read. So this check FAILS on a high or critical
advisory that HAS a fix (upgrade and move on: Next.js 16.3.6 → 16.4.0 on
2026-10-13, SSRF in image optimization among others), and only REPORTS the
ones without a fix.

Covers what ships: the frontend's runtime packages (`npm audit --omit=dev`)
and the portal's Python requirements (`pip-audit`).

Usage: python3 scripts/check-deps.py [<pip-audit binary>]
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKING = {'high', 'critical'}


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


def pip_findings(pip_audit):
    r = subprocess.run([pip_audit, '-r', os.path.join(ROOT, 'dgx-portal/requirements.txt'), '--format', 'json'],
                       capture_output=True, text=True)
    try:
        deps = json.loads(r.stdout or '{}').get('dependencies', [])
    except json.JSONDecodeError:
        return [('error', f"pip-audit: unreadable output ({r.stderr.strip()[:200]})")]
    out = []
    for d in deps:
        for v in d.get('vulns', []):
            kind = 'fix' if v.get('fix_versions') else 'nofix'
            out.append((kind, f"pip {d['name']} {d.get('version')}: {v.get('id')} "
                              f"(fix: {', '.join(v.get('fix_versions') or []) or 'none'})"))
    return out


def main():
    pip_audit = sys.argv[1] if len(sys.argv) > 1 else 'pip-audit'
    findings = npm_findings() + pip_findings(pip_audit)
    for kind, text in findings:
        mark = '✗' if kind in ('fix', 'error') else '·'
        suffix = '' if kind != 'nofix' else '  [no fix published yet — reported, not blocking]'
        print(f"{mark} {text}{suffix}")
    if any(k in ('fix', 'error') for k, _ in findings):
        print("✗ a known vulnerability has a published fix: upgrade the package")
        return 1
    print("✓ no fixable high/critical vulnerability in the shipped dependencies")
    return 0


if __name__ == '__main__':
    sys.exit(main())
