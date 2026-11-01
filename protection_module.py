"""WHOOPS fingerprint minimisation research prototype.

Important: this module transforms stored research records only. It does not change
browser APIs or provide browser-wide protection.
"""
import csv
import json
import math
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request

protection_bp = Blueprint("protection", __name__, url_prefix="/protection")

PROFILES = {
    "basic": {
        "name": "Basic Protection",
        "description": "Compatibility-focused minimisation of screen and language detail.",
        "policies": ["screen", "language"],
    },
    "balanced": {
        "name": "Balanced Protection",
        "description": "Stronger minimisation of screen, language, canvas, WebGL, timezone and hardware details.",
        "policies": ["screen", "language", "canvas", "webgl", "timezone", "hardware"],
    },
    "strong": {
        "name": "Strong Protection",
        "description": "Strictest supported research representation. User-agent and platform detail are also suppressed; compatibility may be affected.",
        "policies": ["screen", "language", "canvas", "webgl", "timezone", "hardware", "platform"],
    },
}

def data_dir():
    path = Path(current_app.config.get("WHOOPS_PROTECTION_DATA_DIR", Path(current_app.root_path) / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path

def _bucket(value, step=400):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    lower = ((number - 1) // step) * step + 1
    upper = lower + step - 1
    return f"{lower}-{upper}"

def protect_signals(signals, profile):
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError("Unknown protection profile.")
    if not isinstance(signals, dict):
        raise ValueError("Fingerprint record must be a JSON object.")
    out = dict(signals)
    policies = PROFILES[profile]["policies"]
    applied = []
    changed = set()

    if "screen" in policies:
        for key in ("screenWidth", "screenHeight", "viewportWidth", "viewportHeight"):
            if key in out and out[key] not in (None, ""):
                new_value = _bucket(out[key])
                if new_value != out[key]:
                    out[key] = new_value
                    changed.add(key)
        if changed:
            applied.append("Screen and viewport dimensions generalised into broad ranges")

    if "language" in policies:
        lang_changed = False
        if isinstance(out.get("language"), str):
            val = out["language"].split("-")[0].lower()
            lang_changed |= val != out["language"]
            out["language"] = val
        if isinstance(out.get("languages"), list):
            val = [str(x).split("-")[0].lower() for x in out["languages"][:1]]
            lang_changed |= val != out["languages"]
            out["languages"] = val
        if lang_changed:
            changed.update(k for k in ("language", "languages") if k in out)
            applied.append("Language region and secondary language details reduced")

    if "canvas" in policies and "canvasChecksum" in out and out["canvasChecksum"] is not None:
        out["canvasChecksum"] = None
        changed.add("canvasChecksum")
        applied.append("Canvas checksum suppressed in the research representation")

    if "webgl" in policies and "webglAvailable" in out and out["webglAvailable"] is not None:
        # WHOOPS currently collects availability, not GPU vendor/renderer strings.
        out["webglAvailable"] = None
        changed.add("webglAvailable")
        applied.append("WebGL availability detail suppressed in the research representation")

    if "timezone" in policies and "timezone" in out and out["timezone"] not in (None, ""):
        out["timezone"] = "timezone-present"
        changed.add("timezone")
        applied.append("Exact timezone identifier replaced with a presence category")

    if "hardware" in policies and "hardwareConcurrency" in out and out["hardwareConcurrency"] not in (None, ""):
        try:
            value = int(float(out["hardwareConcurrency"]))
            new_value = "1-4" if value <= 4 else "5+"
        except (TypeError, ValueError):
            new_value = None
        if new_value != out["hardwareConcurrency"]:
            out["hardwareConcurrency"] = new_value
            changed.add("hardwareConcurrency")
            applied.append("Hardware concurrency reduced to a broad category")

    if "platform" in policies:
        for key in ("platform", "userAgent"):
            if key in out and out[key] is not None:
                out[key] = None
                changed.add(key)
        if changed.intersection({"platform", "userAgent"}):
            applied.append("Platform and user-agent details suppressed")

    return out, applied, changed

def _latest_record():
    """Read the latest actual collected record; prefer the ID-bearing CSV."""
    new_csv = Path(current_app.config["WHOOPS_NEW_CSV"])
    main_csv = Path(current_app.config["WHOOPS_MAIN_CSV"])
    for path in (new_csv, main_csv):
        if not path.exists() or path.stat().st_size == 0:
            continue
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        if rows:
            row = rows[-1]
            for key, value in list(row.items()):
                if value == "":
                    row[key] = None
                elif key in {"screenWidth", "screenHeight", "colorDepth", "viewportWidth",
                             "viewportHeight", "devicePixelRatio", "hardwareConcurrency", "touchPoints"}:
                    try:
                        row[key] = int(float(value)) if float(value).is_integer() else float(value)
                    except (ValueError, TypeError):
                        pass
                elif key in {"webglAvailable", "cookieEnabled"} and value is not None:
                    row[key] = str(value).strip().lower() in {"true", "1", "yes"}
            return row
    return None

def _cohort_metrics(rows, profile=None):
    """Dataset-level exact-row metrics; descriptive only, not a tracking guarantee."""
    if len(rows) < 2:
        return {"records": len(rows), "uniqueness_percent": None, "shannon_entropy_bits": None,
                "normalized_entropy": None, "collision_rate_percent": None}
    signatures = []
    for row in rows:
        values = dict(row)
        values.pop("timestamp", None)
        values.pop("record_id", None)
        values.pop("request_id", None)
        values.pop("browser_id", None)
        values.pop("session_id", None)
        if profile:
            values, _, _ = protect_signals(values, profile)
        signatures.append(json.dumps(values, sort_keys=True, default=str, separators=(",", ":")))
    counts = Counter(signatures)
    n = len(signatures)
    unique = len(counts)
    entropy = -sum((c/n) * math.log2(c/n) for c in counts.values())
    max_entropy = math.log2(n) if n > 1 else 0
    return {
        "records": n,
        "uniqueness_percent": round(unique / n * 100, 2),
        "shannon_entropy_bits": round(max(0.0, entropy), 4),
        "normalized_entropy": round(entropy / max_entropy, 4) if max_entropy else None,
        "collision_rate_percent": round((n - unique) / n * 100, 2),
    }

def _read_cohort():
    path = Path(current_app.config["WHOOPS_MAIN_CSV"])
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))

def _feature_ablation_comparison(rows, profile):
    """Run the existing WHOOPS feature-ablation engine on original and transformed cohorts."""
    if len(rows) < 2:
        return {"available": False, "note": "At least two collected records are required by the existing feature-ablation engine."}
    try:
        import pandas as pd
        from analysing_module import run_analysis
        original_frame = pd.DataFrame(rows)
        protected_rows = [protect_signals(row, profile)[0] for row in rows]
        protected_frame = pd.DataFrame(protected_rows)
        before = run_analysis(original_frame)
        after = run_analysis(protected_frame)
        return {
            "available": True,
            "original_uniqueness": before["original_uniqueness"],
            "protected_uniqueness": after["original_uniqueness"],
            "original_results": before["results"],
            "protected_results": after["results"],
            "note": "Existing WHOOPS feature-ablation engine applied independently to the original cohort and to the same cohort transformed with the selected profile.",
        }
    except Exception as exc:
        return {"available": False, "note": f"Existing feature-ablation comparison could not be completed: {exc}"}

@protection_bp.get("/")
def page():
    return render_template("protection.html", profiles=PROFILES)

@protection_bp.get("/status")
def status():
    return jsonify({"available": True, "profiles": PROFILES,
                    "scope": "research representation only; not browser-wide protection"})

@protection_bp.get("/latest")
def latest():
    try:
        record = _latest_record()
    except (OSError, csv.Error) as exc:
        return jsonify({"error": f"Could not read the WHOOPS collection file: {exc}"}), 500
    if record is None:
        return jsonify({"error": "No fingerprint has been collected yet. Return to WHOOPS and collect a fingerprint first."}), 404
    return jsonify({"record": record})

@protection_bp.post("/apply")
def apply():
    payload = request.get_json(silent=True) or {}
    profile = payload.get("profile", "basic")
    if not isinstance(profile, str) or profile not in PROFILES:
        return jsonify({"error": "Choose Basic, Balanced or Strong protection."}), 400
    try:
        original = _latest_record()
    except (OSError, csv.Error) as exc:
        return jsonify({"error": f"Could not read the latest collected fingerprint: {exc}"}), 500
    if original is None:
        return jsonify({"error": "No fingerprint has been collected yet. Collect a fingerprint before applying protection."}), 404

    try:
        protected, policies, changed = protect_signals(original, profile)
        cohort = _read_cohort()
        before_metrics = _cohort_metrics(cohort)
        after_metrics = _cohort_metrics(cohort, profile)
        feature_ablation = _feature_ablation_comparison(cohort, profile)
        metadata_keys = {"timestamp", "record_id", "request_id", "origin", "browser_id", "session_id"}
        signal_keys = sorted((set(original) | set(protected)) - metadata_keys)
        statuses = {}
        target_fields = {
            "screen": {"screenWidth", "screenHeight", "viewportWidth", "viewportHeight"},
            "language": {"language", "languages"},
            "canvas": {"canvasChecksum"},
            "webgl": {"webglAvailable"},
            "timezone": {"timezone"},
            "hardware": {"hardwareConcurrency"},
            "platform": {"platform", "userAgent"},
        }
        selected_targets = set().union(*(target_fields[name] for name in PROFILES[profile]["policies"]))
        for key in signal_keys:
            if original.get(key) != protected.get(key):
                statuses[key] = "modified" if protected.get(key) is not None else "restricted"
            elif key in selected_targets and original.get(key) in (None, ""):
                statuses[key] = "unsupported"
            else:
                statuses[key] = "unchanged"
        record_id = str(uuid.uuid4())
        protection_record = {
            "protection_record_id": record_id,
            "original_record_id": original.get("record_id"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "profile": profile,
            "original": original,
            "protected_representation": protected,
            "policies_applied": policies,
            "signal_status": statuses,
            "summary": {
                "signals_examined": len(signal_keys),
                "signals_modified": sum(v == "modified" for v in statuses.values()),
                "signals_restricted": sum(v == "restricted" for v in statuses.values()),
                "signals_unchanged": sum(v == "unchanged" for v in statuses.values()),
                "signals_unsupported": sum(v == "unsupported" for v in statuses.values()),
            },
            "cohort_metrics_before": before_metrics,
            "cohort_metrics_after_same_policy": after_metrics,
            "feature_ablation_comparison": feature_ablation,
            "metrics_note": "Dataset-level exact-row metrics describe this local collected cohort only. They do not establish protection against a website or real-world tracker.",
            "browser_level_protection": False,
        }
        path = data_dir() / "whoops_protection_records.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(protection_record, ensure_ascii=False, default=str) + "\n")
    except (OSError, csv.Error, ValueError, TypeError) as exc:
        return jsonify({"error": f"Protection could not be completed or saved: {exc}"}), 500

    return jsonify({
        "record_id": record_id,
        "original_record_id": original.get("record_id"),
        "timestamp": protection_record["timestamp"],
        "profile": profile,
        "original": original,
        "protected": protected,
        "policies_applied": policies,
        "signal_status": statuses,
        "summary": protection_record["summary"],
        "metrics_before": before_metrics,
        "metrics_after": after_metrics,
        "feature_ablation": feature_ablation,
        "metrics_note": protection_record["metrics_note"],
        "notice": "This is a transformed research representation. It does not change browser APIs or protect unrelated websites.",
    })

@protection_bp.get("/report")
def report():
    path = data_dir() / "whoops_protection_records.jsonl"
    rows = []
    if path.exists():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError as exc:
            return jsonify({"error": f"Could not read protection records: {exc}"}), 500
    return jsonify({"count": len(rows), "records": rows[-100:]})

def register_protection(app):
    if "protection" not in app.blueprints:
        app.register_blueprint(protection_bp)
