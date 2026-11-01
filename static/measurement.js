"use strict";

const $ = id => document.getElementById(id);
const datasetSelect = $("datasets");
const originSelect = $("origin-filter");
const runButton = $("run");
const statusLine = $("measure-status");
const runPicker = $("run-picker");

function setStatus(message, isError = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle("error", isError);
}

async function getJSON(url, options) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return Number.isInteger(n) ? n.toLocaleString() : n.toFixed(4);
}

function provenanceText(prov) {
  if (!prov) return "";
  if (typeof prov === "string") return prov.replaceAll(";", " · ").replaceAll(":", " ");
  return Object.entries(prov).map(([k, v]) => `${v} ${k}`).join(" · ");
}

function renderWarnings(warnings, kind) {
  const box = $("warnings");
  box.replaceChildren();
  for (const text of warnings || []) {
    const div = document.createElement("div");
    div.className = "analysis-diagnostic warning";
    div.setAttribute("role", "status");
    div.textContent = text;
    box.appendChild(div);
  }
  if (!(warnings || []).length && kind === "real") {
    const div = document.createElement("div");
    div.className = "analysis-diagnostic";
    div.textContent = "All records in this run are real browser data collected by this server.";
    box.appendChild(div);
  }
}

function renderRun(run) {
  const rows = run.rows || [];
  const kind = run.data_kind || "unverified";
  const prov = run.provenance || run.data_provenance;
  renderWarnings(run.warnings, kind);

  const uniq = rows.find(r => r.metric === "uniqueness_pct" && r.status === "computed");
  $("m-records").textContent = Number(run.record_count || 0).toLocaleString();
  $("m-uniqueness").textContent = uniq && uniq.value !== null ? `${Number(uniq.value).toFixed(2)}%` : "n/a";
  $("m-kind").textContent = kind.toUpperCase();
  $("run-title").textContent = `Run ${run.run_id}`;
  $("run-meta").textContent =
    `Dataset: ${run.dataset} · ${provenanceText(prov)} · measured ${run.measured_at ? new Date(run.measured_at).toLocaleString() : "—"}`;
  $("download-run").href = `/measurements/export?run_id=${encodeURIComponent(run.run_id)}`;

  const body = $("measure-body");
  body.replaceChildren();
  let lastMetric = null;
  for (const r of rows) {
    if (r.metric !== lastMetric) {
      const head = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 6;
      cell.className = "category-cell";
      cell.textContent = r.metric;
      head.appendChild(cell);
      body.appendChild(head);
      lastMetric = r.metric;
    }
    const tr = document.createElement("tr");
    const ci = (r.ci_low !== null && r.ci_low !== undefined && r.ci_low !== "")
      ? `${formatNumber(r.ci_low)} – ${formatNumber(r.ci_high)} (${Math.round(Number(r.ci_level) * 100)}%, ${r.n_resamples} resamples)`
      : "—";
    const cells = [
      ["", ""],
      [r.scope, ""],
      [`${formatNumber(r.value)}${r.unit && r.value !== null && r.value !== "" ? " " + r.unit : ""}`, "number-cell"],
      [ci, "number-cell"],
      [String(r.status || "").replaceAll("_", " "), ""],
      [r.note || "", ""],
    ];
    cells.forEach(([text, cls], index) => {
      const td = document.createElement("td");
      td.textContent = text;
      if (cls) td.className = cls;
      if (index === 0) td.className = "";
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
  $("results").hidden = false;
}

function renderRunPicker(runs, selectedId) {
  runPicker.replaceChildren();
  for (const run of runs || []) {
    const option = document.createElement("option");
    option.value = run.run_id;
    option.textContent = `${new Date(run.measured_at).toLocaleString()} · ${run.dataset} · ${run.record_count} records · ${run.data_kind}`;
    option.selected = run.run_id === selectedId;
    runPicker.appendChild(option);
  }
}

async function loadStoredRun(runId = "latest") {
  try {
    const data = await getJSON(`/measurements/results?run_id=${encodeURIComponent(runId)}`);
    renderRun(data.run);
    renderRunPicker(data.runs, data.run.run_id);
    setStatus(`Showing stored run · ${data.run.dataset} · ${data.run.record_count} records`);
  } catch (error) {
    if (error.status === 404) {
      setStatus("No stored measurements yet. Choose datasets and click “Run measurement”.");
    } else {
      setStatus(error.message, true);
    }
  }
}

async function loadDatasets() {
  const data = await getJSON("/measurements/datasets");
  datasetSelect.replaceChildren();
  for (const d of data.datasets) {
    const option = document.createElement("option");
    option.value = d.id;
    option.disabled = !d.available;
    option.selected = (data.default_datasets || []).includes(d.id) && d.available;
    const prov = provenanceText(d.provenance);
    option.textContent = `${d.label} — ${d.records} records${prov ? " (" + prov + ")" : ""}${d.available ? "" : " — file not found"}`;
    datasetSelect.appendChild(option);
  }
}

async function runMeasurement() {
  const chosen = Array.from(datasetSelect.selectedOptions).map(o => o.value);
  if (!chosen.length) {
    setStatus("Select at least one dataset.", true);
    return;
  }
  runButton.disabled = true;
  setStatus("Running measurements… large datasets can take a little while.");
  try {
    const report = await getJSON("/measurements/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ datasets: chosen, origin_filter: originSelect.value }),
    });
    renderRun(report);
    setStatus(`Measurement complete · ${report.dataset} · ${report.record_count} records · saved to measurement_results.csv`);
    const stored = await getJSON("/measurements/results?run_id=latest").catch(() => null);
    if (stored) renderRunPicker(stored.runs, report.run_id);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    runButton.disabled = false;
  }
}

runButton.addEventListener("click", runMeasurement);
runPicker.addEventListener("change", () => loadStoredRun(runPicker.value));
loadDatasets().then(() => loadStoredRun("latest")).catch(error => setStatus(error.message, true));
