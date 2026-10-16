#!/usr/bin/env python3
"""The runner's HTTP surface compared: runner.py (Flask) and the Rust daemon,
one after the other on port 8001, with the same stand-in engine and the same
stand-in sudo, in fresh state directories. Answers must match after masking
what varies between two runs (pids, timestamps, waiting times, host metrics).

    python3 tools/http_parity.py <runner.py> <vllm-runner binary>
"""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN = "parity-token-0123456789"
PORT = 8001


def env_for(home: Path):
    fakes = str(HERE / "fakes")
    e = dict(os.environ)
    e.update({
        "RUNNER_TOKEN": TOKEN, "HOME": str(home), "RUNNER_LOG_DIR": str(home / "logs"),
        "MODELS_DIR": str(home / "models"), "HF_HOME": str(home / "hf"),
        "TEMPLATES_DIR": str(home / "templates"), "TABBY_DIR": str(home / "tabby"),
        "VLLM_BIN": str(HERE / "fakes" / "engine.sh"), "LLAMA_BIN": str(home / "missing-llama"),
        "DS4_BIN": str(home / "missing-ds4"), "EXL3_PY": str(home / "missing-py"),
        "PATH": f"{fakes}:/usr/local/bin:/usr/bin:/bin",
        "MAX_JOBS": "4",
    })
    for k in ("VLLM_BIN_025", "VLLM_BIN_027", "VLLM_BIN_028", "VLLM_BIN_NIGHTLY",
              "LLAMA_BIN_NEXT", "LLAMA_BIN_K2", "LLAMA_BIN_PRISM"):
        e.pop(k, None)
    (home / "models" / "local-model" / "snap").mkdir(parents=True, exist_ok=True)
    (home / "models" / "local-model" / "snap" / "w.gguf").write_bytes(b"x" * 10)
    (home / "templates").mkdir(parents=True, exist_ok=True)
    return e


def call(method, path, body=None, token=TOKEN):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def mask(text):
    """Answers as data: parsed and written back canonically (Flask's compact
    JSON and its trailing newline are not what the two runners differ on),
    then the values that legitimately differ between two runs are masked."""
    try:
        value = json.loads(text)
    except ValueError:
        return mask_text(text)
    return mask_text(json.dumps(mask_value(value), sort_keys=True))


def mask_value(v):
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if k in ("pid", "time", "checked_at", "uptime_s"):
                out[k] = "<masked>" if x is not None else None
            else:
                out[k] = mask_value(x)
        return out
    if isinstance(v, list):
        return [mask_value(x) for x in v]
    if isinstance(v, str):
        return mask_text(v)
    return v


def mask_text(text):
    text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", "<ts>", text)
    text = re.sub(r"\d{8}-\d{6}", "<stamp>", text)
    text = re.sub(r"waited \d+s", "waited <s>", text)
    text = re.sub(r"Memory released: [\d.]+ GiB free", "Memory released: <g>", text)
    text = re.sub(r"\[runner\] \(vllm\) \$ .*", "[runner] (vllm) $ <cmd>", text)
    text = re.sub(r"\[runner\] \(llamacpp\) \$ .*", "[runner] (llamacpp) $ <cmd>", text)
    text = re.sub(r"\[runner\] \(ds4\) \$ .*", "[runner] (ds4) $ <cmd>", text)
    text = re.sub(r"\[runner\] \(exllamav3\) \$ .*", "[runner] (exllamav3) $ <cmd>", text)
    text = re.sub(r"\[runner\] \(vllm\) \$ .*", "", text)
    return text


def wait_running(timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        _, body = call("GET", "/status")
        if json.loads(body).get("status") == "running":
            return True
        time.sleep(0.2)
    return False


def scenario():
    out = []
    def rec(name, res):
        status, body = res
        out.append({"step": name, "status": status, "body": mask(body)})

    rec("no token", call("GET", "/status", token=None))
    rec("wrong token", call("GET", "/status", token="x" * 20))
    rec("status idle", call("GET", "/status"))
    rec("launch no id", call("POST", "/launch", {}))
    rec("launch bad id", call("POST", "/launch", {"hf_model_id": "--model=/x"}))
    rec("launch dotdot", call("POST", "/launch", {"hf_model_id": "/a/../b"}))
    rec("launch unknown engine", call("POST", "/launch", {"hf_model_id": "org/m", "engine": "tpu"}))
    rec("launch engine missing", call("POST", "/launch", {"hf_model_id": "org/m", "engine": "llamacpp"}))
    rec("launch bad flag", call("POST", "/launch", {"hf_model_id": "org/m", "vllm_args": "--model /etc"}))
    rec("launch flag needs value", call("POST", "/launch", {"hf_model_id": "org/m", "vllm_args": "--max-model-len"}))
    rec("launch vllm", call("POST", "/launch", {"hf_model_id": "org/first", "model_name": "first", "vllm_args": "--max-model-len 4096 --enable-auto-tool-choice"}))
    rec("running after launch", (200, "running" if wait_running() else "not running"))
    rec("status running", call("GET", "/status"))
    rec("logs 5", call("GET", "/logs?n=5"))
    rec("logs bad n", call("GET", "/logs?n=x"))
    rec("launch second (replaces)", call("POST", "/launch", {"hf_model_id": "org/second", "model_name": "second"}))
    rec("running second", (200, "running" if wait_running() else "not running"))
    rec("logs after second", call("GET", "/logs?n=500"))
    rec("stop", call("POST", "/stop"))
    rec("stop again", call("POST", "/stop"))
    rec("status stopped", call("GET", "/status"))
    rec("launch local gguf", call("POST", "/launch", {"hf_model_id": "local:local-model", "model_name": "loc", "engine": "vllm"}))
    rec("stop after", call("POST", "/stop"))
    rec("delete no id", call("POST", "/models/delete-files", {}))
    rec("delete bad id", call("POST", "/models/delete-files", {"hf_model_id": "x"}))
    rec("delete dotdot", call("POST", "/models/delete-files", {"hf_model_id": "local:a..b"}))
    rec("delete absent", call("POST", "/models/delete-files", {"hf_model_id": "org/absent"}))
    rec("delete busy", call("POST", "/models/delete-files", {"hf_model_id": "org/busy"}))
    rec("delete ok", call("POST", "/models/delete-files", {"hf_model_id": "org/gone"}))
    for side in ("ocr", "voice", "asr", "image", "video"):
        if side == "video":
            rec("video status", call("GET", "/video/status"))
            rec("video stop", call("POST", "/video/stop"))
            rec("video start", call("POST", "/video/start"))
            rec("video logs", call("GET", "/video/logs"))
        else:
            rec(f"{side} status", call("GET", f"/{side}/status"))
            rec(f"{side} stop", call("POST", f"/{side}/stop"))
            rec(f"{side} start", call("POST", f"/{side}/start"))
            rec(f"{side} logs", call("GET", f"/{side}/logs"))
    rec("ocr launch bad id", call("POST", "/ocr/launch", {}))
    rec("ocr launch bad flag", call("POST", "/ocr/launch", {"hf_model_id": "org/x", "vllm_args": "--evil 1"}))
    rec("ocr launch ok", call("POST", "/ocr/launch", {"hf_model_id": "org/x", "vllm_args": "--max-model-len 2048"}))
    rec("voice launch bad", call("POST", "/voice/launch", {"repo_id": "nope"}))
    rec("voice launch ok", call("POST", "/voice/launch", {"repo_id": "chatterbox"}))
    rec("asr launch bad", call("POST", "/asr/launch", {"model_id": "x"}))
    rec("asr launch ok", call("POST", "/asr/launch", {"model_id": "openai/whisper-small"}))
    rec("image launch bad", call("POST", "/image/launch", {"model_id": "x"}))
    rec("image launch ok", call("POST", "/image/launch", {"model_id": "black-forest-labs/FLUX.2-klein-4B"}))
    rec("unknown sidecar", call("GET", "/nope/status"))
    body = json.loads(call("GET", "/metrics")[1])
    out.append({"step": "metrics keys", "status": 200, "body": sorted(body)})
    out.append({"step": "metrics ram keys", "status": 200, "body": sorted(body["ram"])})
    return out


def serve(binary_cmd, env):
    # a daemon left over from the previous run would answer for this one
    end = time.time() + 20
    while not port_is_free() and time.time() < end:
        time.sleep(0.2)
    if not port_is_free():
        raise SystemExit("port 8001 still in use before a run")
    proc = subprocess.Popen(binary_cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + 30
    while time.time() < end:
        try:
            call("GET", "/status", token=None)
            return proc
        except OSError:
            time.sleep(0.2)
    raise SystemExit("the daemon did not come up")


def stop_daemon(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    proc.wait(timeout=30)
    subprocess.run(["pkill", "-f", str(HERE / "fakes" / "engine.sh")], check=False)
    subprocess.run(["pkill", "-f", str(HERE / "fakes" / "crash.sh")], check=False)
    end = time.time() + 20
    while not port_is_free() and time.time() < end:
        time.sleep(0.2)


# The journal line of a superseded process (its exit) lands before or after the
# next launch's clear, depending on which thread runs first: in runner.py too.
# It is left out of the comparison of the replacement step, both sides.
def settle(answers):
    for a in answers:
        if a["step"] == "logs after second":
            logs = json.loads(a["body"])["logs"]
            logs = [line for line in logs if not line.startswith("[runner] Process exited")]
            a["body"] = json.dumps({"logs": logs})
    return answers


def run(label, cmd):
    home = Path(tempfile.mkdtemp(prefix=f"parity-{label}-"))
    proc = serve(cmd, env_for(home))
    try:
        return settle(scenario())
    finally:
        stop_daemon(proc)


def port_is_free():
    import socket
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", PORT)) != 0
    finally:
        s.close()


def main(runner_py, rust_bin):
    # The production runner answers on this port: a run that reached it would
    # launch and stop the real model. Refused unless the port is free.
    if not port_is_free():
        raise SystemExit("port 8001 is in use: run this in an isolated namespace (unshare -n)")
    if os.environ.get("PARITY_ORDER") == "rs-first":
        rs = run("rs", [rust_bin])
        py = run("py", [sys.executable, runner_py])
    else:
        py = run("py", [sys.executable, runner_py])
        rs = run("rs", [rust_bin])
    bad = []
    for a, b in zip(py, rs):
        if a != b:
            bad.append((a["step"], a, b))
    print(f"{len(py)} steps, {len(bad)} differ")
    for step, a, b in bad[:12]:
        print(f"  {step}:\n    python {a['status']} {a['body'][:200]!r}\n    rust   {b['status']} {b['body'][:200]!r}")
    return 1 if bad or len(py) != len(rs) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
