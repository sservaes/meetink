#!/usr/bin/env python3
"""Download an mlx-community model snapshot into ~/.meetink/models/mlx/<name>/.

Uses huggingface_hub's snapshot_download — the same machinery mlx_lm.load()
uses internally — but with an explicit local_dir so the files end up where
the rest of meetink keeps state (rather than the opaque HF cache under
~/.cache/huggingface). Lets `/llm rm` work via plain rm -rf, and lets users
audit models with `du` / Finder.

Args:
  --repo      HF repo id, e.g. "mlx-community/Qwen3.5-2B-4bit"
  --target    Destination directory (absolute path)

Streams progress to stderr (huggingface_hub's tqdm bar) so the launcher's
mk_download_with_progress isn't needed for this — the Python progress bar
is more accurate (it shows total across all the .safetensors shards).
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("mlx_download: huggingface_hub not installed. Run: meetink setup",
              file=sys.stderr)
        return 1

    try:
        snapshot_download(
            repo_id=args.repo,
            local_dir=args.target,
            # Skip large optional artefacts: original weights, eval logs, etc.
            # Keep:
            #   .safetensors  — model weights
            #   .json         — config.json, tokenizer.json, etc.
            #   tokenizer*    — tokenizer.model on some Llama-derived archs
            #   .txt          — vocab files for older tokenizers
            #   .jinja        — chat_template.jinja (HF moved chat templates
            #                   out of tokenizer_config.json into a sibling
            #                   .jinja file in 2025; transformers >=4.43
            #                   auto-loads it). Without this, the tokenizer
            #                   has no template and apply_chat_template
            #                   raises ValueError.
            allow_patterns=[
                "*.safetensors",
                "*.json",
                "tokenizer*",
                "*.txt",
                "*.jinja",
            ],
        )
    except Exception as e:
        print(f"mlx_download: failed: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
