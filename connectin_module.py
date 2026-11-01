"""Flask web app for WHOOPS fingerprint collection, analysis and measurement."""
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import uuid

import pandas as pd
from flask import Flask, Response, jsonify, render_template, request, send_file

import storage_module as store
from analysing_module import run_analysis, compare_to_reference, build_combined
from measurement_module import MeasurementConfig, measure_selection

BASE_DIR = store.BASE_DIR
# DATA_DIR comes from the WHOOPS_DATA_DIR environment variable (default: ./data).
# On Render, mount a persistent disk and point WHOOPS_DATA_DIR at its mount path.
DATA_DIR = store.DATA_DIR
SAMPLE_CSV = store.resolve_data_file(store.SAMPLE_NAME) or (DATA_DIR / store.SAMPLE_NAME)
CSV_FILE = store.MAIN_CSV                    # whoops_fingerprints.csv  (all collected records)
NEW_CSV = store.NEW_CSV                      # whoops_new_fingerprints.csv (every new collection)
RESULTS_CSV = DATA_DIR / "feature_ablation_results.csv"

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["WHOOPS_PROTECTION_DATA_DIR"] = DATA_DIR
app.config["WHOOPS_MAIN_CSV"] = CSV_FILE
app.config["WHOOPS_NEW_CSV"] = NEW_CSV
from protection_module import register_protection
register_protection(app)

FIELDS = [
    "timestamp", "userAgent", "language", "platform", "timezone",
    "screenWidth", "screenHeight", "colorDepth", "viewportWidth",
    "viewportHeight", "devicePixelRatio", "hardwareConcurrency",
    "touchPoints", "cookieEnabled", "canvasChecksum", "webglAvailable",
]
# whoops_fingerprints.csv keeps the original FIELDS schema so historical rows stay valid.
# The new-collections file adds a record id, the origin of the record and optional
# identifiers that make stability / distinguishability measurable in the future.
NEW_FIELDS = FIELDS + ["record_id", "request_id", "origin", "browser_id", "session_id"]
store.NEW_FIELDS_DEFAULT = NEW_FIELDS
_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

try:
    store.ensure_storage()
except OSError as exc:  # keep the app importable; /collect reports the problem per request
    print(f"WHOOPS: could not prepare data directory {DATA_DIR}: {exc}", file=sys.stderr)

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/analysis")
def analysis_page():
    return render_template("analysis.html")

@app.post("/collect")
def collect():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Send a JSON object containing the fingerprint fields."}), 400

    # Whitelist the expected fields and use a server-controlled timestamp.
    record = {field: data.get(field) for field in FIELDS}
    record["timestamp"] = datetime.now(timezone.utc).isoformat()

    # Canvas can be unavailable in privacy-hardened browsers, so null is allowed.
    required = [
        "userAgent", "screenWidth", "screenHeight",
        "viewportWidth", "viewportHeight", "webglAvailable",
    ]
    missing = [field for field in required if record[field] is None]
    if missing:
        return jsonify({
            "error": "Required fingerprint fields are missing.",
            "fields": missing,
        }), 400

    # Optional identifiers (never inferred from the fingerprint). Malformed values are
    # rejected rather than stored, because an unreliable id would corrupt stability metrics.
    ids = {}
    for key, header in (("requestId", "X-Request-ID"), ("browserId", None), ("sessionId", None)):
        value = data.get(key) or (request.headers.get(header) if header else None)
        if value is None or value == "":
            ids[key] = None
        elif isinstance(value, str) and _ID_PATTERN.match(value):
            ids[key] = value
        else:
            return jsonify({"error": f"'{key}' must be 8-128 characters of letters, digits, . _ : -"}), 400

    try:
        outcome = save_collection(record, request_id=ids["requestId"],
                                  browser_id=ids["browserId"], session_id=ids["sessionId"])
    except OSError as exc:
        return jsonify({"error": f"Could not save the record: {exc}"}), 500

    if outcome.get("duplicate"):
        return jsonify({
            "message": "This request was already saved; no duplicate record was written.",
            "saved": False, "duplicate": True,
            "timestamp": outcome.get("timestamp"), "record_id": outcome.get("record_id"),
        }), 200

    body = {
        "message": "Fingerprint record saved on this server.",
        "saved": True,
        "timestamp": record["timestamp"],
        "record_id": outcome["record_id"],
        "stored_in": [name for name, ok in (("whoops_fingerprints.csv", outcome["main"]),
                                            ("whoops_new_fingerprints.csv", outcome["new"])) if ok],
    }
    if outcome.get("warning"):
        body["warning"] = outcome["warning"]
    return jsonify(body), 201


def save_collection(record: dict, *, request_id=None, browser_id=None, session_id=None) -> dict:
    """Append ONE collected record to both CSV files, exactly once, under one lock.

    * whoops_fingerprints.csv       - main file, original schema, append-only
    * whoops_new_fingerprints.csv   - same record plus record_id/origin/identifiers
    A request that repeats an earlier `request_id` is ignored (idempotent retry).
    Repeated scans WITHOUT a request id are legitimate separate observations and are kept."""
    with store.store_lock():
        if request_id:
            prior = store.find_recent_row(NEW_CSV, "request_id", request_id)
            if prior:
                return {"duplicate": True, "record_id": prior.get("record_id"),
                        "timestamp": prior.get("timestamp")}
        record_id = uuid.uuid4().hex
        new_row = {**record, "record_id": record_id, "request_id": request_id or "",
                   "origin": "collected", "browser_id": browser_id or "", "session_id": session_id or ""}
        store._append_unlocked(CSV_FILE, FIELDS, [record])
        outcome = {"duplicate": False, "record_id": record_id, "main": True, "new": False}
        try:
            store._append_unlocked(NEW_CSV, NEW_FIELDS, [new_row])
            outcome["new"] = True
        except OSError as exc:
            # The main record is already safely stored; report instead of failing the request,
            # because a client retry would otherwise duplicate it in the main file.
            outcome["warning"] = f"Saved to whoops_fingerprints.csv but not to whoops_new_fingerprints.csv: {exc}"
        return outcome

@app.get("/records")
def records():
    """Return the most recent records saved by this local server."""
    if not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0:
        return jsonify({"count": 0, "records": []})
    try:
        df = pd.read_csv(CSV_FILE).fillna("")
        records_list = df.tail(50).iloc[::-1].to_dict(orient="records")
        return jsonify({"count": int(len(df)), "records": records_list})
    except (pd.errors.ParserError, OSError) as exc:
        return jsonify({"error": f"Could not read saved fingerprints: {exc}"}), 500

@app.get("/export")
def export_csv():
    if not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0:
        return jsonify({"error": "No fingerprints have been collected by this server yet."}), 404
    return send_file(CSV_FILE, as_attachment=True, download_name="whoops_fingerprints.csv")

def selected_dataset(source: str):
    if source == "combined":
        return build_combined(CSV_FILE, SAMPLE_CSV), "Sample + collected records (labelled by origin)"
    if source == "sample":
        return SAMPLE_CSV, "Supplied sample dataset"
    if source == "collected":
        return CSV_FILE, "Fingerprints collected by this running server"
    raise ValueError("Unknown data source. Choose 'sample' or 'collected'.")

@app.get("/analyse")
def analyse():
    source = request.args.get("source", "sample")
    try:
        path, label = selected_dataset(source)
        if isinstance(path, Path) and (not path.exists() or path.stat().st_size == 0):
            return jsonify({
                "error": "No collected dataset exists yet. Collect at least two records first."
            }), 404

        result = run_analysis(path)
        result["source"] = source
        result["source_label"] = label
        return jsonify(result)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except (ValueError, pd.errors.ParserError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400

@app.get("/compare")
def compare():
    """Compare the latest COLLECTED record against the SAMPLE reference population."""
    try:
        return jsonify(compare_to_reference(CSV_FILE, SAMPLE_CSV))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except (ValueError, pd.errors.ParserError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400

@app.get("/analysis/export")
def export_analysis():
    source = request.args.get("source", "sample")
    try:
        path, _label = selected_dataset(source)
        result = run_analysis(path)
        RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
        export_rows = []
        for item in result["results"]:
            export_rows.append({
                "Dataset source": source,
                "Origin counts": str(result.get("origin_counts") or "not labelled"),
                "Records analysed": result["rows"],
                "Original uniqueness (%)": result["original_uniqueness"],
                "Unique rows before removal": result["unique_rows"],
                "Duplicate rows before removal": result["duplicate_rows"],
                **item,
                "Dataset diagnostic": result["diagnostic"],
            })
        pd.DataFrame(export_rows).to_csv(RESULTS_CSV, index=False)
        download_name = f"whoops_feature_ablation_{source}.csv"
        return send_file(RESULTS_CSV, as_attachment=True, download_name=download_name)
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except (ValueError, pd.errors.ParserError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400

@app.get("/export/new")
def export_new_csv():
    """Download whoops_new_fingerprints.csv (every collection since the measurement upgrade)."""
    df, _ = store.read_csv_safe(NEW_CSV)
    if df.empty:
        return jsonify({"error": "No new fingerprints have been collected yet."}), 404
    with store.store_lock():
        payload = NEW_CSV.read_bytes()
    return Response(payload, mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=whoops_new_fingerprints.csv"})


# ------------------------------------------------------------------ measurements
@app.get("/measurement")
def measurement_page():
    return render_template("measurement.html")


def _number(value, name, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{name}' must be a number.")
    if integer and int(value) != value:
        raise ValueError(f"'{name}' must be a whole number.")
    if not low <= value <= high:
        raise ValueError(f"'{name}' must be between {low} and {high}.")
    return int(value) if integer else float(value)


@app.get("/measurements/datasets")
def measurement_datasets():
    """Datasets available for measurement (built-in, imports/, datasets.json) with provenance counts."""
    return jsonify({
        "datasets": store.describe_datasets(),
        "origin_filters": ["all", "real", "synthetic", "unverified"],
        "default_datasets": ["collected"],
    })


@app.post("/measurements/run")
def run_measurement():
    """Run all measurements on the selected dataset(s) and append the results to measurement_results.csv.

    JSON body (all optional): {"datasets": ["collected"], "origin_filter": "all",
    "bootstrap_iterations": 1000, "confidence_level": 0.95, "seed": 12345, "save": true}"""
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({"error": "Send a JSON object (or no body)."}), 400
    datasets = body.get("datasets") or ["collected"]
    if not isinstance(datasets, list) or not all(isinstance(d, str) for d in datasets):
        return jsonify({"error": "'datasets' must be a list of dataset ids."}), 400
    try:
        config = MeasurementConfig(
            bootstrap_iterations=_number(body.get("bootstrap_iterations", 1000), "bootstrap_iterations", 50, 5000, True),
            confidence_level=_number(body.get("confidence_level", 0.95), "confidence_level", 0.5, 0.999),
            seed=_number(body.get("seed", 12345), "seed", 0, 2**32 - 1, True),
        )
        save = body.get("save", True)
        if not isinstance(save, bool):
            raise ValueError("'save' must be true or false.")
        report = measure_selection(datasets, str(body.get("origin_filter", "all")), config, save=save)
    except KeyError as exc:
        return jsonify({"error": exc.args[0],
                        "available": [d["id"] for d in store.describe_datasets()]}), 404
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (OSError, pd.errors.ParserError) as exc:
        return jsonify({"error": f"Measurement failed: {exc}"}), 500
    if report["record_count"] == 0:
        return jsonify({
            "error": "The selected datasets contain no records (after the origin filter), so nothing was measured or saved.",
            "dataset": report["dataset"], "warnings": report["warnings"],
        }), 422
    return jsonify(report)


def _typed_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    for rec in df.to_dict(orient="records"):
        for key in ("value", "ci_low", "ci_high", "ci_level"):
            try:
                rec[key] = float(rec[key]) if rec.get(key) not in (None, "") and not pd.isna(rec[key]) else None
            except (TypeError, ValueError):
                pass
        for key in ("record_count", "n_resamples"):
            try:
                rec[key] = int(float(rec[key])) if rec.get(key) not in (None, "") and not pd.isna(rec[key]) else None
            except (TypeError, ValueError):
                pass
        rows.append({k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in rec.items()})
    return rows


@app.get("/measurements/results")
def measurement_results():
    """Stored measurement results. ?run_id=<id> (default: latest run). Also lists recent runs."""
    df = store.read_measurement_rows()
    if df.empty:
        return jsonify({"error": "No measurement results have been saved yet. POST /measurements/run first."}), 404
    runs = df.drop_duplicates("run_id", keep="first")[
        ["run_id", "measured_at", "dataset", "data_kind", "data_provenance", "record_count"]]
    runs = runs.iloc[::-1]  # the file is append-only, so the last run is the newest
    run_id = request.args.get("run_id", "latest")
    if run_id == "latest":
        run_id = runs.iloc[0]["run_id"]
    chosen = df[df["run_id"] == run_id]
    if chosen.empty:
        return jsonify({"error": f"Unknown run_id '{run_id}'."}), 404
    meta = chosen.iloc[0]
    kind = meta["data_kind"]
    warnings = []
    if kind in ("synthetic", "mixed"):
        warnings.append("This run includes synthetic records. It must not be presented as a real-world browser measurement.")
    if kind == "unverified":
        warnings.append("The origin of these records is unverified.")
    return jsonify({
        "run": {"run_id": run_id, "measured_at": meta["measured_at"], "dataset": meta["dataset"],
                "data_kind": kind, "data_provenance": meta["data_provenance"],
                "record_count": int(float(meta["record_count"])), "warnings": warnings,
                "rows": _typed_rows(chosen)},
        "runs": _typed_rows(runs.head(50)),
    })


@app.get("/measurements/export")
def export_measurements():
    """Download measurement_results.csv (full history), or one run with ?run_id=<id>."""
    df = store.read_measurement_rows()
    if df.empty:
        return jsonify({"error": "No measurement results have been saved yet."}), 404
    run_id = request.args.get("run_id")
    if run_id:
        if run_id == "latest":
            run_id = df.iloc[-1]["run_id"]
        chosen = df[df["run_id"] == run_id]
        if chosen.empty:
            return jsonify({"error": f"Unknown run_id '{run_id}'."}), 404
        payload, name = chosen.to_csv(index=False).encode("utf-8"), f"whoops_measurement_{run_id}.csv"
    else:
        with store.store_lock():
            payload, name = store.RESULTS_PATH.read_bytes(), "measurement_results.csv"
    return Response(payload, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}"})


if __name__ == "__main__":
    # Local development only.
    app.run(host="127.0.0.1", port=5000, debug=False)
