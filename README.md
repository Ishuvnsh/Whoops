# WHOOPS — New UI + Flask backend integrated

This package uses the new monochrome cinematic WHOOPS interface as the default page, wired directly to the existing Flask collector and feature-ablation analysis. The old collector page is no longer the home screen.

## Run on Windows
1. Extract the ZIP.
2. Open the `WHOOPS_integrated` folder.
3. Double-click `run_whoops.bat` (Python 3.10+ required), or run `py -m pip install -r requirements.txt` then `py connectin_module.py`.
4. Open http://127.0.0.1:5000.

## How it works
- The spiral landing page starts real browser-signal collection only when clicked.
- The browser sends the record to the local Flask `/collect` endpoint.
- Every successful collection is written to `data/whoops_fingerprints.csv` and appears in the visible “Collected fingerprints” list. The list loads saved records when the page opens and can expand each row to show all stored fields.
- The page requests analysis from `/analyse?source=collected`. Meaningful uniqueness analysis requires at least two records; with only one record, the fingerprint is still shown and the UI explains why analysis is waiting.
- Export controls download the current fingerprint JSON and actual CSV analysis. The existing `/analysis` route remains available for the original detailed dashboard.

## Privacy
This prototype stores collected browser signals on the machine running Flask. Run locally and collect only with appropriate consent. Do not expose it publicly without appropriate consent, access controls, retention limits, and deployment security.


## Analysis bars and percentages
The home-screen bar labels now show a **relative impact ratio (%)**, scaled so the category with the largest measured uniqueness change is 100%. This makes small but real differences visible. Hover a bar to see the actual uniqueness change in percentage points. If all ratios are 0%, removing categories did not change uniqueness in the currently collected dataset; repeated scans from the same browser commonly produce this result and should not be presented as evidence of cross-browser uniqueness.


## Interpreting zero-valued feature-ablation results
A feature-ablation test cannot infer a feature's effect if the selected dataset contains no variation that makes its removal change uniqueness. Repeated scans of the same browser commonly produce identical fingerprints, so zero contributions can be a correct result—not a working-analysis failure. The analysis page now reports unique/duplicate row counts and a diagnostic, and the exported CSV includes baseline uniqueness, row counts, category diversity, relative impact ratio, and an interpretation for each category. Use the supplied sample dataset to verify that the pipeline runs; use varied, consented browser/device records to evaluate real-world differences. Do not fabricate non-zero percentages for identical observations.

## Collected vs sample data (distinguishability)
- `data/sample_fingerprints.csv` now has an `origin` column: `real_browser` (1 row, inferred from its browser-style timestamp) or `synthetic` (100 rows). Sample results test the pipeline only.
- `/analyse?source=collected` only measures your own scans against each other; repeated scans of one browser correctly give 0%.
- `/compare` checks the latest collected record against the sample: how many sample rows match every signal (anonymity set), the closest match, and per-category overlap. It is shown on the home page after collection.
- `viewportWidth`/`viewportHeight` are ignored for uniqueness because resizing the window made one browser look like several.
- The results table has an "Alone" column (uniqueness using only that category) so redundant categories with 0 contribution are not misread as useless.
