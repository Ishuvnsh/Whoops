"""Flask web app for WHOOPS fingerprint collection and analysis."""
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
import csv

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file

from analysing_module import run_analysis, compare_to_reference, build_combined

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SAMPLE_CSV = DATA_DIR / "sample_fingerprints.csv"
CSV_FILE = DATA_DIR / "whoops_fingerprints.csv"
RESULTS_CSV = DATA_DIR / "feature_ablation_results.csv"

app = Flask(__name__, template_folder="templates", static_folder="static")
CSV_LOCK = Lock()

FIELDS = [
    "timestamp", "userAgent", "language", "platform", "timezone",
    "screenWidth", "screenHeight", "colorDepth", "viewportWidth",
    "viewportHeight", "devicePixelRatio", "hardwareConcurrency",
    "touchPoints", "cookieEnabled", "canvasChecksum", "webglAvailable",
]

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

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_LOCK:
        needs_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0
        with CSV_FILE.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(record)

    return jsonify({
        "message": "Fingerprint record saved on this server.",
        "saved": True,
        "timestamp": record["timestamp"],
    }), 201

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

if __name__ == "__main__":
    # Local development only.
    app.run(host="127.0.0.1", port=5000, debug=False)
