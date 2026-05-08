#!/usr/bin/env python3
"""Convert any document to markdown + (optionally) summarize it for /context.

Used by src/lib/context.sh as a multi-subcommand CLI. Three jobs:

  convert <file>    Convert a PDF/DOCX/XLSX/PPTX/etc. to markdown via the
                    Microsoft `markitdown` package. Writes the result with
                    YAML frontmatter (source, converter, converted_at).

  summarize <md>    Generate a ~500-token structured summary of the
                    converted markdown via the active backend (local
                    mlx-lm or claude). Same YAML frontmatter shape as
                    meeting summaries (generated_by, generated_at).

  tokens <text>     Count tokens of <text> using the active local model's
                    tokenizer (loads only the tokenizer, not the weights —
                    fast, ~200ms). Prints the integer to stdout. Falls
                    back to a rough chars/4 estimate if the tokenizer
                    can't be loaded.

We use markitdown rather than per-format libraries because:
  - Single dep covers PDF, DOCX, XLSX, PPTX, HTML, EPUB, images (OCR)
  - Output is consistent markdown ready for LLM consumption
  - Microsoft maintains it, regular updates
The trade-off is install size (~80 MB with the format extras), which is
acceptable next to mlx-lm's footprint.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def _die(code: int, msg: str) -> None:
    print(f"context_helper: {msg}", file=sys.stderr)
    sys.exit(code)


def _frontmatter(d: dict) -> str:
    """Render a dict as a minimal YAML frontmatter block. Only string and
    int/float scalars; we don't need the full YAML spec for this."""
    lines = ["---"]
    for k, v in d.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


def _strip_frontmatter(text: str) -> str:
    """Return `text` with a leading YAML frontmatter block removed (if any).
    Mirrors the convention used for summaries elsewhere in the codebase."""
    if not text.startswith("---\n"):
        return text
    rest = text[4:]
    end = rest.find("\n---\n")
    if end == -1:
        return text
    return rest[end + 5:]


# ---------------------------------------------------------------------------
# convert
# ---------------------------------------------------------------------------

def cmd_convert(args) -> int:
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        _die(2, f"file not found: {src}")

    # Plain text / markdown short-circuit: markitdown's output for these
    # is essentially the file's contents (modulo whitespace), so we skip
    # the dep + process altogether. Lets users attach .md/.txt without
    # paying the ~80 MB markitdown install on first /context add.
    ext = src.suffix.lower()
    if ext in (".md", ".markdown", ".txt"):
        try:
            body = src.read_text(encoding="utf-8")
        except OSError as e:
            _die(3, f"read failed: {e}")
        # Drop any existing leading frontmatter — we'll write our own.
        body = _strip_frontmatter(body).strip()
        if not body:
            _die(4, "file is empty")
        converter_version = "passthrough"
    else:
        try:
            from markitdown import MarkItDown
        except ImportError:
            _die(1, "markitdown not installed. Run: /context add will install it on first use.")

        md = MarkItDown(enable_plugins=False)
        try:
            result = md.convert(str(src))
        except Exception as e:
            _die(3, f"conversion failed: {e}")

        body = (result.text_content or "").strip()
        if not body:
            _die(4, "conversion produced no text — file may be image-only PDF without OCR enabled")

        converter_version = "markitdown"
        try:
            # markitdown exposes __version__ on the package on recent releases.
            import markitdown as _m
            converter_version = f"markitdown {getattr(_m, '__version__', '')}".strip()
        except Exception:
            pass

    front = _frontmatter({
        "source": src.name,
        "source_size_bytes": src.stat().st_size,
        "converted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "converter": converter_version,
    })

    if args.output == "-":
        print(front)
        print()
        print(body)
    else:
        out = Path(args.output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"{front}\n\n{body}\n")
    return 0


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM = """You produce a tight summary of a document so an assistant can recall its key points later. Output ONLY the four sections below, in this exact order, using markdown bullet lists. No preamble, no closing remarks, no headings above the sections.

## Purpose
- 1-2 bullets: what kind of document this is and what it's about.

## Key facts
- 4-8 bullets capturing the concrete claims, numbers, names, and decisions in the document. Prefer specifics over abstractions.

## Important quotes
- Up to 3 short verbatim excerpts that crystallize the document's core. Skip if none stand out.

## Open threads
- Things the document raises but doesn't resolve. Skip if everything is settled."""


def cmd_summarize(args) -> int:
    src = Path(args.input).expanduser().resolve()
    if not src.is_file():
        _die(2, f"file not found: {src}")
    body = _strip_frontmatter(src.read_text())
    if not body.strip():
        _die(4, "input is empty after stripping frontmatter")

    backend = args.backend
    model = args.model
    if backend == "local":
        # In-process via mlx_lm — model singleton may already be loaded by
        # the REPL, sharing it saves a cold start.
        try:
            from llm.mlx_runtime import get_runtime
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from llm.mlx_runtime import get_runtime
        runtime = get_runtime()
        try:
            out = runtime.generate(
                model_path=Path(args.model_path),
                prompt=body,
                system=_SUMMARY_SYSTEM,
                max_tokens=600,
                temp=0.3,
            )
        except Exception as e:
            _die(3, f"summarization failed: {e}")
    elif backend == "claude":
        full_prompt = f"{_SUMMARY_SYSTEM}\n\nDocument:\n{body}"
        try:
            res = subprocess.run(
                ["claude", "-p",
                 "--model", model,
                 "--tools", "",
                 "--strict-mcp-config",
                 full_prompt],
                capture_output=True, text=True, check=False,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            _die(1, "claude CLI not found")
        if res.returncode != 0:
            _die(3, f"claude failed: {res.stderr.strip() or 'no stderr'}")
        out = res.stdout
    else:
        _die(2, f"unknown backend: {backend}")

    # Strip any <think> stub the model may have emitted (insurance).
    cleaned: list[str] = []
    in_think = False
    for line in out.splitlines():
        if "<think>" in line:
            in_think = True
            continue
        if "</think>" in line:
            in_think = False
            continue
        if not in_think:
            cleaned.append(line)
    summary_body = "\n".join(cleaned).strip()

    front = _frontmatter({
        "generated_by": model,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_doc": src.name,
    })
    if args.output == "-":
        print(front)
        print()
        print(summary_body)
    else:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f"{front}\n\n{summary_body}\n")
    return 0


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

def cmd_tokens(args) -> int:
    """Count tokens of a file's content using the active local model's
    tokenizer. Loads only the tokenizer (fast). Falls back to chars/4 if
    the tokenizer can't be loaded — useful for /context list when the
    model snapshot isn't on disk yet."""
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        print(0)
        return 0
    text = src.read_text()
    text = _strip_frontmatter(text)

    if args.model_path:
        try:
            from transformers import AutoTokenizer
            tok = AutoTokenizer.from_pretrained(args.model_path)
            print(len(tok.encode(text)))
            return 0
        except Exception:
            pass
    # Fallback: rough chars/4 estimate (consistent with how OpenAI rule-of-
    # thumb counts English tokens; accurate enough for budget UX).
    print(len(text) // 4)
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_conv = sub.add_parser("convert")
    p_conv.add_argument("file")
    p_conv.add_argument("--output", default="-",
                        help="output path; '-' prints to stdout (default)")
    p_conv.set_defaults(fn=cmd_convert)

    p_sum = sub.add_parser("summarize")
    p_sum.add_argument("input", help="path to converted .md file")
    p_sum.add_argument("--output", default="-")
    p_sum.add_argument("--backend", choices=["local", "claude"], required=True)
    p_sum.add_argument("--model", required=True,
                       help="model identifier for frontmatter (e.g., qwen3.5-2b, claude-sonnet-4-6)")
    p_sum.add_argument("--model-path", default="",
                       help="MLX snapshot dir (required for backend=local)")
    p_sum.set_defaults(fn=cmd_summarize)

    p_tok = sub.add_parser("tokens")
    p_tok.add_argument("file")
    p_tok.add_argument("--model-path", default="",
                       help="MLX snapshot dir for the tokenizer; falls back to chars/4")
    p_tok.set_defaults(fn=cmd_tokens)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
