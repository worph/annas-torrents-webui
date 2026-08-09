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


def _candidate_bytes(c: dict) -> int:
    return max(0, int(c.get("reclaimable_bytes") or 0))


def _best_single(pool: list[dict], request_bytes: int) -> tuple[list[dict], int] | None:
    """Smallest single item that covers the request (min overshoot)."""
    best: dict | None = None
    best_over = None
    for c in pool:
        b = _candidate_bytes(c)
        if b < request_bytes:
            continue
        over = b - request_bytes
        if best is None or over < best_over:
            best, best_over = c, over
    if best is None:
        return None
    return [best], _candidate_bytes(best)


def _best_pair(pool: list[dict], request_bytes: int) -> tuple[list[dict], int] | None:
    """Best 2-sum cover with minimal overshoot (O(n²); pools stay small)."""
    best: tuple[dict, dict] | None = None
    best_over = None
    best_total = None
    n = len(pool)
    for i in range(n):
        bi = _candidate_bytes(pool[i])
        for j in range(i + 1, n):
            total = bi + _candidate_bytes(pool[j])
            if total < request_bytes:
                continue
            over = total - request_bytes
            if best is None or over < best_over or (over == best_over and total < best_total):
                best = (pool[i], pool[j])
                best_over = over
                best_total = total
    if best is None:
        return None
    return [best[0], best[1]], best_total


def _best_triple(pool: list[dict], request_bytes: int) -> tuple[list[dict], int] | None:
    """Best 3-sum cover with minimal overshoot.

    # ponytail: O(n³) capped at 36 largest items; full DP if packs stay huge.
    """
    if len(pool) > 36:
        pool = sorted(pool, key=_candidate_bytes, reverse=True)[:36]
    best: tuple[dict, dict, dict] | None = None
    best_over = None
    best_total = None
    n = len(pool)
    for i in range(n):
        bi = _candidate_bytes(pool[i])
        for j in range(i + 1, n):
            bij = bi + _candidate_bytes(pool[j])
            for k in range(j + 1, n):
                total = bij + _candidate_bytes(pool[k])
                if total < request_bytes:
                    continue
                over = total - request_bytes
                if best is None or over < best_over or (over == best_over and total < best_total):
                    best = (pool[i], pool[j], pool[k])
                    best_over = over
                    best_total = total
    if best is None:
        return None
    return [best[0], best[1], best[2]], best_total


def _pick_min_overshoot(
    greedy_sel: list[dict],
    greedy_freed: int,
    pool: list[dict],
    request_bytes: int,
) -> tuple[list[dict], int]:
    """Prefer greedy unless a single/pair/triple covers with less overshoot."""
    if request_bytes <= 0:
        return greedy_sel, greedy_freed
    candidates: list[tuple[list[dict], int]] = [(greedy_sel, greedy_freed)]
    single = _best_single(pool, request_bytes)
    if single:
        candidates.append(single)
    pair = _best_pair(pool, request_bytes)
    if pair:
        candidates.append(pair)
    triple = _best_triple(pool, request_bytes)
    if triple:
        candidates.append(triple)
    # Prefer covering plans; among covers, min overshoot then fewer items.
    covering = [(s, f) for s, f in candidates if f >= request_bytes]
    if not covering:
        return greedy_sel, greedy_freed
    covering.sort(key=lambda sf: (sf[1] - request_bytes, len(sf[0]), -sf[1]))
    return covering[0]


def pick_combination(candidates: list, request_bytes: int) -> dict:
    """≥10 GB first (deletion_score); only then <10 GB largest→smallest.

    Never touches <10 GB while ≥10 GB can still cover the request.
    Excludes seeds_known=False from auto selection.
    After greedy, also try best single/pair/triple for lower overshoot.
    # ponytail: full DP knapsack only if >3-item overshoot becomes a complaint.
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

    # Prefer lower overshoot from large pool alone when it still covers.
    if request_bytes and large:
        large_only_sel, large_only_freed = _pick_min_overshoot(
            [c for c in selected if not c.get("protected")],
            sum(_candidate_bytes(c) for c in selected if not c.get("protected")),
            large,
            request_bytes,
        )
        # If large alone covers, prefer that overshoot-aware plan (avoids eating small).
        if large_only_freed >= request_bytes:
            selected, freed = large_only_sel, large_only_freed
        else:
            pool = large + small
            selected, freed = _pick_min_overshoot(selected, freed, pool, request_bytes)

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

    # Three mid-size packs beat one huge torrent on overshoot.
    triple_pool = [
        {"infohash": "t1", "name": "a", "size": 20 * GB, "num_seeds": 10, "progress": 1.0},
        {"infohash": "t2", "name": "b", "size": 20 * GB, "num_seeds": 10, "progress": 1.0},
        {"infohash": "t3", "name": "c", "size": 20 * GB, "num_seeds": 10, "progress": 1.0},
        {"infohash": "t4", "name": "huge", "size": 100 * GB, "num_seeds": 10, "progress": 1.0},
    ]
    trip = pick_combination(triple_pool, 55 * GB)
    assert trip["freed_bytes"] == 60 * GB
    assert trip["overshoot_bytes"] == 5 * GB
    assert len(trip["selected"]) == 3
    assert "t4" not in {s["infohash"] for s in trip["selected"]}

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
