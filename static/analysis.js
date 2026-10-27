"use strict";

const sourceSelect = document.getElementById("source");
const refreshButton = document.getElementById("refresh");
const statusLine = document.getElementById("analysis-status");
const resultsSection = document.getElementById("results");
const sourceNote = document.getElementById("source-note");

function setStatus(message, isError = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle("error", isError);
}

function renderResults(data) {
  document.getElementById("rows").textContent = data.rows.toLocaleString();
  document.getElementById("cols").textContent = data.cols.toLocaleString();
  document.getElementById("uniqueness").textContent =
    `${Number(data.original_uniqueness).toFixed(2)}%`;

  const tbody = document.getElementById("results-body");
  tbody.replaceChildren();
  const bestImpact = Math.max(0, ...data.results.map(item => Number(item["Contribution (percentage points)"]) || 0));

  for (const item of data.results) {
    const row = document.createElement("tr");
    const rank = document.createElement("td");
    rank.textContent = String(item.Rank);
    row.appendChild(rank);

    const category = document.createElement("td");
    category.textContent = String(item["Category Removed"]);
    category.className = "category-cell";
    row.appendChild(category);

    const after = document.createElement("td");
    after.textContent = `${Number(item["Uniqueness After Removal (%)"]).toFixed(2)}%`;
    after.className = "number-cell";
    row.appendChild(after);

    const contributionValue = Number(item["Contribution (percentage points)"]) || 0;
    const contribution = document.createElement("td");
    contribution.textContent = `${contributionValue.toFixed(2)} pp`;
    contribution.className = "number-cell";
    row.appendChild(contribution);

    const alone = document.createElement("td");
    alone.className = "number-cell";
    alone.textContent = `${Number(item["Uniqueness Alone (%)"] ?? 0).toFixed(2)}%`;
    row.appendChild(alone);

    const ratioValue = Number(item["Relative Impact Ratio (%)"]) || 0;
    const ratio = document.createElement("td");
    ratio.className = "ratio-cell";
    const ratioLine = document.createElement("div");
    ratioLine.className = "ratio-line";
    const track = document.createElement("span");
    track.className = "ratio-track";
    const fill = document.createElement("span");
    fill.className = "ratio-fill";
    fill.style.width = `${Math.max(0, Math.min(100, ratioValue))}%`;
    track.appendChild(fill);
    const label = document.createElement("strong");
    label.textContent = `${ratioValue.toFixed(2)}%`;
    ratioLine.append(track, label);
    const note = document.createElement("small");
    note.textContent = bestImpact > 0
      ? `${((contributionValue / bestImpact) * 100).toFixed(2)}% of strongest effect`
      : "No measurable effect";
    ratio.append(ratioLine, note);
    row.appendChild(ratio);

    const distinct = document.createElement("td");
    distinct.className = "number-cell";
    distinct.textContent = String(item["Distinct Category Signatures"] ?? "—");
    row.appendChild(distinct);
    tbody.appendChild(row);
  }

  const diagnostic = document.getElementById("analysis-diagnostic");
  const allZero = data.results.every(item => Number(item["Contribution (percentage points)"]) <= 0);
  diagnostic.classList.toggle("warning", allZero);
  diagnostic.textContent = allZero
    ? `Analysis ran successfully, but this dataset shows no measurable uniqueness change when any category is removed. That commonly happens when repeated scans come from the same browser. The relative ratios are correctly 0.00% because there is no positive contribution to compare. Use the sample dataset to test the UI, or collect varied, consented records from different browsers/devices.`
    : `Analysis completed. Relative impact is scaled to the strongest positive category (100%). The contribution in pp is the actual change in measured uniqueness; ratios are not absolute uniqueness percentages.`;

  document.getElementById("download-results").href =
    `/analysis/export?source=${encodeURIComponent(data.source)}`;
  const oc = data.origin_counts || {};
  const originText = Object.keys(oc).length
    ? ` Row origins: ${Object.entries(oc).map(([k, v]) => `${v} ${k.replace("_", " ")}`).join(", ")}.`
    : " Row origins are not labelled.";
  sourceNote.textContent = `${data.source === "sample"
    ? "Source: supplied sample dataset — mostly synthetic, so it tests the pipeline and is not real-world evidence."
    : "Source: records saved by this running Flask server (real scans). Repeated scans of one browser are expected to show 0% uniqueness; use the collector page to compare against the sample."}${originText} ${data.diagnostic || ""} Distinct rows: ${data.unique_rows}; duplicate rows: ${data.duplicate_rows}. Window-size signals are ignored because they change with the window.`;
  resultsSection.hidden = false;
}

async function loadAnalysis() {
  const source = sourceSelect.value;
  refreshButton.disabled = true;
  resultsSection.hidden = true;
  setStatus("Running pandas analysis…");
  try {
    const response = await fetch(`/analyse?source=${encodeURIComponent(source)}`);
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || `Analysis failed (${response.status}).`);
    }
    renderResults(data);
    const allZero = data.results.every(item => Number(item["Contribution (percentage points)"]) <= 0);
    setStatus(allZero
      ? `Analysis ran · no measurable contribution in ${data.rows} records`
      : `Analysis complete · ${data.source_label}`);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", loadAnalysis);
sourceSelect.addEventListener("change", loadAnalysis);
loadAnalysis();
