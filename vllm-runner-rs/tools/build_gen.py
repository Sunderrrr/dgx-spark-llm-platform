#!/usr/bin/env python3
"""Fixtures for the command construction (runner.py's _build_cmd and its
resolvers), with a fake model tree.

    python3 tools/build_gen.py <path-to-vllm-runner/runner.py>

tests/fixtures/tree/ is the committed tree (small files, no run state). The
expected answers come from runner.py run on a COPY of that tree, in a scratch
directory; the copy's path is written as @ROOT@ so the Rust test can rebuild
the same layout anywhere.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
TREE = HERE / "tests" / "fixtures" / "tree"


def make_tree(root: Path):
    """The fake layout: weights of two sizes in the HF cache, a split GGUF with
    its projector, a template, an exllamav3 model with a drafter, an escape link."""
    def f(p: Path, n: int):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * n)
    m = root / "models"
    f(m / "local-split" / "snapshots" / "s1" / "model-00001-of-00002.gguf", 300)
    f(m / "local-split" / "snapshots" / "s1" / "model-00002-of-00002.gguf", 200)
    f(m / "local-split" / "snapshots" / "s1" / "mmproj-f16.gguf", 50)
    f(m / "local-single" / "snap" / "single.gguf", 120)
    f(m / "local-single" / "snap" / "single-imatrix.gguf", 999)
    f(root / "templates" / "mistral.jinja", 40)
    (m / "exl3-model" / "main-model").mkdir(parents=True, exist_ok=True)
    (m / "exl3-model" / "draft-model").mkdir(parents=True, exist_ok=True)
    hub = root / "hf" / "hub"
    f(hub / "models--org--small" / "snapshots" / "abc" / "q4.gguf", 80)
    f(hub / "models--org--small" / "snapshots" / "abc" / "q8.gguf", 160)
    f(hub / "models--org--big" / "snapshots" / "def" / "big.gguf", 400)
    f(hub / "models--org--big" / "snapshots" / "def" / "big-00001-of-00003.gguf", 10)
    f(hub / "models--org--nogguf" / "snapshots" / "x" / "config.json", 5)
    os.symlink("..", m / "escape")
    os.symlink("../../../../../etc", m / "local-escape-target")


def copy_tree(src: Path, dst: Path):
    for dirpath, dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for d in dirnames:
            p = Path(dirpath) / d
            if p.is_symlink():
                os.symlink(os.readlink(p), dst / rel / d)
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.is_symlink():
                os.symlink(os.readlink(p), dst / rel / fn)
            else:
                shutil.copy2(p, dst / rel / fn)


CASES = []
ENGINES = ["vllm", "llamacpp", "ds4", "exllamav3"]
HF = ["local:local-split", "local:local-single", "local:escape", "local:nope", "local:..",
      "local:exl3-model", "org/small", "org/big", "org/nogguf", "org/missing", "@bad"]
ARGS = ["", "--max-model-len 4096", "--chat-template-file mistral.jinja",
        "--chat-template-file ../etc/passwd", "--chat-template-file nope.jinja",
        "--mmproj mmproj-f16.gguf", "--mmproj other.gguf", "--mmproj ../x.gguf",
        "--cpu", "--cuda --n-predict 9", "--model-name main-model",
        "--model-name main-model --draft-model-name draft-model", "--model-name ../x",
        "--model-name nope", "--draft-model-name draft-model",
        "--chat-template-file", "--ctx-size 32768 --jinja", "--parallel 4 --flash-attn"]


def main(runner_path):
    scratch = Path(tempfile.mkdtemp(prefix="build-gen-"))
    shutil.rmtree(TREE, ignore_errors=True)
    make_tree(TREE)
    # The exllamav3 run dir is state: never committed.
    if (TREE / "home").exists():
        shutil.rmtree(TREE / "home")
    run = scratch / "tree"
    make_tree(run)
    os.environ["RUNNER_TOKEN"] = "gen-only-token-0123456789"
    os.environ["MODELS_DIR"] = str(run / "models")
    os.environ["HF_HOME"] = str(run / "hf")
    os.environ["TEMPLATES_DIR"] = str(run / "templates")
    os.environ["HOME"] = str(run / "home")
    os.environ["TABBY_DIR"] = str(run / "tabby")
    os.environ["RUNNER_LOG_DIR"] = str(run / "logs")
    sys.path.insert(0, os.path.dirname(os.path.abspath(runner_path)))
    import importlib.util
    spec = importlib.util.spec_from_file_location("runner_build", runner_path)
    r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r)
    cases = []
    for eng in ENGINES:
        for hf in HF:
            for a in ARGS:
                for override in (None, "/opt/llama-next/llama-server"):
                    name = "catalog-name"
                    try:
                        cmd = r._build_cmd(hf, name, a.split(), eng, override)
                        res = {"ok": True, "cmd": cmd}
                    except Exception as e:  # runner.py's own refusal or failure
                        res = {"ok": False, "error": str(e), "kind": type(e).__name__}
                    cases.append({"engine": eng, "hf_id": hf, "name": name, "args": a,
                                  "bin": override, **res})
    text = json.dumps(cases, ensure_ascii=False, indent=1)
    text = text.replace(str(run), "@ROOT@")
    (HERE / "tests" / "fixtures" / "build_cmd.json").write_text(text)
    print(f"{len(cases)} command cases")


if __name__ == "__main__":
    main(sys.argv[1])
