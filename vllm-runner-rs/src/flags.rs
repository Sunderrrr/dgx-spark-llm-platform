//! The allow-lists of the runner's arguments, as the former runner.py defined them
//! (frozen copy: the Python runner is retired, this file is now edited by hand).

pub static BOOL_FLAGS: &[&str] = &[
    "--cute-dsl-arch-sm121a",
    "--disable-log-requests",
    "--disable-log-stats",
    "--enable-auto-tool-choice",
    "--enable-mamba-cache-stochastic-rounding",
    "--enable-mamba-shared-prefix-checkpoint",
    "--enforce-eager",
    "--force-fp8-marlin",
    "--language-model-only",
    "--skip-mm-profiling",
    "--vllm-032",
];

pub static VALUE_FLAGS: &[&str] = &[
    "--attention-backend",
    "--block-size",
    "--default-chat-template-kwargs",
    "--dtype",
    "--gpu-memory-utilization",
    "--kv-cache-dtype",
    "--limit-mm-per-prompt",
    "--mamba-backend",
    "--mamba-cache-philox-rounds",
    "--mamba-ssm-cache-dtype",
    "--max-model-len",
    "--max-num-batched-tokens",
    "--max-num-seqs",
    "--pipeline-parallel-size",
    "--prefix-match-unit",
    "--quantization",
    "--reasoning-parser",
    "--speculative-config",
    "--speculative_config.method",
    "--speculative_config.model",
    "--speculative_config.num_speculative_tokens",
    "--swap-space",
    "--tensor-parallel-size",
    "--tokenizer-mode",
    "--tool-call-parser",
    "--uvicorn-log-level",
    "--video-pruning-rate",
];

pub static ENV_FLAGS: &[&str] = &[
    "--cute-dsl-arch-sm121a",
    "--force-fp8-marlin",
];

pub static VLLM_BIN_FLAGS: &[&str] = &[
    "--vllm-032",
];

pub static BIN_FLAGS: &[&str] = &[
    "--llama-v080",
    "--vllm-032",
];

pub static LLAMA_BOOL_FLAGS: &[&str] = &[
    "--cont-batching",
    "--context-shift",
    "--jinja",
    "--kv-unified",
    "--llama-v080",
    "--metrics",
    "--mlock",
    "--no-context-shift",
    "--no-kv-offload",
    "--no-kv-unified",
    "--no-mmap",
    "--no-warmup",
];

pub static LLAMA_VALUE_FLAGS: &[&str] = &[
    "--batch-size",
    "--cache-type-k",
    "--cache-type-v",
    "--chat-template-file",
    "--chat-template-kwargs",
    "--ctx-size",
    "--defrag-thold",
    "--flash-attn",
    "--kv-unified-per-slot",
    "--lazy-mode",
    "--load-mode",
    "--log-verbosity",
    "--main-gpu",
    "--mmproj",
    "--n-gpu-layers",
    "--n-predict",
    "--override-tensor",
    "--parallel",
    "--reasoning-format",
    "--rope-freq-base",
    "--rope-freq-scale",
    "--rope-scaling",
    "--seed",
    "--split-mode",
    "--threads",
    "--threads-batch",
    "--ubatch-size",
];

pub static DS4_BOOL_FLAGS: &[&str] = &[
    "--cpu",
    "--cuda",
    "--disable-exact-dsml-tool-replay",
    "--kv-cache-reject-different-quant",
];

pub static DS4_VALUE_FLAGS: &[&str] = &[
    "--backend",
    "--ctx",
    "--kv-cache-boundary-align-tokens",
    "--kv-cache-boundary-trim-tokens",
    "--kv-cache-cold-max-tokens",
    "--kv-cache-continued-interval-tokens",
    "--kv-cache-min-tokens",
    "--kv-disk-space-mb",
];

pub static OCR_BOOL_FLAGS: &[&str] = &[
    "--cute-dsl-arch-sm121a",
    "--disable-log-requests",
    "--disable-log-stats",
    "--enable-auto-tool-choice",
    "--enforce-eager",
    "--force-fp8-marlin",
    "--language-model-only",
    "--no-enable-prefix-caching",
    "--skip-mm-profiling",
    "--trust-remote-code",
    "--vllm-032",
];

pub static OCR_VALUE_FLAGS: &[&str] = &[
    "--attention-backend",
    "--block-size",
    "--default-chat-template-kwargs",
    "--dtype",
    "--gpu-memory-utilization",
    "--kv-cache-dtype",
    "--limit-mm-per-prompt",
    "--logits_processors",
    "--max-model-len",
    "--max-num-batched-tokens",
    "--max-num-seqs",
    "--mm-processor-cache-gb",
    "--pipeline-parallel-size",
    "--quantization",
    "--reasoning-parser",
    "--speculative-config",
    "--swap-space",
    "--tensor-parallel-size",
    "--tokenizer-mode",
    "--tool-call-parser",
    "--uvicorn-log-level",
];

pub static EXL3_BOOL_FLAGS: &[&str] = &[
];

pub static EXL3_VALUE_FLAGS: &[&str] = &[
    "--cache-mode",
    "--cache-size",
    "--chunk-size",
    "--draft-cache-mode",
    "--draft-mode",
    "--draft-model-name",
    "--draft-num-tokens",
    "--gpu-split-auto",
    "--max-batch-size",
    "--max-seq-len",
    "--model-name",
    "--reasoning",
    "--vision",
];

pub static TRUST_RC_MODELS: &[&str] = &[
    "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4",
];

/// (flag, env var, value)
pub static ENV_FLAG_VARS: &[(&str, &str, &str)] = &[
    ("--cute-dsl-arch-sm121a", "CUTE_DSL_ARCH", "sm_121a"),
    ("--force-fp8-marlin", "VLLM_TEST_FORCE_FP8_MARLIN", "1"),
];

/// (flag, env var, default binary relative to the runner's HOME)
pub static BIN_FLAG_PATHS: &[(&str, &str, &str)] = &[
    // Upgrades of 2026-10-29, kept ALONGSIDE the old builds: opting in is a
    // per-launch flag, so a regression costs one relaunch, not a rebuild.
    ("--llama-v080", "LLAMA_BIN_V080", "llama-cpp-v080/build/bin/llama-server"),
    ("--vllm-032", "VLLM_BIN_032", "venvs/vllm-032/bin/vllm"),
];

