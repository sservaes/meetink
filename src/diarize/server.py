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
POST   /profiles/<name>/pop?count=N   drop last N samples, recompute centroid
                                      (count defaults to 1; refuses if it
                                      would empty the profile — use DELETE
                                      for that)

GET    /session/clusters              list current clusters (letter, count)
POST   /session/clear                 reset clusters (called by /start)
POST   /session/assign?cluster=A&name=Alice
                                      promote cluster A to a real profile
POST   /session/merge?from=A&into=B   merge cluster A's samples into cluster B
POST   /session/rename?from=bob&to=flavio
                                      rename a profile, OR fold its samples
                                      into an existing profile if `to` already
                                      exists (used to fix split identities like
                                      bob/flavio being the same person)
GET    /session/sensitivity           current threshold/margin/cluster_threshold
POST   /session/sensitivity?mode=focused|default|strict
                                      apply a preset; takes effect on next
                                      /identify, no restart required
GET    /session/auto-train            current auto-train settings
POST   /session/auto-train?enabled=true|false&floor=0.88
                                      &margin_multiplier=2.0&min_samples=5
                                      tweak any subset; high-confidence
                                      /identify matches fold back into the
                                      profile when guardrails all pass
GET    /session/whitelist             current per-session profile whitelist
POST   /session/whitelist?profiles=alex,stacey
                                      restrict /identify to a subset of
                                      profiles (others won't match → cluster
                                      as THEM-X). Eliminates the "Mike's
                                      voice scores 0.89 against ALEX" risk
                                      when going into a meeting with people
                                      who aren't all enrolled.
POST   /session/whitelist?clear=true  drop the whitelist (match all profiles)
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

MK_HOME = Path(os.environ.get("MEETINK_HOME", os.path.expanduser("~/.meetink")))
MODEL_PATH = Path(os.environ.get("MEETINK_DIARIZE_MODEL", MK_HOME / "models" / "speaker-embedding.onnx"))
PROFILES_DIR = Path(os.environ.get("MEETINK_PROFILES_DIR", MK_HOME / "profiles"))
PORT = int(os.environ.get("MEETINK_DIARIZE_PORT", "8179"))

# --- Sensitivity presets ----------------------------------------------------
#
# Three knobs control how the server hands out names:
#   threshold         — cosine ≥ this is required to claim a profile match
#   margin            — top profile must beat runner-up by ≥ this
#   cluster_threshold — cosine ≥ this is required to join an existing cluster
#
# Different meetings want different bias. The presets below are sized
# around the failure modes we've seen, not just nudges to the defaults.
#
# focused  — 1:1s and small meetings with familiar speakers.
#            Wide MARGIN guards against bob-vs-flavio confusion when two
#            enrolled profiles sit close in voice space (the common
#            failure: Flavio scoring 0.66 against BOB and 0.62 against
#            FLAVIO, false-naming as BOB). Low CLUSTER_THRESHOLD keeps
#            an unmatched speaker as one cluster instead of splintering
#            into THEM-A/THEM-B/THEM-C across the call.
#
# default  — what shipped before sensitivity was a runtime knob. Kept as
#            a baseline for backwards compatibility, not because it's
#            the universal best.
#
# strict   — large meetings with strangers. Higher THRESHOLD avoids
#            misnaming an unknown speaker as someone enrolled. Higher
#            CLUSTER_THRESHOLD preserves distinct voices as distinct
#            clusters even when they're tonally similar.
PRESETS: dict[str, dict[str, float]] = {
    "focused": {"threshold": 0.62, "margin": 0.12, "cluster_threshold": 0.55},
    "default": {"threshold": 0.65, "margin": 0.07, "cluster_threshold": 0.72},
    "strict":  {"threshold": 0.70, "margin": 0.10, "cluster_threshold": 0.78},
}

# Live settings — every code path reads these via dict lookup so a POST
# /session/sensitivity update takes effect on the very next /identify
# without a server restart. Env-var overrides at boot still win on the
# initial read; presets are applied on top of them when chosen.
settings: dict[str, float] = {
    "threshold": float(os.environ.get(
        "MEETINK_DIARIZE_THRESHOLD",
        str(PRESETS["default"]["threshold"]),
    )),
    "margin": float(os.environ.get(
        "MEETINK_DIARIZE_MARGIN",
        str(PRESETS["default"]["margin"]),
    )),
    "cluster_threshold": float(os.environ.get(
        "MEETINK_DIARIZE_CLUSTER_THRESHOLD",
        str(PRESETS["default"]["cluster_threshold"]),
    )),
}
# Track which preset (if any) the user explicitly selected, so GET
# /session/sensitivity can echo "focused" instead of always "custom".
settings_preset: str = os.environ.get("MEETINK_DIARIZE_PRESET", "default")


# --- Auto-train ------------------------------------------------------------
#
# When /identify scores well above noise against an enrolled profile, fold
# the embedding back into that profile's samples. Continuous self-improvement
# from real conversational audio without manual /profile train calls.
#
# Three guardrails to prevent the failure mode that bit FLAVIO earlier
# today (pollution from a wrong-but-confident match):
#
#   1. Confidence floor — match cosine must clear `floor` (much higher
#      than the matching THRESHOLD). 0.88 by default vs the matching
#      THRESHOLD of 0.62-0.70.
#   2. Margin multiplier — top match must beat runner-up by at least
#      `margin_multiplier` × the current MARGIN. So in default mode
#      (MARGIN=0.07) we require 0.14+ separation; in focused mode
#      (MARGIN=0.12) we require 0.24+. Either way, two-profiles-tied
#      situations don't auto-train.
#   3. Min samples — profiles with very few samples (cold start) skip
#      auto-train; one bad sample on a 3-sample profile shifts the
#      centroid 25%, which is too risky.
#
# Anything that does land in the profile is plain append-via-_add_sample,
# so /profile undo <name> N peels recent auto-additions cleanly. Server
# also stderr-logs every auto-add so /diarize log surfaces them.
auto_train_settings: dict = {
    "enabled": os.environ.get(
        "MEETINK_AUTO_TRAIN", "true",
    ).lower() in ("1", "true", "yes", "on"),
    "floor": float(os.environ.get("MEETINK_AUTO_TRAIN_FLOOR", "0.88")),
    "margin_multiplier": float(
        os.environ.get("MEETINK_AUTO_TRAIN_MARGIN_MULT", "2.0"),
    ),
    "min_samples": int(
        os.environ.get("MEETINK_AUTO_TRAIN_MIN_SAMPLES", "5"),
    ),
}


# --- Profile representation tuning ----------------------------------------
#
# Three changes from the original "single centroid + uniform mean" model
# that the user hit pollution issues with:
#
#   1. Outlier rejection. New samples must score ≥ OUTLIER_FLOOR against
#      at least one existing centroid before being accepted. Catches the
#      common failure modes: a different speaker briefly bleeding into a
#      /profile train sample, or a /session/assign / /session/rename fold
#      where the user mistakenly identifies two distinct speakers as one.
#   2. k-means per profile. A single centroid can't represent multimodal
#      voice (calm vs excited, headset vs laptop mic, etc.) — averaging
#      them lands you in the middle of voice space, fitting neither
#      mode. With up to MAX_CENTROIDS centroids per profile, matching
#      scores against the *best* mode, not the mean of all modes.
#   3. Time decay. Old samples are weighted exponentially less in the
#      centroid computation, so the profile tracks the speaker's
#      *current* voice characteristics (new headset, recovered from cold,
#      etc.). Default TAU=180 days is conservative — recent samples
#      dominate but old ones still meaningfully contribute.
#
# All three are conservative defaults; can be disabled / loosened via
# env vars without code changes.
PROFILE_MAX_CENTROIDS = int(os.environ.get("MEETINK_PROFILE_MAX_CENTROIDS", "3"))
PROFILE_SAMPLES_PER_CENTROID = int(
    os.environ.get("MEETINK_PROFILE_SAMPLES_PER_CENTROID", "10")
)
PROFILE_OUTLIER_FLOOR = float(
    os.environ.get("MEETINK_PROFILE_OUTLIER_FLOOR", "0.40")
)
# Time decay TAU in seconds. 0 = disabled (uniform weights). Default
# 180 days: a sample from TAU seconds ago has weight 1/e ≈ 0.37.
PROFILE_TIME_DECAY_TAU_S = float(
    os.environ.get(
        "MEETINK_PROFILE_TIME_DECAY_TAU_S",
        str(180 * 24 * 3600),
    )
)
# Cap k-means iterations. Spherical k-means on ~100 samples × 3 centroids
# converges in 5-10 iters; 20 is plenty of headroom.
_KMEANS_MAX_ITERS = 20


# --- Session whitelist ----------------------------------------------------
#
# When set, /identify only considers this subset of profiles. Voices that
# would otherwise have matched a profile outside the whitelist fall through
# to clustering (THEM-X) — which is exactly what you want when you go into
# a meeting with people who aren't all enrolled. Auto-train naturally
# inherits the restriction (it operates on identify's output).
#
# None = no whitelist, match against everything (default, backwards-compat).
# []   = match against nothing, always cluster.
session_whitelist: "list[str] | None" = None


def _maybe_auto_train(
    emb: np.ndarray,
    name: str,
    confidence: float,
    runner_up_confidence: float,
) -> bool:
    """Append `emb` to profile `name` if all guardrails pass. Returns
    True iff a sample was actually added."""
    if not auto_train_settings["enabled"]:
        return False
    if confidence < auto_train_settings["floor"]:
        return False
    # Margin requirement scales with the active sensitivity preset's
    # MARGIN: stricter presets demand stricter auto-train margin.
    margin_gap = confidence - runner_up_confidence
    required = settings["margin"] * auto_train_settings["margin_multiplier"]
    if margin_gap < required:
        return False
    profile = profiles.get(name)
    if profile is None:
        return False
    if profile["samples"].shape[0] < auto_train_settings["min_samples"]:
        return False
    # `_add_sample` runs the outlier check as well. An auto-train sample
    # that scored 0.88+ in /identify will easily clear the 0.40 outlier
    # floor, so this is effectively a no-op for legitimate matches —
    # but it does protect against the edge case where the auto-train
    # centroid is held together by old samples and the new one is
    # actually from a similar-sounding stranger.
    _, accepted, _ = _add_sample(name, emb, source="auto")
    return accepted

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

# Default to CPU because sherpa-onnx's CoreML provider has been
# unreliable for the WeSpeaker model on recent macOS — every
# `compute()` returns "Unable to compute the prediction using a
# neural network model" with no useful diagnostic. The model is
# small (25 MB) and the inference is fast on CPU (~5-10 ms per 10 s
# audio window), so the performance trade is negligible. Override
# via MEETINK_DIARIZE_PROVIDER=coreml if you want to opt back in.
DIARIZE_PROVIDER = os.environ.get("MEETINK_DIARIZE_PROVIDER", "cpu")

print(
    f"loading model: {MODEL_PATH} (provider={DIARIZE_PROVIDER})",
    file=sys.stderr,
)
extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
    sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(MODEL_PATH),
        num_threads=2,
        debug=False,
        provider=DIARIZE_PROVIDER,
    )
)


# ---------------------------------------------------------------------------
# Profile storage
#
# Each profile dict contains:
#   centroids    K×D float32  — K cluster centroids (L2-normalised). K is
#                                derived from sample count, capped at
#                                PROFILE_MAX_CENTROIDS.
#   samples      N×D float32  — every enrollment / train / assign / auto
#                                sample, in addition order.
#   cluster_ids  N int32      — index into `centroids` for each sample.
#   timestamps   N float64    — unix epoch per sample; drives time decay.
#
# On disk: .npz with the four fields above. The legacy single-centroid
# format ({centroid, samples}) is loaded with cluster_ids all zero and
# fake-old timestamps so existing profiles keep working until they get
# re-saved (which happens on any mutation).
# ---------------------------------------------------------------------------


def _l2(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v if n == 0.0 else v / n


def _l2_rows(m: np.ndarray) -> np.ndarray:
    """Row-wise L2-normalise an N×D matrix."""
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return m / norms


def _centroid(samples: np.ndarray) -> np.ndarray:
    """Simple L2-normalised mean. Used by the in-memory session-cluster
    state where multi-centroid + time-decay don't apply (clusters are
    per-session, single-voice by definition)."""
    return _l2(samples.mean(axis=0))


def _pick_k(n: int) -> int:
    """Number of centroids for a profile with N samples. Single centroid
    until we have enough samples to support more; capped at the env-tuned
    MAX_CENTROIDS so we don't over-split."""
    if n < PROFILE_SAMPLES_PER_CENTROID:
        return 1
    return min(PROFILE_MAX_CENTROIDS, max(1, n // PROFILE_SAMPLES_PER_CENTROID))


def _time_weights(timestamps: np.ndarray) -> np.ndarray:
    """exp(-(now - t) / TAU). Older samples get less weight in the
    centroid. Returns ones if decay is disabled (TAU <= 0)."""
    if PROFILE_TIME_DECAY_TAU_S <= 0:
        return np.ones_like(timestamps, dtype=np.float64)
    now = time.time()
    ages = np.maximum(0.0, now - timestamps)
    return np.exp(-ages / PROFILE_TIME_DECAY_TAU_S)


def _farthest_point_init(samples: np.ndarray, k: int) -> list[int]:
    """Pick K well-separated sample indices to seed k-means. Greedy:
    start with sample 0, then iteratively add the sample with the lowest
    max-cosine-similarity to the picked set. Robust on the unit sphere."""
    n = samples.shape[0]
    if k >= n:
        return list(range(n))
    seeds = [0]
    for _ in range(k - 1):
        sims = samples @ samples[seeds].T  # N×|seeds|
        max_sims = np.max(sims, axis=1)
        for s in seeds:
            max_sims[s] = np.inf  # exclude already-picked
        seeds.append(int(np.argmin(max_sims)))
    return seeds


def _kmeans(samples: np.ndarray, k: int) -> np.ndarray:
    """Spherical k-means on L2-normalised embeddings. Returns an int32
    array of cluster assignments (length N). Uses cosine (= dot product
    on the unit sphere) as the similarity."""
    n = samples.shape[0]
    if k <= 1 or n <= 1:
        return np.zeros(n, dtype=np.int32)
    if k >= n:
        # Each sample is its own cluster (degenerate but well-defined)
        return np.arange(n, dtype=np.int32)

    seed_idx = _farthest_point_init(samples, k)
    centroids = samples[seed_idx].copy()

    prev_assign: np.ndarray | None = None
    for _ in range(_KMEANS_MAX_ITERS):
        sims = samples @ centroids.T              # N×K cosine matrix
        assign = np.argmax(sims, axis=1).astype(np.int32)
        if prev_assign is not None and np.array_equal(assign, prev_assign):
            break
        # Recompute centroids as L2-normalised means of their members.
        # Empty clusters (rare with farthest-point init) keep stale value.
        for ci in range(k):
            mask = assign == ci
            if mask.any():
                m = samples[mask].mean(axis=0)
                norm = float(np.linalg.norm(m))
                if norm > 0:
                    centroids[ci] = (m / norm).astype(np.float32)
        prev_assign = assign

    return assign


def _compute_centroids(
    samples: np.ndarray,
    cluster_ids: np.ndarray,
    k: int,
    timestamps: np.ndarray | None = None,
) -> np.ndarray:
    """L2-normalised time-weighted mean per cluster. Cluster with no
    members gets a zero vector (won't match anything — fine)."""
    d = samples.shape[1]
    centroids = np.zeros((k, d), dtype=np.float32)
    weights = _time_weights(timestamps) if timestamps is not None else None
    for ci in range(k):
        mask = cluster_ids == ci
        if not mask.any():
            continue
        if weights is None:
            mean_vec = samples[mask].mean(axis=0)
        else:
            w = weights[mask].astype(np.float32)
            wsum = float(w.sum())
            if wsum <= 0:
                mean_vec = samples[mask].mean(axis=0)
            else:
                mean_vec = (samples[mask] * w[:, None]).sum(axis=0) / wsum
        norm = float(np.linalg.norm(mean_vec))
        if norm > 0:
            centroids[ci] = (mean_vec / norm).astype(np.float32)
    return centroids


def _rebuild_profile(
    samples: np.ndarray, timestamps: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Cluster samples + compute centroids. Returns (centroids, cluster_ids,
    samples) — samples is returned for symmetry but unchanged."""
    n = samples.shape[0]
    k = _pick_k(n)
    cluster_ids = _kmeans(samples, k)
    # k might have been overridden by the degenerate case in _kmeans
    actual_k = int(cluster_ids.max()) + 1 if n > 0 else 1
    actual_k = max(actual_k, k)
    centroids = _compute_centroids(samples, cluster_ids, actual_k, timestamps)
    return centroids, cluster_ids, samples


profiles: dict[str, dict] = {}


def _load_all() -> None:
    profiles.clear()
    for path in PROFILES_DIR.glob("*.npz"):
        try:
            data = np.load(path)
            keys = set(data.files)
            samples = data["samples"].astype(np.float32)
            n = samples.shape[0]
            if "centroids" in keys:
                # New format with k centroids + per-sample metadata.
                profiles[path.stem] = {
                    "centroids": data["centroids"].astype(np.float32),
                    "samples": samples,
                    "cluster_ids": data["cluster_ids"].astype(np.int32),
                    "timestamps": data["timestamps"].astype(np.float64),
                }
            else:
                # Legacy: single "centroid" key. Synthesise the new fields
                # with all samples in cluster 0 and a single old timestamp.
                # First mutation re-clusters into the new format.
                old_centroid = data["centroid"].astype(np.float32)
                profiles[path.stem] = {
                    "centroids": old_centroid[np.newaxis, :],
                    "samples": samples,
                    "cluster_ids": np.zeros(n, dtype=np.int32),
                    "timestamps": np.full(
                        n, time.time() - 30 * 24 * 3600, dtype=np.float64,
                    ),
                }
        except Exception as e:
            print(
                f"warning: failed to load profile {path}: {e}",
                file=sys.stderr,
            )
    # Even-older format: .npy with one embedding (1-sample profile).
    for path in PROFILES_DIR.glob("*.npy"):
        if path.stem in profiles:
            continue
        try:
            emb = _l2(np.load(path).astype(np.float32))
            profiles[path.stem] = {
                "centroids": emb[np.newaxis, :],
                "samples": emb[np.newaxis, :],
                "cluster_ids": np.zeros(1, dtype=np.int32),
                "timestamps": np.array(
                    [time.time() - 30 * 24 * 3600], dtype=np.float64,
                ),
            }
        except Exception as e:
            print(
                f"warning: failed to load legacy profile {path}: {e}",
                file=sys.stderr,
            )


def _save(name: str) -> None:
    p = profiles[name]
    np.savez(
        PROFILES_DIR / f"{name}.npz",
        centroids=p["centroids"],
        samples=p["samples"],
        cluster_ids=p["cluster_ids"],
        timestamps=p["timestamps"],
    )


def _outlier_reject(name: str, new_emb_l2: np.ndarray) -> tuple[bool, float]:
    """Check whether `new_emb_l2` is too dissimilar from profile `name`'s
    existing centroids to be the same speaker. Returns (rejected, best_sim).
    No-op if the profile is new or empty."""
    if name not in profiles:
        return False, 1.0
    centroids = profiles[name]["centroids"]
    if centroids.shape[0] == 0:
        return False, 1.0
    sims = centroids @ new_emb_l2
    best_sim = float(np.max(sims))
    return best_sim < PROFILE_OUTLIER_FLOOR, best_sim


def _add_sample(
    name: str, embedding: np.ndarray, source: str = "manual",
) -> tuple[int, bool, float]:
    """Append a single sample to a profile. Re-clusters and re-saves.
    Returns (total_samples_after, accepted, best_sim_against_existing).

    `accepted = False` only when outlier rejection fires (sample's cosine
    against every existing centroid is below PROFILE_OUTLIER_FLOOR).
    Fresh profiles always accept.
    """
    new = _l2(embedding).astype(np.float32)
    rejected, best_sim = _outlier_reject(name, new)
    if rejected:
        print(
            f"outlier rejected: {name} "
            f"(best_sim={best_sim:.3f} < {PROFILE_OUTLIER_FLOOR}, "
            f"source={source})",
            file=sys.stderr,
        )
        existing_n = (
            profiles[name]["samples"].shape[0] if name in profiles else 0
        )
        return existing_n, False, best_sim

    now = time.time()
    if name in profiles:
        all_samples = np.vstack([profiles[name]["samples"], new[None, :]])
        all_timestamps = np.concatenate(
            [profiles[name]["timestamps"], [now]]
        )
    else:
        all_samples = new[None, :]
        all_timestamps = np.array([now], dtype=np.float64)

    centroids, cluster_ids, all_samples = _rebuild_profile(
        all_samples, all_timestamps,
    )
    profiles[name] = {
        "centroids": centroids,
        "samples": all_samples,
        "cluster_ids": cluster_ids,
        "timestamps": all_timestamps,
    }
    _save(name)
    return all_samples.shape[0], True, best_sim


def _add_samples_bulk(
    name: str,
    embeddings: np.ndarray,
    source: str = "manual",
    skip_outliers: bool = True,
) -> tuple[int, int]:
    """Append M samples to a profile in one shot. Used by /session/assign
    (folding a cluster into a profile) and /session/rename (merging two
    profiles). Returns (added_count, rejected_count).

    Outlier filtering applies the same per-sample floor as /enroll, so a
    /session/assign of a cluster that *doesn't actually match* the target
    profile drops the off-voice samples instead of polluting the centroid.
    """
    if embeddings.shape[0] == 0:
        return 0, 0
    new = _l2_rows(embeddings).astype(np.float32)

    accepted_mask = np.ones(new.shape[0], dtype=bool)
    if skip_outliers and name in profiles:
        sims = new @ profiles[name]["centroids"].T  # M×K
        max_sims = np.max(sims, axis=1)
        accepted_mask = max_sims >= PROFILE_OUTLIER_FLOOR
        rejected_count = int((~accepted_mask).sum())
        if rejected_count:
            print(
                f"bulk outlier reject: {name} "
                f"dropped {rejected_count}/{new.shape[0]} "
                f"(source={source}, floor={PROFILE_OUTLIER_FLOOR})",
                file=sys.stderr,
            )
    else:
        rejected_count = 0

    accepted = new[accepted_mask]
    added = int(accepted.shape[0])
    if added == 0:
        return 0, rejected_count

    now = time.time()
    timestamps_new = np.full(added, now, dtype=np.float64)
    if name in profiles:
        all_samples = np.vstack([profiles[name]["samples"], accepted])
        all_timestamps = np.concatenate(
            [profiles[name]["timestamps"], timestamps_new]
        )
    else:
        all_samples = accepted
        all_timestamps = timestamps_new

    centroids, cluster_ids, all_samples = _rebuild_profile(
        all_samples, all_timestamps,
    )
    profiles[name] = {
        "centroids": centroids,
        "samples": all_samples,
        "cluster_ids": cluster_ids,
        "timestamps": all_timestamps,
    }
    _save(name)
    return added, rejected_count


def _trim_profile(name: str, count: int) -> tuple[int, int]:
    """Drop the last `count` samples (and their metadata) from a profile,
    re-cluster, re-save. Returns (removed, remaining)."""
    p = profiles[name]
    n = p["samples"].shape[0]
    drop = min(count, n)
    new_samples = p["samples"][:-drop] if drop > 0 else p["samples"]
    new_timestamps = (
        p["timestamps"][:-drop] if drop > 0 else p["timestamps"]
    )
    remaining = new_samples.shape[0]
    centroids, cluster_ids, new_samples = _rebuild_profile(
        new_samples, new_timestamps,
    )
    profiles[name] = {
        "centroids": centroids,
        "samples": new_samples,
        "cluster_ids": cluster_ids,
        "timestamps": new_timestamps,
    }
    _save(name)
    return drop, remaining


_load_all()
print(
    f"loaded {len(profiles)} profile(s): "
    + (
        ", ".join(
            f"{k}({v['samples'].shape[0]}s/{v['centroids'].shape[0]}c)"
            for k, v in profiles.items()
        )
        or "(none)"
    ),
    file=sys.stderr,
)
print(
    f"profile tuning: max_centroids={PROFILE_MAX_CENTROIDS} "
    f"samples_per_centroid={PROFILE_SAMPLES_PER_CENTROID} "
    f"outlier_floor={PROFILE_OUTLIER_FLOOR} "
    f"time_decay_tau_days={PROFILE_TIME_DECAY_TAU_S / 86400:.0f}",
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

    # Apply session whitelist if set. Profiles outside it are simply
    # invisible to this match — voices that resemble them fall through
    # to clustering (THEM-X) just like any other unknown speaker.
    candidates = profiles
    if session_whitelist is not None:
        candidates = {
            n: p for n, p in profiles.items() if n in session_whitelist
        }
        if not candidates:
            return {"speaker": None, "confidence": 0.0, "runner_up": None, "runner_up_confidence": 0.0}

    # Per-profile score is the cosine to the BEST-matching centroid (not
    # a mean across centroids). Multimodal voices — same person with
    # different recording conditions or mood — get their best mode used
    # for matching instead of averaging modes into the middle.
    emb_l2 = _l2(emb).astype(np.float32)
    sims = sorted(
        (
            (name, float(np.max(p["centroids"] @ emb_l2)))
            for name, p in candidates.items()
        ),
        key=lambda kv: kv[1],
        reverse=True,
    )
    top_name, top_sim = sims[0]
    second_name, second_sim = (sims[1] if len(sims) > 1 else (None, -1.0))

    accepted = (
        top_sim >= settings["threshold"]
        and (top_sim - second_sim) >= settings["margin"]
    )
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
        if sim >= settings["cluster_threshold"]:
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
                "threshold": settings["threshold"],
                "margin": settings["margin"],
                "cluster_threshold": settings["cluster_threshold"],
                "preset": settings_preset,
                "clusters": len(clusters),
                "whitelist": session_whitelist,
            })
            return
        if path == "/profiles":
            self._json(200, {
                "profiles": [
                    {
                        "name": n,
                        "samples": int(p["samples"].shape[0]),
                        "centroids": int(p["centroids"].shape[0]),
                    }
                    for n, p in profiles.items()
                ]
            })
            return
        if path == "/session/sensitivity":
            self._json(200, {
                "preset": settings_preset,
                "threshold": settings["threshold"],
                "margin": settings["margin"],
                "cluster_threshold": settings["cluster_threshold"],
                "available": list(PRESETS.keys()),
            })
            return
        if path == "/session/auto-train":
            self._json(200, dict(auto_train_settings))
            return
        if path == "/session/whitelist":
            self._json(200, {
                "whitelist": session_whitelist,
                "profiles_known": list(profiles.keys()),
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
                else:
                    # High-confidence profile match → fold the embedding
                    # back into the profile if the auto-train guardrails
                    # all pass. The guardrails (floor / margin multiplier
                    # / min-samples) make this conservative on purpose;
                    # the cost of polluting a profile is much higher
                    # than the cost of skipping a marginal match.
                    runner_up = resp.get("runner_up_confidence") or 0.0
                    if _maybe_auto_train(
                        emb,
                        resp["speaker"],
                        resp.get("confidence") or 0.0,
                        runner_up,
                    ):
                        resp["auto_trained"] = True
                        print(
                            f"auto-train: {resp['speaker']} += sample "
                            f"(confidence={resp.get('confidence')}, "
                            f"runner_up={runner_up}, "
                            f"total={profiles[resp['speaker']]['samples'].shape[0]})",
                            file=sys.stderr,
                        )
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
                # Promote the cluster's samples to the profile via
                # _add_samples_bulk so outlier rejection + k-means kick
                # in. Re-assigning into an existing profile accumulates
                # voice data; the outlier floor drops any cluster samples
                # that clearly don't match the target's centroids (catches
                # the failure mode where /profile assign A flavio is run
                # on a cluster that's actually a different speaker).
                added, rejected = _add_samples_bulk(
                    name, cluster["samples"],
                    source=f"assign:{letter}",
                    # First-time profile creation: nothing to be outlier
                    # vs, so skip the check on `name not in profiles`.
                    skip_outliers=(name in profiles),
                )
                clusters.remove(cluster)
                total = int(profiles[name]["samples"].shape[0])
                print(
                    f"session: cluster {letter} → profile {name} "
                    f"(+{added} samples, {rejected} outliers rejected, "
                    f"total={total})",
                    file=sys.stderr,
                )
                self._json(200, {
                    "ok": True,
                    "cluster": letter,
                    "name": name,
                    "samples": total,
                    "added": added,
                    "rejected": rejected,
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
            if url.path == "/session/whitelist":
                qs = parse_qs(url.query)
                global session_whitelist
                if qs.get("clear", [""])[0].lower() in ("1", "true", "yes"):
                    session_whitelist = None
                    print("session: whitelist cleared", file=sys.stderr)
                    self._json(200, {"ok": True, "whitelist": None})
                    return
                raw = qs.get("profiles", [""])[0].strip()
                if not raw:
                    self._json(400, {
                        "error": "need ?profiles=alex,stacey or ?clear=true",
                    })
                    return
                requested = [n.strip() for n in raw.split(",") if n.strip()]
                # Filter to profiles we actually know about. Unknown names
                # are reported back so the caller can warn.
                known = [n for n in requested if n in profiles]
                unknown = [n for n in requested if n not in profiles]
                session_whitelist = known
                print(
                    f"session: whitelist set to {known} "
                    f"(unknown ignored: {unknown})",
                    file=sys.stderr,
                )
                self._json(200, {
                    "ok": True,
                    "whitelist": known,
                    "unknown": unknown,
                })
                return
            if url.path == "/session/auto-train":
                qs = parse_qs(url.query)
                changed: dict = {}
                if "enabled" in qs:
                    v = qs["enabled"][0].strip().lower()
                    auto_train_settings["enabled"] = v in (
                        "1", "true", "yes", "on",
                    )
                    changed["enabled"] = auto_train_settings["enabled"]
                if "floor" in qs:
                    try:
                        f = float(qs["floor"][0])
                    except ValueError:
                        self._json(400, {"error": "floor must be a number"})
                        return
                    if not (0.0 <= f <= 1.0):
                        self._json(400, {"error": "floor must be between 0 and 1"})
                        return
                    auto_train_settings["floor"] = f
                    changed["floor"] = f
                if "margin_multiplier" in qs:
                    try:
                        m = float(qs["margin_multiplier"][0])
                    except ValueError:
                        self._json(400, {
                            "error": "margin_multiplier must be a number",
                        })
                        return
                    if m < 0:
                        self._json(400, {
                            "error": "margin_multiplier must be >= 0",
                        })
                        return
                    auto_train_settings["margin_multiplier"] = m
                    changed["margin_multiplier"] = m
                if "min_samples" in qs:
                    try:
                        n = int(qs["min_samples"][0])
                    except ValueError:
                        self._json(400, {
                            "error": "min_samples must be an integer",
                        })
                        return
                    if n < 1:
                        self._json(400, {
                            "error": "min_samples must be >= 1",
                        })
                        return
                    auto_train_settings["min_samples"] = n
                    changed["min_samples"] = n
                if not changed:
                    self._json(400, {
                        "error": (
                            "no settings provided — pass at least one of "
                            "?enabled=&floor=&margin_multiplier=&min_samples="
                        ),
                    })
                    return
                print(f"auto-train updated: {changed}", file=sys.stderr)
                self._json(200, {
                    "ok": True,
                    "changed": changed,
                    **dict(auto_train_settings),
                })
                return
            if url.path == "/session/sensitivity":
                qs = parse_qs(url.query)
                mode = (qs.get("mode", [""])[0]).strip().lower()
                if not mode:
                    self._json(400, {"error": "need ?mode=focused|default|strict"})
                    return
                if mode not in PRESETS:
                    self._json(400, {
                        "error": f"unknown mode '{mode}'",
                        "available": list(PRESETS.keys()),
                    })
                    return
                # Mutate in place so existing dict references stay live.
                # Each consumer reads via dict lookup at call time, so
                # the next /identify hits the new values immediately.
                preset = PRESETS[mode]
                settings["threshold"] = preset["threshold"]
                settings["margin"] = preset["margin"]
                settings["cluster_threshold"] = preset["cluster_threshold"]
                global settings_preset
                settings_preset = mode
                print(
                    f"sensitivity: preset={mode} "
                    f"threshold={settings['threshold']} "
                    f"margin={settings['margin']} "
                    f"cluster_threshold={settings['cluster_threshold']}",
                    file=sys.stderr,
                )
                self._json(200, {
                    "ok": True,
                    "preset": mode,
                    "threshold": settings["threshold"],
                    "margin": settings["margin"],
                    "cluster_threshold": settings["cluster_threshold"],
                })
                return
            if url.path == "/session/rename":
                qs = parse_qs(url.query)
                src_name = (qs.get("from", [""])[0]).strip()
                dst_name = (qs.get("to", [""])[0]).strip()
                if not src_name or not dst_name:
                    self._json(400, {"error": "need ?from=alice&to=alex"})
                    return
                if src_name == dst_name:
                    self._json(400, {"error": "from and to must differ"})
                    return
                if any(c in dst_name for c in "/\\.."):
                    self._json(400, {"error": "invalid name (no slashes or dots)"})
                    return
                if src_name not in profiles:
                    self._json(404, {"error": f"no profile named {src_name}"})
                    return
                src_samples = profiles[src_name]["samples"]
                src_timestamps = profiles[src_name]["timestamps"]
                merged_into_existing = dst_name in profiles
                rejected = 0
                if merged_into_existing:
                    # Fold via _add_samples_bulk so outlier rejection +
                    # k-means kick in. This is exactly the FLAVIO/BOB
                    # failure path — folding samples from a *different*
                    # speaker would have polluted the destination
                    # centroid. Now: each src sample is checked against
                    # dst's centroids; ones that clearly don't match get
                    # dropped with a per-source log line.
                    _, rejected = _add_samples_bulk(
                        dst_name, src_samples,
                        source=f"rename:{src_name}",
                        skip_outliers=True,
                    )
                else:
                    # Pure rename: rekey, preserving all metadata. No
                    # outlier check (nothing to compare against).
                    profiles[dst_name] = profiles[src_name]
                    _save(dst_name)
                # Drop src from memory and disk regardless of which branch.
                profiles.pop(src_name, None)
                for ext in (".npz", ".npy"):
                    p = PROFILES_DIR / f"{src_name}{ext}"
                    if p.exists():
                        p.unlink()
                count = int(profiles[dst_name]["samples"].shape[0])
                print(
                    f"renamed: {src_name} → {dst_name} "
                    f"({count} samples, merged={merged_into_existing}, "
                    f"rejected={rejected})",
                    file=sys.stderr,
                )
                self._json(200, {
                    "ok": True,
                    "from": src_name,
                    "to": dst_name,
                    "samples": count,
                    "merged": merged_into_existing,
                    "rejected": rejected,
                })
                return
            if url.path.startswith("/profiles/") and url.path.endswith("/pop"):
                name = url.path[len("/profiles/"):-len("/pop")].strip()
                if not name:
                    self._json(400, {"error": "missing profile name"})
                    return
                if name not in profiles:
                    self._json(404, {"error": f"no profile named {name}"})
                    return
                qs = parse_qs(url.query)
                try:
                    count = int(qs.get("count", ["1"])[0])
                except ValueError:
                    self._json(400, {"error": "count must be an integer"})
                    return
                if count < 1:
                    self._json(400, {"error": "count must be >= 1"})
                    return
                total = int(profiles[name]["samples"].shape[0])
                if count >= total:
                    self._json(400, {
                        "error": (
                            f"can't pop {count} of {total} — would empty "
                            f"the profile. Use DELETE /profiles/{name} "
                            f"instead."
                        ),
                    })
                    return
                removed, remaining = _trim_profile(name, count)
                print(
                    f"popped: {name} -{removed} sample(s), "
                    f"remaining={remaining}",
                    file=sys.stderr,
                )
                self._json(200, {
                    "ok": True,
                    "name": name,
                    "removed": removed,
                    "remaining": remaining,
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
                count, accepted, best_sim = _add_sample(
                    name, embed(samples), source="enroll",
                )
                if not accepted:
                    # Outlier rejected. The sample's cosine vs every
                    # existing centroid was below PROFILE_OUTLIER_FLOOR
                    # — almost certainly a different voice slipped into
                    # the recording. Surface the score so the caller
                    # can show a helpful warning instead of silently
                    # dropping.
                    self._json(200, {
                        "ok": False,
                        "rejected": "outlier",
                        "name": name,
                        "samples": count,
                        "best_sim": round(best_sim, 3),
                        "floor": PROFILE_OUTLIER_FLOOR,
                    })
                    return
                print(
                    f"enrolled: {name} ({len(samples) / 16000:.1f}s, "
                    f"total={count}, best_sim={best_sim:.3f})",
                    file=sys.stderr,
                )
                self._json(200, {
                    "ok": True,
                    "name": name,
                    "samples": count,
                    "best_sim": round(best_sim, 3),
                })
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
    print(
        f"diarize-server ready on 127.0.0.1:{PORT} "
        f"(preset={settings_preset}, threshold={settings['threshold']}, "
        f"margin={settings['margin']}, "
        f"cluster_threshold={settings['cluster_threshold']})",
        file=sys.stderr,
    )
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
