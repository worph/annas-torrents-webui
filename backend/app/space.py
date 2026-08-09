"""Deletion ranking for free-space workflows. Pure helpers, no I/O."""

from __future__ import annotations

import math

# Decimal GB — prefer keeping torrents smaller than this; only pick them if needed.
PREFER_KEEP_BELOW_BYTES = 10 * 1000**3
# Back-compat alias for tests / callers.
PROTECT_BELOW_BYTES = PREFER_KEEP_BELOW_BYTES


def deletion_score(size_bytes: int | float, seed_count: int | float) -> float:
    """Higher = more disposable (big + well-replicated)."""
    size_gb = max(0.0, float(size_bytes)) / (1000**3)
    seeds = max(0.0, float(seed_count))
    return math.log1p(size_gb) * math.log1p(seeds + 1)


def classify_torrent(t: dict) -> dict:
    """Normalize a torrents_status row for space UI / pick_combination."""
    infohash = (t.get("infohash") or "").lower()
    name = t.get("name") or ""
    size = int(t.get("size") or 0)
    state = str(t.get("state") or "").lower()
    progress = t.get("progress")
    # Explicit incompleteness / missing files beats a stale progress=1.0.
    if t.get("is_complete") is False or state in {
        "missing_files",
        "error",
        "unknown",
    }:
        incomplete = True
    elif progress is not None:
        incomplete = float(progress) < 1.0
    elif "incomplete" in t:
        incomplete = bool(t["incomplete"])
    else:
        # Default unknown → incomplete (do not treat as fully reclaimable).
        incomplete = not bool(t.get("is_complete", False))

    if t.get("seeds_known") is False:
        seeds_known = False
        num_seeds = t.get("num_seeds")
    else:
        # Prefer swarm total for deletion ranking (same as qBit parentheses value).
        tot = t.get("seeds_total")
        if tot is not None and int(tot) >= 0:
            seeds_known = True
            num_seeds = int(tot)
        elif t.get("num_seeds") is not None:
            seeds_known = True
            num_seeds = int(t.get("num_seeds"))
        else:
            seeds_known = False
            num_seeds = None

    prefer_keep = size < PREFER_KEEP_BELOW_BYTES
    raw_reclaimable = t.get("allocated_bytes")
    if raw_reclaimable is None:
        raw_reclaimable = t.get("downloaded")
    if raw_reclaimable is None:
        raw_reclaimable = size * max(0.0, min(1.0, float(progress or 0.0)))
    reclaimable = max(0, int(raw_reclaimable))
    if not incomplete:
        reclaimable = max(reclaimable, size)
    elif size:
        reclaimable = min(reclaimable, size)
    if incomplete:
        reason = "incomplete"
    elif not seeds_known:
        reason = "seeds unknown"
    elif prefer_keep:
        reason = "prefer keep (<10 GB)"
    else:
        reason = "eligible"

    return {
        "infohash": infohash,
        "name": name,
        "size": size,
        "reclaimable_bytes": reclaimable,
        "num_seeds": num_seeds,
        "seeds_known": seeds_known,
        "protected": prefer_keep,  # soft preference; may still be auto-selected
        "incomplete": incomplete,
        "reason": reason,
        "save_path": t.get("save_path") or "",
    }


def _greedy_pick(
    pool: list[dict],
    request_bytes: int,
    already_freed: int = 0,
    *,
    key,
) -> tuple[list[dict], int]:
    """Pick in ``key`` descending order until freed >= request. Returns (selected, freed)."""
    pool = sorted(pool, key=key, reverse=True)
    selected: list[dict] = []
    freed = already_freed
    used: set[int] = set()
    for i, c in enumerate(pool):
        if freed >= request_bytes:
            break
        selected.append(c)
        used.add(i)
        freed += int(c["reclaimable_bytes"])

    # Optional one-swap: replace last pick with a later unused item that reduces overshoot.
    if selected and freed > request_bytes and request_bytes > 0:
        last = selected[-1]
        without = freed - last["reclaimable_bytes"]
        best_overshoot = freed - request_bytes
        best: dict | None = None
        last_i = max(used)
        for i, c in enumerate(pool):
            if i in used or i <= last_i:
                continue
            total = without + int(c["reclaimable_bytes"])
            if total < request_bytes:
                continue
            overshoot = total - request_bytes
            if overshoot < best_overshoot:
                best_overshoot = overshoot
                best = c
        if best is not None:
            selected[-1] = best
            freed = without + best["reclaimable_bytes"]

    return selected, freed


def pick_combination(candidates: list, request_bytes: int) -> dict:
    """≥10 GB first (deletion_score); only then <10 GB largest→smallest.

    Never touches <10 GB while ≥10 GB can still cover the request.
    Excludes seeds_known=False from auto selection.
    # ponytail: greedy two-pass (+ optional one-swap trim); DP knapsack if overshoot becomes a complaint.
    """
    request_bytes = max(0, int(request_bytes))
    unscored: list[dict] = []
    large: list[dict] = []
    small: list[dict] = []

    for raw in candidates:
        c = classify_torrent(raw)
        if c["reclaimable_bytes"] <= 0:
            continue
        if not c["seeds_known"]:
            unscored.append(c)
            continue
        if c["protected"]:
            small.append(c)
        else:
            large.append(c)

    score_key = lambda c: deletion_score(c["size"], int(c["num_seeds"] or 0))
    size_key = lambda c: int(c["size"])

    selected, freed = _greedy_pick(large, request_bytes, key=score_key)
    if freed < request_bytes and small:
        more, freed = _greedy_pick(small, request_bytes, already_freed=freed, key=size_key)
        selected.extend(more)

    return {
        "selected": selected,
        "freed_bytes": freed,
        "overshoot_bytes": max(0, freed - request_bytes) if request_bytes else 0,
        "protected_manual": [],  # API compat; soft prefer-keep no longer blocks
        "unscored": unscored,
    }

if __name__ == "__main__":
    GB = 1000**3
    assert deletion_score(0, 0) == 0.0
    assert deletion_score(20 * GB, 100) > deletion_score(20 * GB, 1)

    small = classify_torrent(
        {"infohash": "aa", "name": "tiny", "size": 5 * GB, "num_seeds": 10, "progress": 1.0}
    )
    assert small["protected"] and "prefer keep" in small["reason"]

    big = {"infohash": "bb", "name": "big", "size": 50 * GB, "num_seeds": 50, "progress": 1.0}
    unknown = {
        "infohash": "cc",
        "name": "unk",
        "size": 50 * GB,
        "num_seeds": None,
        "seeds_known": False,
        "progress": 1.0,
    }
    out = pick_combination([small, big, unknown], 40 * GB)
    assert [s["infohash"] for s in out["selected"]] == ["bb"]
    assert out["freed_bytes"] == 50 * GB
    assert out["overshoot_bytes"] == 10 * GB
    assert len(out["unscored"]) == 1

    # Only small torrents — largest first (keep tiniest last), ignore seed score.
    only_small = pick_combination(
        [
            {"infohash": "s1", "name": "tiny", "size": 2 * GB, "num_seeds": 999, "progress": 1.0},
            {"infohash": "s2", "name": "mid", "size": 8 * GB, "num_seeds": 1, "progress": 1.0},
        ],
        5 * GB,
    )
    assert [s["infohash"] for s in only_small["selected"]] == ["s2"]

    # Incomplete uses downloaded/progress-weighted bytes, not full torrent size.
    inc = pick_combination(
        [
            {
                "infohash": "i",
                "name": "inc",
                "size": 20 * GB,
                "downloaded": 10 * GB,
                "num_seeds": 5,
                "progress": 0.5,
            }
        ],
        10 * GB,
    )
    assert len(inc["selected"]) == 1 and inc["selected"][0]["incomplete"] is True
    print("ok: space deletion ranking checks passed")
