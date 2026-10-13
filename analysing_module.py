"""Pandas feature-ablation analysis for WHOOPS."""
from pathlib import Path
import pandas as pd

CATEGORIES = {
    "Navigator": [
        "userAgent", "language", "platform",
        "hardwareConcurrency", "touchPoints", "cookieEnabled",
    ],
    "Screen": [
        "screenWidth", "screenHeight", "colorDepth",
        "viewportWidth", "viewportHeight", "devicePixelRatio",
    ],
    "Canvas": ["canvasChecksum"],
    "WebGL": ["webglAvailable"],
    "Timezone": ["timezone"],
}

# Columns that describe the record, not the browser. Never part of a fingerprint.
METADATA_COLUMNS = {"timestamp", "origin"}
# Window-size signals change when the user resizes/rotates the window, so two scans
# of the SAME browser can differ. They stay in the data but are not used to decide
# whether a browser is distinguishable (otherwise resizing a window fakes uniqueness).
VOLATILE_COLUMNS = {"viewportWidth", "viewportHeight"}

def _norm(series: pd.Series) -> pd.Series:
    """Normalise values to comparable strings (1 == 1.0, True == 'True')."""
    if pd.api.types.is_bool_dtype(series):
        return series.astype(str)
    num = pd.to_numeric(series, errors="coerce")
    if num.notna().sum() == series.notna().sum() and series.notna().any():
        return num.map(lambda v: "nan" if pd.isna(v) else repr(float(v)))
    return series.fillna("nan").astype(str)

def stable_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in METADATA_COLUMNS | VOLATILE_COLUMNS]

def calculate_uniqueness(data: pd.DataFrame, columns: list[str]) -> float:
    """Percentage of rows whose selected feature combination occurs once."""
    if len(data) == 0:
        return 0.0
    if not columns:
        return 0.0
    unique_mask = ~data[columns].duplicated(keep=False)
    return float(unique_mask.sum() / len(data) * 100.0)

def build_combined(collected_path: str | Path, sample_path: str | Path) -> pd.DataFrame:
    """Sample rows (keeping their origin label) plus collected rows labelled 'collected'."""
    cpath, spath = Path(collected_path), Path(sample_path)
    if not spath.exists():
        raise FileNotFoundError(f"Dataset not found: {spath.name}")
    if not cpath.exists() or cpath.stat().st_size == 0:
        raise FileNotFoundError("No collected records yet. Collect at least one first.")
    sample, coll = pd.read_csv(spath), pd.read_csv(cpath)
    if "origin" not in sample.columns:
        sample["origin"] = "sample"
    coll = coll.reindex(columns=sample.columns)
    coll["origin"] = "collected"
    return pd.concat([sample, coll], ignore_index=True)

def run_analysis(csv_path) -> dict:
    """Run the original feature-ablation method and return JSON-safe results.

    `csv_path` may be a CSV path or an already-loaded DataFrame (combined data)."""
    if isinstance(csv_path, pd.DataFrame):
        df = csv_path.copy()
    else:
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"Dataset not found: {path.name}")
        df = pd.read_csv(path)
    if len(df) < 2:
        raise ValueError(
            f"At least 2 records are required for meaningful uniqueness analysis; found {len(df)}."
        )

    origin_counts = (
        {str(k): int(v) for k, v in df["origin"].fillna("unknown").value_counts().items()}
        if "origin" in df.columns else {}
    )
    all_fp_columns = [col for col in df.columns if col not in METADATA_COLUMNS]
    fingerprint_columns = [col for col in all_fp_columns if col not in VOLATILE_COLUMNS]
    if not fingerprint_columns:
        raise ValueError("The dataset contains no fingerprint columns.")

    for category, columns in CATEGORIES.items():
        missing = [col for col in columns if col not in df.columns]
        if missing:
            raise ValueError(f"{category} is missing columns: {missing}")

    assigned_columns = [col for columns in CATEGORIES.values() for col in columns]
    unassigned = [col for col in all_fp_columns if col not in assigned_columns]
    if unassigned:
        raise ValueError(
            "Assign these fingerprint columns to a category before analysis: "
            f"{unassigned}"
        )

    # Compare on normalised strings so 1 vs 1.0 or True vs "True" never splits a group.
    df = df.copy()
    for col in all_fp_columns:
        df[col] = _norm(df[col])

    original_uniqueness = calculate_uniqueness(df, fingerprint_columns)
    collected_stats = None
    if "origin" in df.columns and (df["origin"] == "collected").any():
        dup = df[fingerprint_columns].duplicated(keep=False)
        is_c = df["origin"] == "collected"
        n_c = int(is_c.sum())
        collected_stats = {
            "collected_rows": n_c,
            "collected_unique_rows": int((~dup & is_c).sum()),
            "collected_uniqueness": round(float((~dup & is_c).sum() / n_c * 100), 2),
        }
    original_unique_rows = int((~df[fingerprint_columns].duplicated(keep=False)).sum())
    results = []

    for category, all_cat_columns in CATEGORIES.items():
        removed_columns = [c for c in all_cat_columns if c not in VOLATILE_COLUMNS]
        remaining_columns = [
            col for col in fingerprint_columns if col not in removed_columns
        ]
        reduced_uniqueness = calculate_uniqueness(df, remaining_columns)
        reduced_unique_rows = int((~df[remaining_columns].duplicated(keep=False)).sum()) if remaining_columns else 0
        contribution = original_uniqueness - reduced_uniqueness
        alone_uniqueness = calculate_uniqueness(df, removed_columns)
        distinct_category_values = int(df[removed_columns].fillna("<missing>").astype(str).drop_duplicates().shape[0])
        results.append({
            "Category Removed": category,
            "Uniqueness After Removal (%)": round(reduced_uniqueness, 2),
            "Uniqueness Alone (%)": round(alone_uniqueness, 2),
            "Contribution (percentage points)": round(contribution, 2),
            "Unique Rows Before": original_unique_rows,
            "Unique Rows After Removal": reduced_unique_rows,
            "Distinct Category Signatures": distinct_category_values,
        })

    max_contribution = max((max(0.0, float(row["Contribution (percentage points)"])) for row in results), default=0.0)
    for row in results:
        contribution = max(0.0, float(row["Contribution (percentage points)"]))
        row["Relative Impact Ratio (%)"] = round((contribution / max_contribution * 100.0), 2) if max_contribution > 0 else 0.0
        row["Interpretation"] = (
            "Measurable uniqueness reduction when removed"
            if contribution > 0 else
            "No measured change in this dataset; this is not proof the feature is universally unimportant"
        )

    results_df = pd.DataFrame(results).sort_values(
        by="Contribution (percentage points)", ascending=False
    ).reset_index(drop=True)
    results_df.insert(0, "Rank", range(1, len(results_df) + 1))

    return {
        "rows": int(len(df)),
        "cols": int(len(df.columns)),
        "origin_counts": origin_counts,
        "collected_stats": collected_stats,
        "ignored_volatile_columns": sorted(VOLATILE_COLUMNS & set(df.columns)),
        "original_uniqueness": round(float(original_uniqueness), 2),
        "unique_rows": original_unique_rows,
        "duplicate_rows": int(len(df) - original_unique_rows),
        "diagnostic": (
            "No feature group changed measured uniqueness. The selected records may be repeated scans from the same browser, or the remaining signals may already distinguish the records. Collect a varied, consented dataset to measure cross-device effects."
            if all(float(row["Contribution (percentage points)"]) <= 0 for row in results)
            else "At least one feature group measurably changed uniqueness in this dataset."
        ),
        "results": results_df.to_dict(orient="records"),
    }

def compare_to_reference(collected_path: str | Path, reference_path: str | Path) -> dict:
    """Is the latest collected browser distinguishable within the reference (sample) data?

    The collected file alone can only say whether your own repeated scans differ from
    each other. This compares collected records against a separate reference population
    and reports how many reference rows are indistinguishable from the latest record.
    """
    cpath, rpath = Path(collected_path), Path(reference_path)
    if not cpath.exists() or cpath.stat().st_size == 0:
        raise FileNotFoundError("No collected records yet. Collect at least one first.")
    if not rpath.exists():
        raise FileNotFoundError(f"Reference dataset not found: {rpath.name}")

    coll, ref = pd.read_csv(cpath), pd.read_csv(rpath)
    cols = [c for c in stable_columns(ref) if c in coll.columns]
    missing = [c for c in stable_columns(ref) if c not in coll.columns]
    if missing:
        raise ValueError(f"Collected data is missing columns present in the reference: {missing}")
    for frame in (coll, ref):
        for c in cols:
            frame[c] = _norm(frame[c])

    latest = coll.iloc[-1]
    key = tuple(latest[cols])
    ref_keys = ref[cols].apply(tuple, axis=1)
    match_mask = ref_keys == key
    matches = int(match_mask.sum())
    origin_of_matches = (
        {str(k): int(v) for k, v in ref.loc[match_mask, "origin"].fillna("unknown").value_counts().items()}
        if "origin" in ref.columns and matches else {}
    )

    per_category = []
    for category, category_columns in CATEGORIES.items():
        use = [c for c in category_columns if c in cols]
        same = (ref[use] == latest[use].values).all(axis=1)
        per_category.append({
            "Category": category,
            "Reference rows sharing this category": int(same.sum()),
            "Share of reference (%)": round(float(same.mean() * 100), 2),
        })

    diff_counts = (ref[cols] != latest[cols].values).sum(axis=1)
    closest = int(diff_counts.min()) if len(ref) else None

    coll_fp = coll[cols].apply(tuple, axis=1)
    ref_set = set(ref_keys)
    origin_counts = None
    if "origin" in ref.columns:
        origin_counts = {str(k): int(v) for k, v in ref["origin"].fillna("unknown").value_counts().items()}

    return {
        "collected_records": int(len(coll)),
        "reference_records": int(len(ref)),
        "reference_origin_counts": origin_counts or {},
        "latest_timestamp": str(coll.iloc[-1].get("timestamp", "")),
        "matching_reference_rows": matches,
        "match_origins": origin_of_matches,
        "anonymity_set_size": matches + 1,
        "distinguishable": matches == 0,
        "closest_match_differing_signals": closest,
        "collected_fingerprints_seen_in_reference_pct": round(float(coll_fp.isin(ref_set).mean() * 100), 2),
        "per_category": per_category,
        "compared_columns": cols,
        "ignored_volatile_columns": sorted(VOLATILE_COLUMNS),
        "caveat": (
            "The reference dataset contains synthetic rows, so this shows how the pipeline behaves, "
            "not how distinguishable your browser is among real users."
            if origin_counts and origin_counts.get("synthetic") else
            "Result applies only to the reference dataset used."
        ),
    }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run WHOOPS feature-ablation analysis.")
    parser.add_argument("csv_path", nargs="?", default="data/sample_fingerprints.csv")
    args = parser.parse_args()
    try:
        output = run_analysis(args.csv_path)
        print(f"Dataset loaded: {args.csv_path}")
        print(f"Rows: {output['rows']} | Columns: {output['cols']}")
        print(f"Original uniqueness: {output['original_uniqueness']:.2f}%")
        print(pd.DataFrame(output["results"]).to_string(index=False))
    except (FileNotFoundError, ValueError, pd.errors.ParserError) as exc:
        raise SystemExit(f"Analysis failed: {exc}")
