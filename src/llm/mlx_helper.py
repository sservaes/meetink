#!/usr/bin/env python3
"""Single-shot MLX text generation — drop-in replacement for the
`llama-completion` shell-out path.

Used by titling.sh (_generate_title_local) and ask.sh (_ask_local) for the
backend=local code path. The REPL also calls this directly when running
out-of-process (pre-Task #4 / fallback).

Why MLX over llama.cpp on Apple Silicon: native Metal + ANE integration,
unified-memory aware, ~30-60% faster generation for the same quant + size,
better streaming. Downside is a Python dep instead of a C++ binary, but we
already need a Python venv for the REPL so it's free.

Args:
  --model       HF id (e.g. "mlx-community/Qwen3.5-2B-4bit") OR a local
                directory containing the model snapshot. We prefer local
                directories so the offline path is well-defined; the
                downloader (cmd_llm download) snapshots each model into
                ~/.meetink/models/mlx/<name>/.
  --prompt      The user prompt. We always wrap it in the model's chat
                template via tokenizer.apply_chat_template so callers don't
                have to know whether the model is Qwen, Llama, etc.
  --system      Optional system prompt. Goes through the same chat template.
  --max-tokens  Cap on generated tokens. titling uses ~30; /ask uses ~512.
  --temp        Sampling temperature. titling uses 0.3; /ask uses 0.4.

Exit codes:
  0  on success (generation printed to stdout)
  1  on missing dep (mlx-lm not installed)
  2  on model load failure
  3  on generation failure
"""

from __future__ import annotations

import argparse
import sys


def _die(code: int, msg: str) -> None:
    print(f"mlx_helper: {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--system", default="")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temp", type=float, default=0.4)
    args = parser.parse_args()

    try:
        from mlx_lm import load, generate
        from mlx_lm.sample_utils import make_sampler
    except ImportError:
        _die(1, "mlx-lm not installed in this venv. Run: meetink setup")

    try:
        model, tokenizer = load(args.model)
    except Exception as e:
        _die(2, f"model load failed: {e}")

    # Chat-template wrapping. Qwen3.5-Instruct supports an optional system
    # prompt; if --system is empty we still pass the messages list so the
    # tokenizer's default system prompt (if any) is used.
    messages = []
    if args.system:
        messages.append({"role": "system", "content": args.system})
    messages.append({"role": "user", "content": args.prompt})

    try:
        # enable_thinking=False suppresses Qwen3.5's <think>...</think>
        # reasoning preamble so we get the answer directly. Older Qwen3
        # templates without this flag harmlessly ignore the kwarg.
        prompt_text = tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=False,
            enable_thinking=False,
        )
    except Exception as e:
        _die(3, f"chat template failed: {e}")

    try:
        sampler = make_sampler(temp=args.temp)
        out = generate(
            model, tokenizer,
            prompt=prompt_text,
            max_tokens=args.max_tokens,
            sampler=sampler,
            verbose=False,
        )
    except Exception as e:
        _die(3, f"generation failed: {e}")

    # `out` is the full generation including any model-emitted special tokens.
    # Qwen3.5 chat templates terminate cleanly so we just print verbatim;
    # callers (titling.sh, ask.sh) do their own post-filtering for stub
    # <think>…</think> blocks etc.
    print(out, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
