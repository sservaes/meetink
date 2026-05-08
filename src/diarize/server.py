#!/usr/bin/env python3
"""Speaker identification sidecar for meetink.

Listens on 127.0.0.1:8179. The Swift capture binary POSTs ~10s WAV windows
to /identify; /profile add (in the REPL) POSTs 3 enrollment samples per
person to /enroll. Profiles persist as .npz files at $MEETINK_PROFILES_DIR
(default: ~/.meetink/profiles/<name>.npz).

Identification is two-stage:
  1. Compare to enrolled profiles. If top match clears THRESHOLD and beats
     the runner-up by MARGIN, return that name.
  2. Otherwise the embedding is "unknown" → group it into an in-memory
     cluster (online clustering) and return `THEM-A`, `THEM-B`, ... so the
     live transcript still distinguishes voices. After the meeting the user
     runs `/profile assign A Alice`, which converts the cluster to a real
     profile and lets the launcher rewrite past transcript lines.

Cluster state is per-session: cleared by POST /session/clear (which the
launcher calls on /start), and never persisted to disk. Lettering is
monotonic — assigning cluster A doesn't free "A" for the next unknown.

Accuracy
--------
- Multi-sample profiles: each name is a centroid of all enrollment samples
  (L2-normalised before averaging). Adding more samples sharpens the centroid
  rather than dilutes it.
- Threshold: cosine similarity ≥ THRESHOLD (default 0.65) is required to claim
  a profile match. Below that we cluster.
- Margin: the top profile match must beat the runner-up by ≥ MARGIN
  (default 0.07). If two profiles score similarly, we cluster instead of
  guessing — avoids the Bob-misidentified-as-Alex failure mode.
- CLUSTER_THRESHOLD (default 0.65): cosine similarity required to join an
  existing cluster vs. starting a new one.

Endpoints
---------
GET    /                              health + profile names + cluster count
GET    /profiles                      list profile names + sample counts
POST   /identify                      body=WAV → {speaker, confidence, runner_up?, cluster?}
POST   /enroll?name=<name>            body=WAV → appends one sample to that profile
DELETE /profiles/<name>               remove

GET    /session/clusters              list current clusters (letter, count)
POST   /session/clear                 reset clusters (called by /start)
POST   /session/assign?cluster=A&name=Alice
                                      promote cluster A to a real profile
POST   /session/merge?from=A&into=B   merge cluster A's samples into cluster B
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

MK_HOME = Path(os.environ.get("MEETINK_HOME", os.path.expanduser("~/.meetink")))
MODEL_PATH = Path(os.environ.get("MEETINK_DIARIZE_MODEL", MK_HOME / "models" / "speaker-embedding.onnx"))
PROFILES_DIR = Path(os.environ.get("MEETINK_PROFILES_DIR", MK_HOME / "profiles"))
PORT = int(os.environ.get("MEETINK_DIARIZE_PORT", "8179"))
THRESHOLD = float(os.environ.get("MEETINK_DIARIZE_THRESHOLD", "0.65"))
MARGIN = float(os.environ.get("MEETINK_DIARIZE_MARGIN", "0.07"))
# Higher than the profile-match THRESHOLD so we err on the side of *splitting*
# clusters (under-merging) rather than fusing two distinct speakers — false
# splits are easy to recover from with /profile merge, false merges aren't.
CLUSTER_THRESHOLD = float(os.environ.get("MEETINK_DIARIZE_CLUSTER_THRESHOLD", "0.72"))

PROFILES_DIR.mkdir(parents=True, exist_ok=True)

if not MODEL_PATH.exists():
    print(f"error: speaker-embedding model not found at {MODEL_PATH}", file=sys.stderr)
    print("  Run /diarize install or set MEETINK_DIARIZE_MODEL.", file=sys.stderr)
    sys.exit(1)

try:
    import sherpa_onnx
except ImportError:
    print("error: sherpa-onnx not installed in this venv", file=sys.stderr)
    print("  Run /diarize install.", file=sys.stderr)
    sys.exit(1)

print(f"loading model: {MODEL_PATH}", file=sys.stderr)
extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
    sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(MODEL_PATH),
        num_threads=2,
        debug=False,
        provider="coreml",  # Apple Silicon; falls back to CPU automatically
    )
)


# ---------------------------------------------------------------------------
# Profile storage: each profile is {"centroid": 1×D, "samples": N×D},
# samples are L2-normalised, centroid is the L2-normalised mean.
# ---------------------------------------------------------------------------


def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v if n == 0.0 else v / n


def _centroid(samples: np.ndarray) -> np.ndarray:
    return _l2(samples.mean(axis=0))


profiles: dict[str, dict] = {}


def _load_all() -> None:
    profiles.clear()
    # New format: .npz with `centroid` and `samples`
    for path in PROFILES_DIR.glob("*.npz"):
        try:
            data = np.load(path)
            profiles[path.stem] = {
                "centroid": data["centroid"].astype(np.float32),
                "samples": data["samples"].astype(np.float32),
            }
        except Exception as e:
            print(f"warning: failed to load profile {path}: {e}", file=sys.stderr)
    # Legacy format: .npy with a single embedding (treat as 1-sample profile)
    for path in PROFILES_DIR.glob("*.npy"):
        if path.stem in profiles:
            continue
        try:
            emb = _l2(np.load(path).astype(np.float32))
            profiles[path.stem] = {
                "centroid": emb,
                "samples": emb[np.newaxis, :],
            }
        except Exception as e:
            print(f"warning: failed to load legacy profile {path}: {e}", file=sys.stderr)


def _save(name: str) -> None:
    p = profiles[name]
    np.savez(PROFILES_DIR / f"{name}.npz", centroid=p["centroid"], samples=p["samples"])


def _add_sample(name: str, embedding: np.ndarray) -> int:
    """Append a sample to a profile (creating it if new). Returns total sample count."""
    new = _l2(embedding)[np.newaxis, :]
    if name in profiles:
        all_samples = np.vstack([profiles[name]["samples"], new])
    else:
        all_samples = new
    profiles[name] = {"centroid": _centroid(all_samples), "samples": all_samples}
    _save(name)
    return all_samples.shape[0]


_load_all()
print(
    f"loaded {len(profiles)} profile(s): "
    + (", ".join(f"{k}({v['samples'].shape[0]})" for k, v in profiles.items()) or "(none)"),
    file=sys.stderr,
)


# ---------------------------------------------------------------------------
# Audio + matching
# ---------------------------------------------------------------------------


def parse_wav(data: bytes) -> np.ndarray:
    """16-bit mono 16 kHz WAV → float32 [-1, 1]."""
    if len(data) < 44:
        raise ValueError("audio too short")
    return np.frombuffer(data[44:], dtype=np.int16).astype(np.float32) / 32768.0


def embed(samples: np.ndarray) -> np.ndarray:
    stream = extractor.create_stream()
    stream.accept_waveform(16000, samples)
    stream.input_finished()
    return np.asarray(extractor.compute(stream), dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def identify(emb: np.ndarray) -> dict:
    """Return {speaker, confidence, runner_up, runner_up_confidence}.

    `speaker` is None unless top match clears THRESHOLD AND beats runner-up
    by at least MARGIN. This biases toward "unknown" rather than guessing —
    avoiding the Bob-misidentified-as-Alex failure mode.
    """
    if not profiles:
        return {"speaker": None, "confidence": 0.0, "runner_up": None, "runner_up_confidence": 0.0}

    sims = sorted(
        ((name, cosine(emb, p["centroid"])) for name, p in profiles.items()),
        key=lambda kv: kv[1],
        reverse=True,
    )
    top_name, top_sim = sims[0]
    second_name, second_sim = (sims[1] if len(sims) > 1 else (None, -1.0))

    accepted = top_sim >= THRESHOLD and (top_sim - second_sim) >= MARGIN
    return {
        "speaker": top_name if accepted else None,
        "confidence": round(top_sim, 3),
        "runner_up": second_name,
        "runner_up_confidence": round(second_sim, 3) if second_sim > -1.0 else None,
    }


# ---------------------------------------------------------------------------
# Online clustering for unidentified embeddings
#
# When `identify()` doesn't match an enrolled profile, we keep the embedding
# in a per-session in-memory cluster pool. New embeddings join the closest
# existing cluster (cosine ≥ CLUSTER_THRESHOLD) or seed a new one. The cluster
# letter is what the live transcript shows: THEM-A, THEM-B, …
#
# Lettering is monotonic — never reused after a cluster is assigned/merged
# away. So the user's mental model "A was Alice" stays valid for the rest of
# the session even if cluster A gets converted to the Alice profile mid-meeting.
# ---------------------------------------------------------------------------

clusters: list[dict] = []  # each: {"letter": str, "centroid": 1×D, "samples": N×D}
_next_cluster_idx: int = 0


def _letter_for(idx: int) -> str:
    """0→A, 1→B, … 25→Z, 26→AA, 27→AB, … (monotonic, never reused)."""
    if idx < 26:
        return chr(ord("A") + idx)
    return _letter_for(idx // 26 - 1) + chr(ord("A") + idx % 26)


def _new_cluster(emb: np.ndarray) -> dict:
    global _next_cluster_idx
    letter = _letter_for(_next_cluster_idx)
    _next_cluster_idx += 1
    cluster = {
        "letter": letter,
        "centroid": _l2(emb),
        "samples": _l2(emb)[np.newaxis, :],
    }
    clusters.append(cluster)
    return cluster


def _cluster_or_create(emb: np.ndarray) -> tuple[str, float]:
    """Find closest cluster ≥ CLUSTER_THRESHOLD, else seed a new one.
    Returns (letter, similarity_to_chosen_cluster_centroid)."""
    if clusters:
        best = max(clusters, key=lambda c: cosine(emb, c["centroid"]))
        sim = cosine(emb, best["centroid"])
        if sim >= CLUSTER_THRESHOLD:
            new_samples = np.vstack([best["samples"], _l2(emb)[np.newaxis, :]])
            best["samples"] = new_samples
            best["centroid"] = _centroid(new_samples)
            return best["letter"], round(sim, 3)
    cluster = _new_cluster(emb)
    return cluster["letter"], 1.0  # similarity to itself


def _find_cluster(letter: str) -> dict | None:
    for c in clusters:
        if c["letter"] == letter:
            return c
    return None


def session_clear() -> None:
    """Reset clusters and the lettering counter. Called on /start."""
    global _next_cluster_idx
    clusters.clear()
    _next_cluster_idx = 0


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        return  # quiet — main.swift logs hits separately

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._json(200, {
                "status": "ok",
                "profiles": list(profiles.keys()),
                "threshold": THRESHOLD,
                "margin": MARGIN,
                "cluster_threshold": CLUSTER_THRESHOLD,
                "clusters": len(clusters),
            })
            return
        if path == "/profiles":
            self._json(200, {
                "profiles": [
                    {"name": n, "samples": int(p["samples"].shape[0])}
                    for n, p in profiles.items()
                ]
            })
            return
        if path == "/session/clusters":
            self._json(200, {
                "clusters": [
                    {"letter": c["letter"], "samples": int(c["samples"].shape[0])}
                    for c in clusters
                ]
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        url = urlparse(self.path)
        try:
            if url.path == "/identify":
                samples = parse_wav(body)
                if len(samples) < 16000:  # < 1 second is unreliable
                    self._json(200, {"speaker": None, "confidence": 0.0, "reason": "too_short"})
                    return
                emb = embed(samples)
                result = identify(emb)
                resp = dict(result)
                if resp["speaker"] is None:
                    # No profile match — assign to a cluster so the live
                    # transcript still distinguishes voices.
                    letter, sim = _cluster_or_create(emb)
                    resp["speaker"] = f"THEM-{letter}"
                    resp["cluster"] = letter
                    resp["cluster_confidence"] = sim
                self._json(200, resp)
                return
            if url.path == "/session/clear":
                session_clear()
                print("session: clusters cleared", file=sys.stderr)
                self._json(200, {"ok": True})
                return
            if url.path == "/session/assign":
                qs = parse_qs(url.query)
                letter = (qs.get("cluster", [""])[0]).strip().upper()
                name = (qs.get("name", [""])[0]).strip()
                if not letter or not name:
                    self._json(400, {"error": "need ?cluster=A&name=Alice"})
                    return
                if any(c in name for c in "/\\.."):
                    self._json(400, {"error": "invalid name (no slashes or dots)"})
                    return
                cluster = _find_cluster(letter)
                if cluster is None:
                    self._json(404, {"error": f"no cluster named {letter}"})
                    return
                # Promote the cluster's samples to a real profile. We
                # vstack onto any existing samples so re-assigning into an
                # existing name accumulates voice data rather than overwriting.
                new_samples = cluster["samples"]
                if name in profiles:
                    new_samples = np.vstack([profiles[name]["samples"], new_samples])
                profiles[name] = {
                    "centroid": _centroid(new_samples),
                    "samples": new_samples,
                }
                _save(name)
                clusters.remove(cluster)
                count = int(new_samples.shape[0])
                print(f"session: cluster {letter} → profile {name} ({count} samples)", file=sys.stderr)
                self._json(200, {
                    "ok": True,
                    "cluster": letter,
                    "name": name,
                    "samples": count,
                })
                return
            if url.path == "/session/merge":
                qs = parse_qs(url.query)
                src_letter = (qs.get("from", [""])[0]).strip().upper()
                dst_letter = (qs.get("into", [""])[0]).strip().upper()
                if not src_letter or not dst_letter:
                    self._json(400, {"error": "need ?from=A&into=B"})
                    return
                if src_letter == dst_letter:
                    self._json(400, {"error": "from and into must differ"})
                    return
                src = _find_cluster(src_letter)
                dst = _find_cluster(dst_letter)
                if src is None or dst is None:
                    self._json(404, {"error": "unknown cluster letter"})
                    return
                merged = np.vstack([dst["samples"], src["samples"]])
                dst["samples"] = merged
                dst["centroid"] = _centroid(merged)
                clusters.remove(src)
                count = int(merged.shape[0])
                print(f"session: cluster {src_letter} merged into {dst_letter} ({count} samples)", file=sys.stderr)
                self._json(200, {
                    "ok": True,
                    "from": src_letter,
                    "into": dst_letter,
                    "samples": count,
                })
                return
            if url.path == "/enroll":
                qs = parse_qs(url.query)
                name = (qs.get("name", [""])[0]).strip()
                if not name:
                    self._json(400, {"error": "missing ?name=..."})
                    return
                if any(c in name for c in "/\\.."):
                    self._json(400, {"error": "invalid name (no slashes or dots)"})
                    return
                samples = parse_wav(body)
                if len(samples) < 16000 * 3:
                    self._json(400, {"error": "need >= 3s of audio"})
                    return
                count = _add_sample(name, embed(samples))
                print(f"enrolled: {name} ({len(samples) / 16000:.1f}s, total={count})", file=sys.stderr)
                self._json(200, {"ok": True, "name": name, "samples": count})
                return
            self._json(404, {"error": "not found"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_DELETE(self) -> None:
        url = urlparse(self.path)
        if url.path.startswith("/profiles/"):
            name = url.path[len("/profiles/"):].strip()
            removed = False
            for ext in (".npz", ".npy"):
                p = PROFILES_DIR / f"{name}{ext}"
                if p.exists():
                    p.unlink()
                    removed = True
            if removed:
                profiles.pop(name, None)
                print(f"removed: {name}", file=sys.stderr)
                self._json(200, {"ok": True, "removed": name})
            else:
                self._json(404, {"error": f"no profile named {name}"})
            return
        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"diarize-server ready on 127.0.0.1:{PORT} (threshold={THRESHOLD}, margin={MARGIN})", file=sys.stderr)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
