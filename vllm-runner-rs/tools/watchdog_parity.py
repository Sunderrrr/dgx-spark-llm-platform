#!/usr/bin/env python3
"""The watchdog compared: an engine that fails at once is relaunched by the
watchdog, up to MAX_AUTO_RETRIES, then left alone; the failure counter lives
in the state directory. Both runners, same stand-ins, isolated network.

    tools/run_isolated.sh python3 tools/watchdog_parity.py <runner.py> <vllm-runner>
"""
import json
import os
import sys
import time
from pathlib import Path

import http_parity as hp

HERE = Path(__file__).resolve().parent


def env_crash(home):
    e = hp.env_for(home)
    e["VLLM_BIN"] = str(HERE / "fakes" / "crash.sh")
    e["MIN_FREE_TO_LAUNCH_GIB"] = e.get("MIN_FREE_TO_LAUNCH_GIB", "30")
    return e


def run(label, cmd, floor=None):
    home = hp.Path(hp.tempfile.mkdtemp(prefix=f"wd-{label}-"))
    env = env_crash(home)
    if floor is not None:
        env["MIN_FREE_TO_LAUNCH_GIB"] = str(floor)
    proc = hp.serve(cmd, env)
    try:
        hp.call("POST", "/launch", {"hf_model_id": "org/crashy", "model_name": "crashy"})
        end = time.time() + 400
        logs = []
        while time.time() < end:
            logs = json.loads(hp.call("GET", "/logs?n=2000")[1])["logs"]
            if any("auto-resume DISABLED" in l or "REFUSED" in l for l in logs) and time.time() > end - 300:
                break
            if any("auto-resume DISABLED" in l for l in logs):
                break
            time.sleep(5)
        fails_file = home / "resume_failures"
        fails = fails_file.read_text().strip() if fails_file.exists() else None
        attempts = sorted({l.split("attempt ")[1].split("/")[0] for l in logs if "auto-resume (attempt" in l})
        disabled = any("auto-resume DISABLED" in l for l in logs)
        refused = sum("REFUSED" in l for l in logs)
        return {"disabled": disabled, "attempts": attempts, "failures": fails, "refused": refused > 0}
    finally:
        hp.stop_daemon(proc)


def main(runner_py, rust_bin):
    if not hp.port_is_free():
        raise SystemExit("port 8001 is in use: run this in an isolated namespace")
    results = {}
    results["py"] = run("py", [sys.executable, runner_py])
    results["rs"] = run("rs", [rust_bin])
    print(json.dumps(results, indent=1))
    same = results["py"] == results["rs"]
    print("OK" if same else "DIFF")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
