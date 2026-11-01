"""WHOOPS measurement module.

Computes fingerprint measurement metrics on ANY dataset frame handed to it. It has no
knowledge of file names, dataset sizes or a fixed column list: columns are discovered
from the data, and pairwise metrics switch from exact to seeded pair sampling above a
size threshold, so cost does not grow quadratically with the number of records.

Metrics (see DEFINITIONS for the exact formulas, also returned with every report):
  A uniqueness          B Shannon entropy       C normalised entropy
  D collision rate      E Hamming distance      F Jaccard similarity
  G feature contribution (extends analysing_module's ablation)
  H stability           I distinguishability    J bootstrap confidence intervals

Honesty rules enforced here
  * H and I are only computed from a real browser_id/session_id column with repeated
    observations. Otherwise the result says so; nothing is inferred or invented.
  * Every report states how many records are real / synthetic / unverified and flags
    any mixture. Synthetic results are never labelled as real-world measurements.
  * No hypothesis tests are run, so no statistical significance is ever claimed.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from analysing_module import (
    METADATA_COLUMNS, VOLATILE_COLUMNS, _norm, compute_feature_contribution,
)

MISSING = "nan"  # sentinel produced by analysing_module._norm for missing cells
MIN_RECORDS = 2
INSUFFICIENT_STABILITY = "Insufficient data for stability measurement."
NO_DISTINGUISHABILITY = (
    "Distinguishability cannot currently be calculated: the dataset has no reliable "
    "browser/session labels with repeated observations."
)

DEFINITIONS = {
    "uniqueness_pct": "Uniqueness (%) = records whose full fingerprint occurs exactly once / total records x 100. "
                      "(Same definition as the existing feature-ablation analysis.) "
                      "Volatile window-size columns are excluded, as in the existing analysis.",
    "shannon_entropy_bits": "H(X) = -sum p(x) log2 p(x) over the empirical distribution of values (missing is its own value). "
                            "Plug-in estimate: biased low for small samples.",
    "normalized_entropy": "H(X) / log2(k), k = number of distinct observed values. k = 1 -> 0 (no uncertainty); k = 0 -> undefined. "
                          "k is observed from the sample, so with few records the ratio saturates towards 1.",
    "collision_rate_record_pct": "Record-based: % of records that belong to a fingerprint group of size >= 2 (= 100 - uniqueness).",
    "pairwise_collision_probability": "Pairwise: sum c(c-1) / (N(N-1)) = probability that two different, randomly chosen records share a fingerprint.",
    "hamming": "Categorical Hamming distance between two records = number of fingerprint fields that differ, counting only fields "
               "present in BOTH records. Normalised = differing / comparable fields. Exact up to a record limit, seeded pair sampling above it.",
    "jaccard": "|A n B| / |A u B| for SET-VALUED features only (JSON-array cells, or columns declared set-valued). "
               "Two empty sets -> 1.0 by convention; missing values are skipped. Never applied to ordinary categorical fields.",
    "feature_contribution_pp": "Baseline uniqueness - uniqueness after removing the feature category, in percentage points "
                               "(extends analysing_module's ablation; same categories).",
    "stability": "Needs repeated observations of the same browser (browser_id, else session_id). Computed over consecutive observations "
                 "of each browser in time order: % of transitions where the fingerprint did not change.",
    "distinguishability": "Needs browser/session labels. Compares normalised Hamming distance between observations of the SAME browser (intra) "
                          "with distance between DIFFERENT browsers (inter). AUC is an effect size, not a significance test.",
    "confidence_interval": "Stability: percentile bootstrap of the mean per-browser stability, resampling whole browsers "
                           "(needs >= 20 browsers with repeats). Uniqueness, collision, entropy and feature contribution are "
                           "singleton/plug-in statistics for which simulated bootstrap coverage was far below nominal, so no "
                           "interval is reported unless experimental_intervals is switched on. No hypothesis test is performed.",
}


@dataclass
class MeasurementConfig:
    bootstrap_iterations: int = 1000
    confidence_level: float = 0.95
    seed: int = 12345
    min_units_for_ci: int = 20          # independent units (records or browsers) needed for a CI
    max_exact_records: int = 3000       # pairwise metrics are exact up to this many records
    sampled_pairs: int = 300_000        # pairs sampled above that
    pair_chunk: int = 200_000
    max_exact_set_records: int = 1000   # Jaccard (python sets) exact up to this many records
    sampled_set_pairs: int = 100_000
    set_valued_features: tuple = ()     # declare delimiter-separated set columns here
    set_delimiter: str = "|"
    bootstrap_budget: float = 3e8       # caps resamples for very large datasets
    ci_method: str = "percentile"       # "percentile" or "basic"
    # Bootstrap intervals for uniqueness/collision/entropy/contribution are OFF by default: simulations
    # (tests/validate_bootstrap_coverage.py) showed their coverage is far below nominal. See README.
    experimental_intervals: bool = False


# =========================================================================== helpers
def _clean(obj):
    """Make nested results JSON-safe (numpy -> python, nan/inf -> None)."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [_clean(v) for v in obj.tolist()]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def _is_blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip() == "" \
        or (v is pd.NA)


def _prepare_column(series: pd.Series) -> pd.Series:
    """Trim text, blank -> missing, unify True/true -> True."""
    def fix(v):
        if _is_blank(v):
            return np.nan
        t = str(v).strip()
        low = t.lower()
        return "True" if low == "true" else "False" if low == "false" else t
    return series.astype(object).map(fix)


def parse_set(value, delimiter: str = "|"):
    """Return a frozenset, or None when the cell is missing/unparseable.
    JSON arrays ('["a","b"]') are unambiguous; any other text is split on `delimiter`
    (only reached for columns the caller explicitly declared set-valued)."""
    if _is_blank(value):
        return None
    text = str(value).strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            return None
        return frozenset(str(x).strip() for x in items) if isinstance(items, list) else None
    return frozenset(t.strip() for t in text.split(delimiter) if t.strip())


def jaccard(a: frozenset, b: frozenset, both_empty: float = 1.0) -> float:
    """|A n B| / |A u B|; two empty sets are identical, so 1.0 by convention."""
    union = len(a | b)
    return both_empty if union == 0 else len(a & b) / union


def detect_set_columns(df: pd.DataFrame, cfg: MeasurementConfig) -> list[str]:
    """Declared columns plus columns whose cells are (almost) all JSON arrays.
    Delimiter heuristics are deliberately NOT used: userAgent contains ';' and ','."""
    cols = [c for c in cfg.set_valued_features if c in df.columns]
    for c in df.columns:
        if c in cols or c in METADATA_COLUMNS:
            continue
        vals = df[c].dropna().astype(str).str.strip()
        vals = vals[vals != ""]
        if len(vals) and (vals.str.startswith("[") & vals.str.endswith("]")).mean() >= 0.9 \
                and all(parse_set(v) is not None for v in vals.head(200)):
            cols.append(c)
    return cols


def _canonical_set(v, delimiter):
    s = parse_set(v, delimiter)
    return np.nan if s is None else json.dumps(sorted(s))


@dataclass
class Prepared:
    n: int
    raw: pd.DataFrame
    norm: pd.DataFrame
    fp_cols: list
    volatile_cols: list
    dropped_empty: list
    set_cols: list


def prepare(df: pd.DataFrame, cfg: MeasurementConfig) -> Prepared:
    """Discover feature columns dynamically and normalise every cell to a comparable string."""
    df = df.reset_index(drop=True)
    candidates = [c for c in df.columns if c not in METADATA_COLUMNS]
    set_cols = detect_set_columns(df, cfg) if len(df) else []
    norm = pd.DataFrame(index=df.index)
    for c in candidates:
        col = _prepare_column(df[c])
        if c in set_cols:  # order-insensitive representation of a set
            col = col.map(lambda v: _canonical_set(v, cfg.set_delimiter))
        norm[c] = _norm(col)
    dropped = [c for c in candidates if len(df) and (norm[c] == MISSING).all()]
    fp = [c for c in candidates if c not in VOLATILE_COLUMNS and c not in dropped]
    vol = [c for c in candidates if c in VOLATILE_COLUMNS and c not in dropped]
    return Prepared(len(df), df, norm, fp, vol, dropped, [c for c in set_cols if c not in dropped])


def group_codes(norm: pd.DataFrame, cols: list) -> np.ndarray:
    if not cols:
        return np.zeros(len(norm), dtype=np.int64)
    return norm.groupby(list(cols), sort=False).ngroup().to_numpy()


def _factor(series: pd.Series) -> np.ndarray:
    codes, _ = pd.factorize(series.where(series != MISSING))  # missing -> -1
    return codes.astype(np.int32)


def code_matrix(norm: pd.DataFrame, cols: list) -> np.ndarray:
    return np.column_stack([_factor(norm[c]) for c in cols]).astype(np.int32) if cols else \
        np.empty((len(norm), 0), dtype=np.int32)


def _insufficient(message: str, **extra) -> dict:
    return {"status": "insufficient_data", "message": message, **extra}


# =========================================================================== pairs
def _all_pairs_chunks(n: int, chunk: int):
    """Yield every unordered pair (i<j) in index-array chunks of about `chunk` pairs."""
    r = 0
    while r < n - 1:
        ii, jj, total = [], [], 0
        while r < n - 1 and (total == 0 or total + (n - 1 - r) <= chunk):
            ii.append(np.full(n - 1 - r, r, dtype=np.int64))
            jj.append(np.arange(r + 1, n, dtype=np.int64))
            total += n - 1 - r
            r += 1
        yield np.concatenate(ii), np.concatenate(jj)


def _random_pairs(n: int, m: int, rng, chunk: int):
    """Yield `m` uniformly random pairs of DIFFERENT records."""
    left = m
    while left > 0:
        k = min(chunk, left)
        i = rng.integers(0, n, k)
        j = rng.integers(0, n - 1, k)
        yield i, j + (j >= i)
        left -= k


def _pair_stream(n, exact, m, rng, chunk):
    return _all_pairs_chunks(n, chunk) if exact else _random_pairs(n, m, rng, chunk)


def _pair_distance(matrix: np.ndarray, i, j):
    """(differing fields, comparable fields) per pair; fields missing in either record are skipped."""
    a, b = matrix[i], matrix[j]
    valid = (a >= 0) & (b >= 0)
    return (valid & (a != b)).sum(axis=1), valid.sum(axis=1)


# =========================================================================== A, D
def uniqueness_metrics(prep: Prepared) -> tuple[dict, np.ndarray]:
    codes = group_codes(prep.norm, prep.fp_cols)
    counts = np.bincount(codes)
    n = prep.n
    singles = int((counts == 1).sum())
    dup_mask = counts >= 2
    dup_records = int(counts[dup_mask].sum())
    return {
        "status": "computed",
        "total_records": n,
        "unique_fingerprints": singles,                 # records whose fingerprint occurs once
        "duplicate_fingerprints": dup_records,          # records sharing a fingerprint with another record
        "duplicate_groups": int(dup_mask.sum()),
        "distinct_fingerprints": int(len(counts)),      # number of different fingerprint values
        "redundant_records": int(n - len(counts)),      # records beyond the first of each fingerprint
        "uniqueness_pct": singles / n * 100.0,
        "distinct_ratio_pct": len(counts) / n * 100.0,  # distinct / total (a different, weaker measure)
    }, codes


def collision_metrics(counts: np.ndarray, n: int) -> dict:
    dup_records = int(counts[counts >= 2].sum())
    pair_colliding = float((counts * (counts - 1)).sum())
    return {
        "status": "computed",
        "record_based_collision_rate_pct": dup_records / n * 100.0,
        "records_in_collision_groups": dup_records,
        "pairwise_collision_probability": pair_colliding / (n * (n - 1)),
        "colliding_pairs": int(pair_colliding // 2),
        "total_pairs": int(n * (n - 1) // 2),
    }


# =========================================================================== B, C
def _entropy(counts) -> float:
    c = np.asarray(counts, dtype=float)
    c = c[c > 0]
    if c.size == 0:
        return float("nan")
    p = c / c.sum()
    return max(0.0, float(-(p * np.log2(p)).sum()))


def normalized_entropy(h: float, k: int):
    if k <= 0 or math.isnan(h):
        return None
    if k == 1:
        return 0.0
    return h / math.log2(k)


def entropy_metrics(prep: Prepared, combined_counts: np.ndarray) -> dict:
    feats = []
    for c in prep.fp_cols:
        vc = prep.norm[c].value_counts().to_numpy()
        h, k = _entropy(vc), int(len(vc))
        feats.append({
            "feature": c, "entropy_bits": h, "distinct_values": k,
            "normalized_entropy": normalized_entropy(h, k),
            "missing_count": int((prep.norm[c] == MISSING).sum()),
        })
    h, k = _entropy(combined_counts), int(len(combined_counts))
    return {
        "status": "computed", "features": feats,
        "combined": {"entropy_bits": h, "distinct_values": k,
                     "normalized_entropy": normalized_entropy(h, k),
                     "max_possible_bits_for_n": math.log2(prep.n)},
    }


# =========================================================================== E
def hamming_metrics(prep: Prepared, cfg: MeasurementConfig, rng) -> dict:
    cols = prep.fp_cols
    if prep.n < MIN_RECORDS or not cols:
        return _insufficient("At least 2 records and 1 fingerprint field are required.")
    matrix = code_matrix(prep.norm, cols)
    exact = prep.n <= cfg.max_exact_records
    m = prep.n * (prep.n - 1) // 2 if exact else cfg.sampled_pairs
    s_raw = s_norm = 0.0
    done = skipped = 0
    lo = nlo = math.inf
    hi = nhi = -math.inf
    for i, j in _pair_stream(prep.n, exact, m, rng, cfg.pair_chunk):
        diff, comp = _pair_distance(matrix, i, j)
        ok = comp > 0
        skipped += int((~ok).sum())
        if not ok.any():
            continue
        d, c = diff[ok].astype(float), comp[ok].astype(float)
        nd = d / c
        s_raw += d.sum(); s_norm += nd.sum(); done += d.size
        lo, hi = min(lo, d.min()), max(hi, d.max())
        nlo, nhi = min(nlo, nd.min()), max(nhi, nd.max())
    if done == 0:
        return _insufficient("No pair of records has a comparable field.")
    return {
        "status": "computed", "exact": exact, "pairs_evaluated": done,
        "pairs_without_comparable_fields": skipped, "fields_used": list(cols),
        "average_distance": s_raw / done, "min_distance": float(lo), "max_distance": float(hi),
        "normalized_average": s_norm / done, "normalized_min": float(nlo), "normalized_max": float(nhi),
        "note": None if exact else
        f"Dataset has more than {cfg.max_exact_records} records: statistics use {done} seeded random pairs; "
        "min/max are the extremes of that sample, not of all pairs.",
    }


# =========================================================================== F
def jaccard_metrics(prep: Prepared, cfg: MeasurementConfig, rng) -> dict:
    cols = prep.set_cols
    if not cols:
        return {"status": "not_applicable", "features": [],
                "message": "No set-valued fingerprint feature in this dataset (cells are single categorical "
                           "values), so Jaccard similarity does not apply. It is not applied to ordinary "
                           "categorical fields such as userAgent or platform."}
    if prep.n < MIN_RECORDS:
        return _insufficient("At least 2 records are required.", features=[])
    exact = prep.n <= cfg.max_exact_set_records
    m = prep.n * (prep.n - 1) // 2 if exact else cfg.sampled_set_pairs
    feats = []
    for c in cols:
        sets = [parse_set(v, cfg.set_delimiter) for v in prep.raw[c]]
        total = skipped = both_empty = 0
        acc = 0.0
        for i, j in _pair_stream(prep.n, exact, m, rng, cfg.pair_chunk):
            for a, b in zip(i.tolist(), j.tolist()):
                sa, sb = sets[a], sets[b]
                if sa is None or sb is None:
                    skipped += 1
                    continue
                if not sa and not sb:
                    both_empty += 1
                acc += jaccard(sa, sb)
                total += 1
        feats.append({
            "feature": c, "pairs_evaluated": total, "pairs_skipped_missing": skipped,
            "pairs_both_empty": both_empty, "exact": exact,
            "mean_similarity": acc / total if total else None,
            "mean_distance": 1 - acc / total if total else None,
        })
    return {"status": "computed", "features": feats}


# =========================================================================== H, I labels
def _timestamps(raw: pd.DataFrame) -> pd.Series:
    """Seconds since epoch (NaN when unparseable) - safe to sort on."""
    if "timestamp" not in raw.columns:
        return pd.Series(np.nan, index=raw.index)
    ts = pd.to_datetime(raw["timestamp"], errors="coerce", utc=True, format="ISO8601")
    return (ts - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()


def label_info(prep: Prepared):
    """Find a reliable browser/session label column. Returns (info|None, reason)."""
    reason = "No browser_id or session_id column exists in the selected data."
    for col in ("browser_id", "session_id"):
        if col not in prep.raw.columns:
            continue
        lab = prep.raw[col].map(lambda v: None if _is_blank(v) else str(v).strip())
        labelled = lab.notna().to_numpy()
        if not labelled.any():
            reason = f"'{col}' exists but contains no values."
            continue
        sizes = lab[labelled].value_counts()
        repeated = int((sizes >= 2).sum())
        if repeated == 0:
            reason = (f"'{col}' has {len(sizes)} label(s) but every label occurs only once, "
                      "so there are no repeated observations.")
            continue
        return {
            "column": col, "labels": lab.to_numpy(dtype=object), "labelled": labelled,
            "n_labelled": int(labelled.sum()), "coverage_pct": float(labelled.mean() * 100),
            "n_labels": int(len(sizes)), "n_repeated_labels": repeated, "max_observations": int(sizes.max()),
        }, ""
    return None, reason


# =========================================================================== H
def _percentile_ci(values: np.ndarray, level: float):
    a = (1 - level) / 2
    lo, hi = np.percentile(values, [a * 100, (1 - a) * 100])
    return float(lo), float(hi)


def stability_metrics(prep: Prepared, info, reason: str, cfg: MeasurementConfig, rng) -> dict:
    if info is None:
        return _insufficient(INSUFFICIENT_STABILITY, reason=reason, detail="Stability needs repeated "
                             "observations of the same browser. Fingerprints are never used to guess which "
                             "records belong to the same browser.")
    idx = np.flatnonzero(info["labelled"])
    order = pd.DataFrame({"i": idx, "label": info["labels"][idx], "ts": _timestamps(prep.raw).to_numpy()[idx]})
    order = order.sort_values(["label", "ts", "i"], kind="stable")
    i_arr, l_arr = order["i"].to_numpy(), order["label"].to_numpy()
    same = l_arr[1:] == l_arr[:-1]
    a, b, who = i_arr[:-1][same], i_arr[1:][same], l_arr[1:][same]

    out = {"status": "computed", "label_column": info["column"], "n_browsers_total": info["n_labels"],
           "n_browsers_with_repeats": info["n_repeated_labels"], "coverage_pct": info["coverage_pct"],
           "scope": "session-level (short-term)" if info["column"] == "session_id" else "browser-level"}

    def summarise(cols):
        matrix = code_matrix(prep.norm, cols)
        diff, comp = _pair_distance(matrix, a, b)
        ok = comp > 0
        stable = (diff[ok] == 0).astype(float)
        per = pd.Series(stable).groupby(pd.Series(who[ok]).to_numpy()).mean()
        return {
            "transitions": int(ok.sum()),
            "pooled_stability_pct": float(stable.mean() * 100) if stable.size else None,
            "mean_browser_stability_pct": float(per.mean() * 100) if len(per) else None,
            "mean_normalized_hamming": float((diff[ok] / comp[ok]).mean()) if ok.any() else None,
            "per_browser": per.to_numpy(),
        }

    fp = summarise(prep.fp_cols)
    allsig = summarise(prep.fp_cols + prep.volatile_cols)
    out["fingerprint"] = {k: v for k, v in fp.items() if k != "per_browser"}
    out["all_signals_including_volatile"] = {k: v for k, v in allsig.items() if k != "per_browser"}

    feats = []
    for c in prep.fp_cols + prep.volatile_cols:
        codes = _factor(prep.norm[c])
        va, vb = codes[a], codes[b]
        valid = (va >= 0) & (vb >= 0)
        compared = int(valid.sum())
        changed = int((valid & (va != vb)).sum())
        feats.append({"feature": c, "volatile": c in VOLATILE_COLUMNS, "transitions_compared": compared,
                      "change_rate_pct": changed / compared * 100 if compared else None,
                      "stability_pct": (1 - changed / compared) * 100 if compared else None})
    out["features"] = feats

    per_browser = fp["per_browser"]
    cautions = []
    if len(per_browser) < 5:
        cautions.append(f"Only {len(per_browser)} browser(s) with repeated observations: descriptive only, "
                        "not generalisable.")
    out["cautions"] = cautions
    if len(per_browser) >= cfg.min_units_for_ci:
        B = min(cfg.bootstrap_iterations, 5000)
        means = np.array([per_browser[rng.integers(0, len(per_browser), len(per_browser))].mean() * 100
                          for _ in range(B)])
        lo, hi = _percentile_ci(means, cfg.confidence_level)
        out["ci"] = {"low": lo, "high": hi, "level": cfg.confidence_level, "n_resamples": B,
                     "unit": "browser"}
    else:
        out["ci"] = {"status": "not_computed",
                     "message": f"Confidence interval needs at least {cfg.min_units_for_ci} browsers with "
                                f"repeated observations; have {len(per_browser)}."}
    return out


# =========================================================================== I
def distinguishability_metrics(prep: Prepared, info, reason: str, cfg: MeasurementConfig, rng) -> dict:
    if info is None or info["n_labels"] < 2:
        why = reason if info is None else "Only one browser/session label is present, so there are no inter-browser pairs."
        return _insufficient(NO_DISTINGUISHABILITY, reason=why)
    rows = np.flatnonzero(info["labelled"])
    codes_lab, _ = pd.factorize(pd.Series(info["labels"][rows]))
    matrix = code_matrix(prep.norm, prep.fp_cols)[rows]
    N = len(rows)
    order = np.argsort(codes_lab, kind="stable")
    bounds = np.flatnonzero(np.diff(codes_lab[order])) + 1
    groups = [g for g in np.split(order, bounds) if len(g) >= 2]
    sizes = np.array([len(g) for g in groups], dtype=float)
    pair_counts = sizes * (sizes - 1) / 2
    total_intra = int(pair_counts.sum())

    # ---- intra-browser pairs (exact when small, otherwise sampled proportionally)
    intra_exact = total_intra <= cfg.sampled_pairs
    ii_parts, jj_parts = [], []
    if intra_exact:
        for g in groups:
            x, y = np.triu_indices(len(g), 1)
            ii_parts.append(g[x]); jj_parts.append(g[y])
    else:
        picks = np.bincount(rng.choice(len(groups), size=cfg.sampled_pairs, p=pair_counts / pair_counts.sum()),
                            minlength=len(groups))
        for g, c in zip(groups, picks):
            if c:
                x = rng.integers(0, len(g), c); y = rng.integers(0, len(g) - 1, c)
                ii_parts.append(g[x]); jj_parts.append(g[y + (y >= x)])
    di, dc = _pair_distance(matrix, np.concatenate(ii_parts), np.concatenate(jj_parts))
    intra = (di / np.maximum(dc, 1))[dc > 0]

    # ---- inter-browser pairs
    total_pairs = N * (N - 1) // 2
    inter_exact = total_pairs <= cfg.max_exact_records * (cfg.max_exact_records - 1) // 2
    parts = []
    if inter_exact:
        for i, j in _all_pairs_chunks(N, cfg.pair_chunk):
            keep = codes_lab[i] != codes_lab[j]
            d, c = _pair_distance(matrix, i[keep], j[keep])
            parts.append((d / np.maximum(c, 1))[c > 0])
    else:
        got, attempts = 0, 0
        while got < cfg.sampled_pairs and attempts < 50:
            for i, j in _random_pairs(N, cfg.sampled_pairs, rng, cfg.pair_chunk):
                keep = codes_lab[i] != codes_lab[j]
                d, c = _pair_distance(matrix, i[keep], j[keep])
                part = (d / np.maximum(c, 1))[c > 0]
                parts.append(part); got += part.size
            attempts += 1
    inter = np.concatenate(parts) if parts else np.array([])
    if intra.size == 0 or inter.size == 0:
        return _insufficient(NO_DISTINGUISHABILITY, reason="No comparable intra- or inter-browser pairs.")

    srt = np.sort(intra)
    less = np.searchsorted(srt, inter, side="left")
    leq = np.searchsorted(srt, inter, side="right")
    auc = float(((less + 0.5 * (leq - less)) / srt.size).mean())
    return {
        "status": "computed", "label_column": info["column"],
        "n_browsers": info["n_labels"], "n_browsers_with_repeats": info["n_repeated_labels"],
        "intra_pairs": int(intra.size), "inter_pairs": int(inter.size),
        "intra_exact": intra_exact, "inter_exact": inter_exact,
        "intra_mean_normalized_hamming": float(intra.mean()), "intra_median": float(np.median(intra)),
        "inter_mean_normalized_hamming": float(inter.mean()), "inter_median": float(np.median(inter)),
        "separation": float(inter.mean() - intra.mean()),
        "auc_inter_greater_than_intra": auc,
        "inter_pairs_not_above_max_intra_pct": float((inter <= intra.max()).mean() * 100),
        "ci": {"status": "not_computed", "message": "Confidence interval for distinguishability is not computed."},
        "cautions": ([f"Only {info['n_repeated_labels']} browser(s) with repeats: descriptive only."]
                     if info["n_repeated_labels"] < 5 else []),
    }


# =========================================================================== J
def bootstrap_metrics(prep: Prepared, cfg: MeasurementConfig, rng, base_codes, contribution, info) -> dict:
    """Bootstrap CIs for uniqueness, collision rate, combined entropy and feature contribution.

    Clone-aware: in a resample a record is 'unique' when its fingerprint group contains exactly
    ONE distinct ORIGINAL record. A naive resample would treat the duplicated copies of one record
    as colliding with each other and make even an all-distinct dataset look ~37% unique.
    Cluster mode (browser/session labels with repeats): whole browsers are resampled together."""
    n = prep.n
    cluster_mode = info is not None
    if cluster_mode:
        lab = info["labels"]
        base = pd.factorize(pd.Series(lab).where(pd.Series(info["labelled"])))[0]
        unl = base < 0  # unlabelled rows are their own cluster
        cl = base.copy()
        cl[unl] = base.max() + 1 + np.arange(int(unl.sum()))
        order = np.argsort(cl, kind="stable")
        members = np.split(order, np.flatnonzero(np.diff(cl[order])) + 1)
        units, unit_name = len(members), "browser/session (rows of a browser resampled together)"
    else:
        units, unit_name = n, "record (assumed independent)"
    if units < cfg.min_units_for_ci:
        return _insufficient(
            f"Confidence intervals need at least {cfg.min_units_for_ci} independent "
            f"{'browsers/sessions' if cluster_mode else 'records'}; have {units}.", unit=unit_name)

    scen = {"__base__": base_codes}
    for r in contribution["results"]:
        remaining = [c for c in prep.fp_cols if c not in r["Columns"]]
        scen[r["Category Removed"]] = group_codes(prep.norm, remaining)
    K = {k: int(v.max()) + 1 for k, v in scen.items()}
    B = int(min(cfg.bootstrap_iterations, max(100, cfg.bootstrap_budget // (max(n, 1) * (len(scen) + 5)))))
    B = max(B, 1)
    uni = {k: np.empty(B) for k in scen}
    ent = np.empty(B)
    base_k = K["__base__"]
    for b in range(B):
        if cluster_mode:
            pick = rng.integers(0, len(members), len(members))
            idx = np.concatenate([members[p] for p in pick])
        else:
            idx = rng.integers(0, n, n)
        rows, w = np.unique(idx, return_counts=True)
        for k, codes in scen.items():
            cu = codes[rows]
            g = np.bincount(cu, minlength=K[k])
            uni[k][b] = w[g[cu] == 1].sum() / idx.size * 100.0
        ent[b] = _entropy(np.bincount(base_codes[idx], minlength=base_k))

    level = cfg.confidence_level

    def interval(samples, point):
        lo, hi = _percentile_ci(samples, level)
        if cfg.ci_method == "basic":  # first-order bias correction: 2*theta - quantiles
            lo, hi = 2 * point - hi, 2 * point - lo
        return {"low": float(lo), "high": float(hi), "level": level, "n_resamples": B,
                "method": cfg.ci_method, "bootstrap_bias": float(samples.mean() - point),
                "contains_point_estimate": bool(lo <= point <= hi)}

    base_point = float((np.bincount(base_codes) == 1).sum() / n * 100.0)
    out = {"status": "computed", "unit": unit_name, "units": units, "n_resamples": B, "level": level,
           "method": cfg.ci_method,
           "uniqueness_pct": interval(uni["__base__"], base_point),
           "combined_entropy_bits": interval(ent, _entropy(np.bincount(base_codes))),
           "contribution": {}}
    ul = interval(100.0 - uni["__base__"], 100.0 - base_point)
    out["collision_rate_record_pct"] = ul
    for r in contribution["results"]:
        name = r["Category Removed"]
        out["contribution"][name] = interval(uni["__base__"] - uni[name], r["Baseline Uniqueness (%)"]
                                             - r["Uniqueness After Removal (%)"])
    out["caveats"] = [
        "EXPERIMENTAL and NOT validated: in simulation (tests/validate_bootstrap_coverage.py) nominal 95% intervals "
        "for uniqueness covered the true value only 12-81% of the time, for plug-in entropy 0-28%, and for feature "
        "contribution 24-100% depending on the population. Treat as a rough spread of the resamples, not a confidence interval.",
        "No hypothesis tests were run; these intervals do not establish statistical significance.",
    ]
    return out


# =========================================================================== orchestration
def provenance_summary(df: pd.DataFrame) -> dict:
    if "origin_class" in df.columns and len(df):
        counts = {str(k): int(v) for k, v in df["origin_class"].value_counts().items()}
    else:
        counts = {"unverified": int(len(df))} if len(df) else {}
    present = {k for k, v in counts.items() if v}
    if present == {"real"}:
        kind = "real"
    elif present == {"synthetic"}:
        kind = "synthetic"
    elif present == {"unverified"} or not present:
        kind = "unverified"
    else:
        kind = "mixed"
    origins = {str(k): int(v) for k, v in df["origin"].value_counts().items()} if "origin" in df.columns and len(df) else {}
    sources = {str(k): int(v) for k, v in df["source_dataset"].value_counts().items()} if "source_dataset" in df.columns and len(df) else {}
    return {"data_kind": kind, "provenance": counts, "origin_counts": origins, "source_datasets": sources}


def _warnings_for(prov: dict, n: int) -> list[str]:
    w = []
    p = prov["provenance"]
    if p.get("synthetic"):
        if prov["data_kind"] == "synthetic":
            w.append("ALL records are synthetic. These numbers test the pipeline only and are NOT real-world browser measurements.")
        else:
            w.append(f"{p['synthetic']} of {n} records are synthetic and are mixed with other data. This result must NOT be "
                     "presented as a real-world browser measurement; re-run with origin_filter=real for real data only.")
    if p.get("unverified"):
        w.append(f"{p['unverified']} record(s) have an unverified origin (not labelled real or synthetic).")
    if prov["data_kind"] == "real" and n < 30:
        w.append(f"Only {n} real record(s): results are descriptive and cannot support general claims about browser uniqueness.")
    return w


def run_measurements(df: pd.DataFrame, *, dataset_name: str = "dataset", source_info: dict | None = None,
                     config: MeasurementConfig | None = None, run_id: str | None = None,
                     measured_at: str | None = None) -> dict:
    """Compute every metric for `df` and return a JSON-safe report (including flat `rows`)."""
    cfg = config or MeasurementConfig()
    rng = np.random.default_rng(cfg.seed)
    prep = prepare(df, cfg)
    prov = provenance_summary(prep.raw)
    report = {
        "run_id": run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6],
        "measured_at": measured_at or datetime.now(timezone.utc).isoformat(),
        "dataset": dataset_name, "record_count": prep.n, **prov,
        "warnings": list((source_info or {}).get("warnings", [])) + _warnings_for(prov, prep.n),
        "source_info": source_info or {},
        "config": asdict(cfg) | {"set_valued_features": list(cfg.set_valued_features)},
        "feature_columns": prep.fp_cols, "ignored_volatile_columns": prep.volatile_cols,
        "dropped_all_missing_columns": prep.dropped_empty, "set_valued_columns": prep.set_cols,
        "definitions": DEFINITIONS,
    }
    if prep.dropped_empty:
        report["warnings"].append(f"Column(s) {prep.dropped_empty} contain no values in this selection and were ignored.")
    m = {}
    if prep.n < MIN_RECORDS or not prep.fp_cols:
        msg = (f"At least {MIN_RECORDS} records are required; found {prep.n}." if prep.n < MIN_RECORDS
               else "The selection contains no fingerprint columns.")
        for key in ("uniqueness", "collision", "entropy", "hamming", "jaccard", "feature_contribution",
                    "stability", "distinguishability", "confidence_intervals"):
            m[key] = _insufficient(msg)
        m["stability"] = _insufficient(INSUFFICIENT_STABILITY, reason=msg)
        m["distinguishability"] = _insufficient(NO_DISTINGUISHABILITY, reason=msg)
        report["metrics"] = m
        report["rows"] = metrics_to_rows(report)
        return _clean(report)

    m["uniqueness"], codes = uniqueness_metrics(prep)
    counts = np.bincount(codes)
    m["collision"] = collision_metrics(counts, prep.n)
    m["entropy"] = entropy_metrics(prep, counts)
    m["hamming"] = hamming_metrics(prep, cfg, rng)
    m["jaccard"] = jaccard_metrics(prep, cfg, rng)
    contribution = compute_feature_contribution(prep.norm, fingerprint_columns=prep.fp_cols, normalised=True)
    m["feature_contribution"] = {"status": "computed", **contribution}
    info, reason = label_info(prep)
    m["stability"] = stability_metrics(prep, info, reason, cfg, rng)
    m["distinguishability"] = distinguishability_metrics(prep, info, reason, cfg, rng)
    if cfg.experimental_intervals:
        m["confidence_intervals"] = bootstrap_metrics(prep, cfg, rng, codes, contribution, info)
    else:
        m["confidence_intervals"] = {
            "status": "not_computed",
            "message": "No confidence interval is reported for uniqueness, collision rate, entropy or feature "
                       "contribution: bootstrap intervals for these statistics were found to under-cover badly in "
                       "simulation, so reporting them as 95% intervals would be misleading. (Stability intervals are "
                       "reported when >= 20 browsers have repeated observations.)"}
    m["labels"] = ({k: info[k] for k in ("column", "n_labelled", "coverage_pct", "n_labels",
                                         "n_repeated_labels", "max_observations")}
                   if info else {"available": False, "reason": reason})
    if info is None:
        report["warnings"].append(
            "Records are not linked to browsers (no usable browser_id/session_id). Repeated scans of the same "
            "browser are therefore counted as collisions, so uniqueness here can understate how unique "
            "individual browsers are.")
    report["metrics"] = m
    report["rows"] = metrics_to_rows(report)
    return _clean(report)


# =========================================================================== flatten to CSV rows
def _round(v, places=6):
    return None if v is None else round(float(v), places)


def metrics_to_rows(report: dict) -> list[dict]:
    """One row per metric value, ready for measurement_results.csv."""
    base = {"run_id": report["run_id"], "measured_at": report["measured_at"], "dataset": report["dataset"],
            "data_kind": report["data_kind"],
            "data_provenance": ";".join(f"{k}:{v}" for k, v in sorted(report["provenance"].items())),
            "record_count": report["record_count"]}
    m = report["metrics"]
    ci = m.get("confidence_intervals", {})
    rows = []

    def add(metric, scope, value, unit="", status="computed", note="", ci_part=None):
        row = {**base, "metric": metric, "scope": scope, "value": _round(value) if isinstance(value, (int, float, np.floating)) else value,
               "unit": unit, "ci_low": "", "ci_high": "", "ci_level": "", "n_resamples": "",
               "status": status, "note": note}
        if value is None and status == "computed":
            row["status"] = "not_defined"
        if ci_part and "low" in ci_part:
            row.update(ci_low=_round(ci_part["low"]), ci_high=_round(ci_part["high"]),
                       ci_level=ci_part["level"], n_resamples=ci_part["n_resamples"])
        rows.append(row)

    def block(key, metrics, scope="overall"):
        blk = m[key]
        if blk.get("status") == "insufficient_data":
            for name in metrics:
                add(name, scope, None, status="insufficient_data", note=blk.get("message", ""))
            return False
        return True

    u = m["uniqueness"]
    if block("uniqueness", ["uniqueness_pct", "total_records", "unique_fingerprints", "duplicate_fingerprints"]):
        ciu = ci.get("uniqueness_pct") if ci.get("status") == "computed" else None
        add("uniqueness_pct", "overall", u["uniqueness_pct"], "%", ci_part=ciu, note=DEFINITIONS["uniqueness_pct"])
        add("total_records", "overall", u["total_records"], "records")
        add("unique_fingerprints", "overall", u["unique_fingerprints"], "records",
            note="Records whose fingerprint occurs exactly once.")
        add("duplicate_fingerprints", "overall", u["duplicate_fingerprints"], "records",
            note=f"Records sharing a fingerprint with another record, in {u['duplicate_groups']} duplicate group(s).")
        add("distinct_fingerprints", "overall", u["distinct_fingerprints"], "values")
    c = m["collision"]
    if block("collision", ["collision_rate_record_pct", "pairwise_collision_probability"]):
        cic = ci.get("collision_rate_record_pct") if ci.get("status") == "computed" else None
        add("collision_rate_record_pct", "overall", c["record_based_collision_rate_pct"], "%", ci_part=cic,
            note=DEFINITIONS["collision_rate_record_pct"])
        add("pairwise_collision_probability", "overall", c["pairwise_collision_probability"], "probability",
            note=DEFINITIONS["pairwise_collision_probability"])
    e = m["entropy"]
    if block("entropy", ["shannon_entropy_bits", "normalized_entropy"]):
        cie = ci.get("combined_entropy_bits") if ci.get("status") == "computed" else None
        comb = e["combined"]
        add("shannon_entropy_bits", "combined_fingerprint", comb["entropy_bits"], "bits", ci_part=cie,
            note=DEFINITIONS["shannon_entropy_bits"])
        add("normalized_entropy", "combined_fingerprint", comb["normalized_entropy"], "ratio 0-1",
            note=DEFINITIONS["normalized_entropy"])
        add("entropy_upper_bound_bits", "combined_fingerprint", comb["max_possible_bits_for_n"], "bits",
            note="log2(N): the most any fingerprint distribution over N records can show.")
        for f in e["features"]:
            add("shannon_entropy_bits", f["feature"], f["entropy_bits"], "bits",
                note=f"{f['distinct_values']} distinct value(s); {f['missing_count']} missing")
            add("normalized_entropy", f["feature"], f["normalized_entropy"], "ratio 0-1",
                note="k=1 -> 0 (constant feature)" if f["distinct_values"] == 1 else "")
    h = m["hamming"]
    if block("hamming", ["hamming_average", "hamming_min", "hamming_max", "hamming_normalized_average"]):
        note = h["note"] or f"exact over {h['pairs_evaluated']} pairs; fields: {len(h['fields_used'])}"
        add("hamming_average", "all_pairs", h["average_distance"], "differing fields", note=note)
        add("hamming_min", "all_pairs", h["min_distance"], "differing fields", note=note)
        add("hamming_max", "all_pairs", h["max_distance"], "differing fields", note=note)
        add("hamming_normalized_average", "all_pairs", h["normalized_average"], "ratio 0-1", note=note)
        add("hamming_normalized_min", "all_pairs", h["normalized_min"], "ratio 0-1", note=note)
        add("hamming_normalized_max", "all_pairs", h["normalized_max"], "ratio 0-1", note=note)
    j = m["jaccard"]
    if j["status"] != "computed":
        add("jaccard_mean_similarity", "overall", None, status=j["status"], note=j.get("message", ""))
    else:
        for f in j["features"]:
            add("jaccard_mean_similarity", f["feature"], f["mean_similarity"], "ratio 0-1",
                note=f"{f['pairs_evaluated']} pairs; {f['pairs_both_empty']} both-empty counted as 1.0; "
                     f"{f['pairs_skipped_missing']} skipped (missing)")
    fc = m["feature_contribution"]
    if block("feature_contribution", ["feature_contribution_pp"]):
        for r in fc["results"]:
            name = r["Category Removed"]
            cif = ci.get("contribution", {}).get(name) if ci.get("status") == "computed" else None
            add("feature_contribution_pp", name, r["Contribution (percentage points)"], "percentage points",
                ci_part=cif, note=f"baseline {r['Baseline Uniqueness (%)']}% -> {r['Uniqueness After Removal (%)']}% "
                                  f"after removing {', '.join(r['Columns'])}")
    s = m["stability"]
    if s["status"] != "computed":
        add("stability", "overall", None, status="insufficient_data", note=s["message"] + " " + s.get("reason", ""))
    else:
        fpst = s["fingerprint"]
        add("stability_mean_browser_pct", "fingerprint", fpst["mean_browser_stability_pct"], "%",
            ci_part=s["ci"] if "low" in s["ci"] else None,
            note=f"{s['scope']}; {s['n_browsers_with_repeats']} browser(s) with repeats; {fpst['transitions']} transitions")
        add("stability_pooled_pct", "fingerprint", fpst["pooled_stability_pct"], "%")
        add("stability_pooled_pct", "all_signals_incl_volatile",
            s["all_signals_including_volatile"]["pooled_stability_pct"], "%")
        for f in s["features"]:
            add("stability_feature_pct", f["feature"], f["stability_pct"], "%",
                note="volatile" if f["volatile"] else "")
    d = m["distinguishability"]
    if d["status"] != "computed":
        add("distinguishability", "overall", None, status="insufficient_data", note=d["message"] + " " + d.get("reason", ""))
    else:
        for name, key in (("intra_browser_mean_normalized_hamming", "intra_mean_normalized_hamming"),
                          ("inter_browser_mean_normalized_hamming", "inter_mean_normalized_hamming"),
                          ("separation", "separation"), ("auc_inter_greater_than_intra", "auc_inter_greater_than_intra")):
            add(f"distinguishability_{name}", "overall", d[key], "ratio 0-1",
                note=f"{d['intra_pairs']} intra / {d['inter_pairs']} inter pairs")
    if ci.get("status") in ("insufficient_data", "not_computed"):
        add("confidence_intervals", "uniqueness/collision/entropy/contribution", None, status=ci["status"], note=ci["message"])
    return rows


# =========================================================================== convenience
def measure_selection(dataset_ids: list[str], origin_filter: str = "all", config: MeasurementConfig | None = None,
                      save: bool = True) -> dict:
    """Load the selected datasets (any number, any size), measure, optionally append to measurement_results.csv."""
    import storage_module as store  # local import keeps the core metrics free of file I/O
    df, info = store.load_selection(dataset_ids, origin_filter)
    name = "+".join(i["id"] for i in info["datasets"]) or "+".join(dataset_ids)
    if origin_filter != "all":
        name += f"[origin={origin_filter}]"
    report = run_measurements(df, dataset_name=name, source_info=info, config=config)
    if save and report["record_count"] > 0:
        store.append_measurement_rows(report["rows"])
    report["saved"] = bool(save and report["record_count"] > 0)
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run WHOOPS measurements from the command line.")
    parser.add_argument("--datasets", nargs="+", default=["collected"], help="dataset ids (default: collected)")
    parser.add_argument("--origin-filter", default="all", choices=["all", "real", "synthetic", "unverified"])
    parser.add_argument("--save", action="store_true", help="append results to measurement_results.csv")
    args = parser.parse_args()
    try:
        rep = measure_selection(args.datasets, args.origin_filter, save=args.save)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"Measurement failed: {exc}")
    print(f"Dataset: {rep['dataset']} | records: {rep['record_count']} | data kind: {rep['data_kind']} {rep['provenance']}")
    for w in rep["warnings"]:
        print("WARNING:", w)
    print(pd.DataFrame(rep["rows"])[["metric", "scope", "value", "unit", "ci_low", "ci_high", "status"]].to_string(index=False))
