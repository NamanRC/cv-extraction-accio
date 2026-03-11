"""
CV Extraction + Hireability Scoring Pipeline
Runs on RapidCanvas: extracts CV data via Gemini, scores for AI startup fit.
"""
from utils.notebookhelpers.helpers import Helpers
import os
import json
import yaml
import re
import pandas as pd

# ── RC Context ──────────────────────────────────────────────────
context = Helpers.getOrCreateContext(contextId='contextId', localVars=locals())

# ── Secrets & Config ────────────────────────────────────────────
GOOGLE_API_KEY = Helpers.get_secret(context, "GOOGLE_API_KEY")
os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY

from google import genai

ARTIFACT_NAME = "cv-pipeline-config"

# Download config (deploy.sh uploads with basename only)
config_path = Helpers.download_artifact_file(context, ARTIFACT_NAME, "config.yaml")
with open(config_path, "r") as f:
    config = yaml.safe_load(f)

SYSTEM_PROMPT = config.get("system_prompt", "")
SCHEMA = config.get("schema", {})

# ── List CV PDFs from artifacts ─────────────────────────────────
cv_files = []
for fname in ["Ashutosh Tripathi.pdf", "Ravi Prakash Tripathi.pdf"]:
    try:
        path = Helpers.download_artifact_file(context, ARTIFACT_NAME, f"CV PDFs/{fname}")
        cv_files.append((fname, path))
        print(f"Downloaded CV: {fname}")
    except Exception as e:
        print(f"Warning: Could not download {fname}: {e}")

print(f"Found {len(cv_files)} CV PDFs to process")

# ── Extraction via Gemini ───────────────────────────────────────
client = genai.Client()

def extract_cv(pdf_path):
    """Extract structured data from a CV PDF using Gemini."""
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=[
            genai.types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            f"""Extract structured data from this CV/resume.

{SYSTEM_PROMPT}

Return ONLY valid JSON matching this schema:
{json.dumps(SCHEMA, indent=2)}

Return the JSON object directly, no markdown fences."""
        ],
    )

    text = response.text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ── Scoring (imported from module) ──────────────────────────────
from pipeline.scorer import score_candidate

# ── Main Pipeline ───────────────────────────────────────────────
results = []
for fname, pdf_path in cv_files:
    print(f"\nProcessing: {fname}")
    try:
        extraction = extract_cv(pdf_path)
        print(f"  Extracted: {extraction.get('candidate_info', {}).get('candidate_name', 'Unknown')}")

        score_result = score_candidate(extraction)
        score_result["source_file"] = fname
        score_result["extraction"] = extraction
        results.append(score_result)

        print(f"  Score: {score_result['total_score']}/100 — {score_result['verdict']}")
    except Exception as e:
        print(f"  ERROR processing {fname}: {e}")
        results.append({"source_file": fname, "error": str(e)})

# ── Save Results ────────────────────────────────────────────────
out_dir = Helpers.getOrCreateArtifactsDir(context, "cv-scoring-results")
output_path = os.path.join(out_dir, "scoring_results.json")
with open(output_path, "w") as f:
    json.dump(results, f, indent=2)
print(f"\nResults saved to artifact: cv-scoring-results/scoring_results.json")

# Also create a summary DataFrame for RC output
rows = []
for r in results:
    if "error" in r:
        rows.append({"candidate": r["source_file"], "error": r["error"]})
    else:
        row = {
            "candidate": r["candidate"],
            "total_score": r["total_score"],
            "verdict": r["verdict"],
            "source_file": r["source_file"],
        }
        for dim, info in r.get("breakdown", {}).items():
            row[f"{dim}_score"] = info["score"]
            row[f"{dim}_max"] = info["max"]
        rows.append(row)

df = pd.DataFrame(rows)
print("\n=== SCORING SUMMARY ===")
print(df.to_string(index=False))

Helpers.save_output_dataset(context=context, output_name='cv_scoring_output', data_frame=df)
print("\nPipeline complete.")
