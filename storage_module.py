"""Persistent CSV storage and dataset registry for WHOOPS.

Responsibilities
----------------
* Resolve ONE data directory (``WHOOPS_DATA_DIR`` env var, default ``./data``) so the
  app can write to a Render persistent disk without code changes.
* Provide an inter-process + inter-thread lock so concurrent requests (and several
  gunicorn workers) can never interleave CSV writes.
* Append-only CSV writers (header only for new/empty files, never truncate, match
  the existing file's header and line endings, fsync after each write).
* A dataset registry/loader: discover CSV datasets dynamically, keep the origin of
  every record, and combine datasets only on request.

Nothing here deletes or rewrites existing records.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:
    msvcrt = None

# --------------------------------------------------------------------------- paths
BASE_DIR = Path(__file__).resolve().parent
BUNDLED_DATA_DIR = BASE_DIR / "data"          # ships with the repo (sample + history)
DATA_DIR = Path(os.environ.get("WHOOPS_DATA_DIR") or BUNDLED_DATA_DIR).expanduser().resolve()

MAIN_NAME = "whoops_fingerprints.csv"          # every collected record (original schema)
NEW_NAME = "whoops_new_fingerprints.csv"       # every NEW collection, with identifiers
SAMPLE_NAME = "sample_fingerprints.csv"        # supplied sample / reference data
RESULTS_NAME = "measurement_results.csv"       # calculated measurements (append-only log)
IMPORT_DIRNAME = "imports"                     # drop extra CSV datasets here
MANIFEST_NAME = "datasets.json"                # optional per-dataset metadata

MAIN_CSV = DATA_DIR / MAIN_NAME
NEW_CSV = DATA_DIR / NEW_NAME
RESULTS_PATH = DATA_DIR / RESULTS_NAME
LOCK_PATH = DATA_DIR / ".whoops_csv.lock"

# Columns of measurement_results.csv (one row per metric value; long format).
RESULT_FIELDS = [
    "run_id", "measured_at", "dataset", "data_kind", "data_provenance", "record_count",
    "metric", "scope", "value", "unit", "ci_low", "ci_high", "ci_level",
    "n_resamples", "status", "note",
]

# Origin labels -> provenance class. Anything not listed is "unverified" so that
# synthetic and real data can never be silently confused.
REAL_ORIGINS = {"real", "real_browser", "real_world", "collected"}
SYNTHETIC_ORIGINS = {"synthetic", "simulated", "generated", "fake", "sample_synthetic"}


def origin_class(origin) -> str:
    key = str(origin).strip().lower() if origin is not None and not pd.isna(origin) else ""
    if key in REAL_ORIGINS:
        return "real"
    if key in SYNTHETIC_ORIGINS:
        return "synthetic"
    return "unverified"


# --------------------------------------------------------------------------- locking
_THREAD_LOCK = threading.RLock()
_state = threading.local()


def _lock_fd(fh) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    elif msvcrt is not None:  # pragma: no cover - Windows only
        deadline = time.time() + 60
        while True:
            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.time() > deadline:
                    raise TimeoutError("Could not obtain the WHOOPS CSV lock within 60 s.")
                time.sleep(0.05)


def _unlock_fd(fh) -> None:
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows only
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def store_lock():
    """Exclusive lock across threads AND processes. Re-entrant within one thread."""
    depth = getattr(_state, "depth", 0)
    if depth:
        _state.depth = depth + 1
        try:
            yield
        finally:
            _state.depth -= 1
        return
    with _THREAD_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOCK_PATH, "a+b") as fh:
            _lock_fd(fh)
            _state.depth = 1
            try:
                yield
            finally:
                _state.depth = 0
                _unlock_fd(fh)


# --------------------------------------------------------------------------- appending
def _read_header(path: Path) -> list[str] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("r", newline="", encoding="utf-8") as fh:
        row = next(csv.reader(fh), None)
    return row or None


def _detect_terminator(path: Path) -> str:
    """Match the existing file's line ending (the history file is LF-only)."""
    if not path.exists() or path.stat().st_size == 0:
        return "\n"
    with path.open("rb") as fh:
        head = fh.read(8192)
    return "\r\n" if b"\r\n" in head else "\n"


def _ends_with_newline(path: Path) -> bool:
    size = path.stat().st_size
    if size == 0:
        return True
    with path.open("rb") as fh:
        fh.seek(size - 1)
        return fh.read(1) in (b"\n", b"\r")


def _append_unlocked(path: Path, default_fields: list[str], rows: list[dict]) -> int:
    """Append rows. Writes the header only when the file is missing/empty.
    If the file already has a header it is respected (extra keys are ignored, missing
    keys become empty cells) so a pre-existing file is never corrupted."""
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    header = _read_header(path)
    fields = header or default_fields
    terminator = _detect_terminator(path)
    needs_header = header is None
    repair_newline = (not needs_header) and not _ends_with_newline(path)
    with path.open("a", newline="", encoding="utf-8") as fh:
        if repair_newline:
            fh.write(terminator)
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore",
                                lineterminator=terminator)
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)
        fh.flush()
        os.fsync(fh.fileno())
    return len(rows)


def append_rows(path: Path, default_fields: list[str], rows: list[dict]) -> int:
    with store_lock():
        return _append_unlocked(Path(path), default_fields, rows)


def find_recent_row(path: Path, column: str, value: str, tail_bytes: int = 262_144) -> dict | None:
    """Look for ``row[column] == value`` in the tail of an append-only CSV.
    Used for request idempotency. Caller should hold the store lock."""
    path = Path(path)
    header = _read_header(path)
    if not header or column not in header:
        return None
    size = path.stat().st_size
    with path.open("rb") as fh:
        start = max(0, size - tail_bytes)
        fh.seek(start)
        chunk = fh.read().decode("utf-8", errors="replace")
    lines = chunk.splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # first line may be cut in half
    found = None
    for row in csv.DictReader(lines, fieldnames=header):
        if row.get(column) == value:
            found = row
    return found


def ensure_storage(*, create_files: bool = True) -> dict:
    """Make the data directory usable. Safe to call repeatedly.

    * If WHOOPS_DATA_DIR points somewhere other than the bundled ./data (e.g. a Render
      disk) and the main CSV is missing/empty there, the bundled history is COPIED in
      once so earlier records are preserved. Existing files are never overwritten.
    * Creates header-only whoops_new_fingerprints.csv / measurement_results.csv when
      absent (never touches non-empty files).
    """
    actions = {"seeded": [], "created": []}
    with store_lock():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        bundled_main = BUNDLED_DATA_DIR / MAIN_NAME
        if (DATA_DIR != BUNDLED_DATA_DIR and bundled_main.exists()
                and (not MAIN_CSV.exists() or MAIN_CSV.stat().st_size == 0)):
            shutil.copyfile(bundled_main, MAIN_CSV)
            actions["seeded"].append(MAIN_NAME)
        if create_files:
            for path, fields in ((NEW_CSV, NEW_FIELDS_DEFAULT), (RESULTS_PATH, RESULT_FIELDS)):
                if not path.exists() or path.stat().st_size == 0:
                    with path.open("w", newline="", encoding="utf-8") as fh:
                        csv.DictWriter(fh, fieldnames=fields, lineterminator="\n").writeheader()
                    actions["created"].append(path.name)
    return actions


# Set by connectin_module (which owns the fingerprint field list) before first use;
# a literal default keeps this module importable on its own.
NEW_FIELDS_DEFAULT = [
    "timestamp", "userAgent", "language", "platform", "timezone",
    "screenWidth", "screenHeight", "colorDepth", "viewportWidth",
    "viewportHeight", "devicePixelRatio", "hardwareConcurrency",
    "touchPoints", "cookieEnabled", "canvasChecksum", "webglAvailable",
    "record_id", "request_id", "origin", "browser_id", "session_id",
]


# --------------------------------------------------------------------------- reading
def read_csv_safe(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Read a CSV as strings. Missing/empty files give an empty frame; malformed
    lines are skipped and reported instead of aborting the whole analysis."""
    path = Path(path)
    warnings: list[str] = []
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(), warnings
    with store_lock():
        try:
            return pd.read_csv(path, dtype=str), warnings
        except pd.errors.EmptyDataError:
            return pd.DataFrame(), warnings
        except pd.errors.ParserError:
            bad: list[str] = []
            df = pd.read_csv(path, dtype=str, engine="python",
                             on_bad_lines=lambda line: bad.append(str(line)[:80]) or None)
            warnings.append(f"{path.name}: skipped {len(bad)} malformed line(s).")
            return df, warnings


# --------------------------------------------------------------------------- registry
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _builtin_specs() -> list[dict]:
    return [
        {"id": "collected", "label": "Collected on this server", "file": MAIN_NAME,
         "default_origin": "collected", "kind": "builtin"},
        {"id": "collected_new", "label": "Newly collected (with identifiers)", "file": NEW_NAME,
         "default_origin": "collected", "kind": "builtin", "subset_of": "collected"},
        {"id": "sample", "label": "Supplied sample / reference dataset", "file": SAMPLE_NAME,
         "default_origin": "sample", "kind": "builtin"},
    ]


def _resolve(file: str) -> Path | None:
    """Locate a dataset file in the data dir, falling back to the bundled copy."""
    for root in (DATA_DIR, BUNDLED_DATA_DIR):
        candidate = (root / file).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            continue  # path traversal guard
        if candidate.exists():
            return candidate
    return None


resolve_data_file = _resolve


def discover_datasets() -> dict[str, dict]:
    """Built-ins + every CSV in <data>/imports + entries of datasets.json."""
    specs = {s["id"]: dict(s) for s in _builtin_specs()}
    import_dir = DATA_DIR / IMPORT_DIRNAME
    if import_dir.is_dir():
        for csv_path in sorted(import_dir.glob("*.csv")):
            ident = "import_" + re.sub(r"[^A-Za-z0-9_.-]", "_", csv_path.stem)[:56]
            specs.setdefault(ident, {
                "id": ident, "label": f"Imported: {csv_path.name}",
                "file": f"{IMPORT_DIRNAME}/{csv_path.name}",
                "default_origin": "imported", "kind": "imported",
            })
    manifest = DATA_DIR / MANIFEST_NAME
    if manifest.exists():
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8")).get("datasets", [])
        except (OSError, ValueError, AttributeError):
            entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not _ID_RE.match(str(entry.get("id", ""))) or not entry.get("file"):
                continue
            base = specs.get(entry["id"], {"kind": "manifest"})
            specs[entry["id"]] = {
                **base, "id": entry["id"], "file": str(entry["file"]),
                "label": entry.get("label") or base.get("label") or entry["id"],
                "default_origin": entry.get("origin") or base.get("default_origin") or "imported",
                "kind": base.get("kind", "manifest"),
                **({"subset_of": entry["subset_of"]} if entry.get("subset_of") else {}),
            }
    return specs


_count_cache: dict = {}


def describe_datasets() -> list[dict]:
    """Registry listing with record counts and provenance, for the API/UI."""
    out = []
    for spec in discover_datasets().values():
        path = _resolve(spec["file"])
        info = {"id": spec["id"], "label": spec["label"], "file": spec["file"], "kind": spec["kind"],
                "available": path is not None, "records": 0, "provenance": {},
                "subset_of": spec.get("subset_of")}
        if path is not None:
            stat = path.stat()
            key = (str(path), stat.st_mtime_ns, stat.st_size)
            if key not in _count_cache:
                df, _ = read_csv_safe(path)
                if len(df) and "origin" in df.columns:
                    origins = df["origin"].fillna("").astype(str).str.strip().replace("", spec["default_origin"])
                elif len(df):
                    origins = pd.Series([spec["default_origin"]] * len(df))
                else:
                    origins = pd.Series([], dtype=str)
                counts = origins.map(origin_class).value_counts()
                _count_cache[key] = (int(len(df)), {str(k): int(v) for k, v in counts.items()})
            info["records"], info["provenance"] = _count_cache[key]
        out.append(info)
    return out


ID_COLUMNS = ("record_id", "browser_id", "session_id")


def load_selection(dataset_ids: list[str], origin_filter: str = "all") -> tuple[pd.DataFrame, dict]:
    """Load one or more datasets into ONE frame, tagging every row with
    ``source_dataset``, ``origin`` and ``origin_class`` (real/synthetic/unverified).

    Raises KeyError for unknown ids and FileNotFoundError when no file exists.
    The core measurement code only ever sees this frame, so adding a CSV (any size,
    any subset of columns) never requires changing it."""
    if origin_filter not in {"all", "real", "synthetic", "unverified"}:
        raise ValueError("origin_filter must be one of: all, real, synthetic, unverified.")
    specs = discover_datasets()
    unknown = [d for d in dataset_ids if d not in specs]
    if unknown:
        raise KeyError(f"Unknown dataset id(s): {unknown}. Available: {sorted(specs)}")
    # de-duplicate while preserving order
    ids = list(dict.fromkeys(dataset_ids))
    info: dict = {"datasets": [], "skipped": [], "warnings": [], "origin_filter": origin_filter}

    # A dataset declared as a subset of another selected one would be double counted.
    for ds in list(ids):
        parent = specs[ds].get("subset_of")
        if parent and parent in ids:
            ids.remove(ds)
            info["skipped"].append({"id": ds, "reason": f"subset of '{parent}', which is already selected"})
            info["warnings"].append(f"'{ds}' skipped: its records are already part of '{parent}'.")

    frames = []
    for ds in ids:
        spec = specs[ds]
        path = _resolve(spec["file"])
        if path is None:
            info["skipped"].append({"id": ds, "reason": f"file '{spec['file']}' not found"})
            continue
        df, warns = read_csv_safe(path)
        info["warnings"].extend(warns)
        df = df.copy()
        if "origin" in df.columns:
            df["origin"] = df["origin"].fillna("").astype(str).str.strip().replace("", spec["default_origin"])
        else:
            df["origin"] = spec["default_origin"]
        df["source_dataset"] = ds
        if ds == "collected":  # attach identifiers recorded only in the new-collections file
            df = _attach_identifiers(df)
        info["datasets"].append({"id": ds, "file": spec["file"], "records": int(len(df))})
        frames.append(df)

    if not frames:
        raise FileNotFoundError("None of the selected datasets exist yet.")
    non_empty = [f for f in frames if len(f)]
    data = pd.concat(non_empty or frames, ignore_index=True, sort=False)
    data["origin_class"] = data["origin"].map(origin_class) if len(data) else pd.Series(dtype=str)
    if origin_filter != "all":
        data = data[data["origin_class"] == origin_filter].reset_index(drop=True)

    # report columns that are absent from some of the combined datasets
    if len(non_empty) > 1:
        all_cols = set().union(*[set(f.columns) for f in non_empty])
        for f, d in zip(non_empty, [d for d in info["datasets"] if d["records"]]):
            missing = sorted(all_cols - set(f.columns))
            if missing:
                info["warnings"].append(
                    f"Dataset '{d['id']}' has no column(s) {missing}; those cells are treated as missing.")
    return data, info


def _attach_identifiers(main_df: pd.DataFrame) -> pd.DataFrame:
    """Left-join record_id/browser_id/session_id from the new-collections file on the
    server timestamp (written identically to both files). Row order is preserved."""
    wanted = [c for c in ID_COLUMNS if c not in main_df.columns]
    if not wanted or "timestamp" not in main_df.columns or not NEW_CSV.exists():
        return main_df
    new_df, _ = read_csv_safe(NEW_CSV)
    if new_df.empty or "timestamp" not in new_df.columns:
        return main_df
    keep = ["timestamp"] + [c for c in wanted if c in new_df.columns]
    if len(keep) == 1:
        return main_df
    meta = new_df[keep].drop_duplicates(subset="timestamp")
    return main_df.merge(meta, on="timestamp", how="left")


# --------------------------------------------------------------------------- results log
def append_measurement_rows(rows: list[dict]) -> int:
    return append_rows(RESULTS_PATH, RESULT_FIELDS, rows)


def read_measurement_rows() -> pd.DataFrame:
    df, _ = read_csv_safe(RESULTS_PATH)
    return df
