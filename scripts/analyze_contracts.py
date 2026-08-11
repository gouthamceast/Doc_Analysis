#!/usr/bin/env python3
"""
analyze_contracts.py

Analyzes photographed contract pages (HEIC images in ./contracts) using GPT-4o mini
vision, to extract structured insights useful for designing the Contract Intelligence
Platform's vectorization pipeline and Neo4j knowledge graph schema.

USAGE
    export OPENAI_API_KEY=sk-...
    python3 scripts/analyze_contracts.py --limit 3        # test on a few images first
    python3 scripts/analyze_contracts.py                  # full run over contracts/

REQUIREMENTS
    pip3 install openai
    macOS only for HEIC conversion — uses the built-in `sips` command, no extra
    image libraries needed. (If running elsewhere, convert contracts/*.HEIC to
    .jpg yourself and point --input at that folder instead.)

OUTPUT (written under ./contract_insights/)
    converted_jpeg/            — JPEG copies used for the API calls
    per_image/*.json           — one structured extraction per page
    aggregated_insights.json   — all per-page extractions combined
    proposed_graph_schema.md   — GPT-4o mini's schema proposal, grounded in the
                                  actual entities/relationships it found across
                                  the real contract set (not a generic template)
"""

import argparse
import base64
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    sys.exit("Missing dependency. Run: pip3 install openai")

MODEL = "gpt-4o-mini"
MAX_WORKERS = 4
MAX_RETRIES = 3
MAX_IMAGE_DIM = 1600  # longest side, px — keeps vision API cost/latency down

# ---------------------------------------------------------------------------
# Per-page extraction schema. Mirrors the node/relationship categories from
# the project's Neo4j schema design (Contract, ContractVersion, Party,
# Milestone, PaymentTerm, Document/REFERENCES) so the output maps directly
# onto graph nodes rather than needing a translation step later.
# ---------------------------------------------------------------------------
PER_IMAGE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "contract_page_insights",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "document_type_guess": {
                    "type": "string",
                    "enum": [
                        "sales_agreement", "statement_of_work", "government_solicitation_form",
                        "wage_determination_table", "subcontracting_plan", "milestone_schedule",
                        "payment_or_pricing_table", "termination_liability_schedule",
                        "signature_page", "terms_and_conditions", "applicable_documents_table",
                        "wbs_or_org_diagram", "other",
                    ],
                },
                "section_heading": {"type": "string"},
                "parties": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                        },
                        "required": ["name", "role"],
                        "additionalProperties": False,
                    },
                },
                "dates_or_milestones": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                        },
                        "required": ["label", "value"],
                        "additionalProperties": False,
                    },
                },
                "monetary_values": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "amount": {"type": "string"},
                        },
                        "required": ["label", "amount"],
                        "additionalProperties": False,
                    },
                },
                "cross_document_references": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "reference_text": {"type": "string"},
                            "target_document_number": {"type": "string"},
                        },
                        "required": ["reference_text", "target_document_number"],
                        "additionalProperties": False,
                    },
                },
                "tables_detected": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "columns": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["title", "columns"],
                        "additionalProperties": False,
                    },
                },
                "confidentiality_marking": {"type": "string"},
                "has_signature": {"type": "boolean"},
                "notes": {"type": "string"},
            },
            "required": [
                "document_type_guess", "section_heading", "parties", "dates_or_milestones",
                "monetary_values", "cross_document_references", "tables_detected",
                "confidentiality_marking", "has_signature", "notes",
            ],
            "additionalProperties": False,
        },
    },
}

EXTRACTION_PROMPT = """You are analyzing one page photographed from a Honeywell Aerospace \
contract document, as part of designing a contract-intelligence system (vector search + \
knowledge graph).

Look at the image and extract structured information per the schema. Guidelines:
- document_type_guess: your best guess at what kind of contract page this is.
- parties: any named companies/entities and their apparent role (buyer, seller, contractor, etc).
- dates_or_milestones: any dates, milestone names (e.g. PDR, CDR, ATP), or schedule offsets.
- monetary_values: any dollar amounts or percentages tied to payment, each with a short label.
- cross_document_references: any reference to another document (e.g. "Exhibit A", "AD1", a \
document number) — capture the reference text and the target document number/id if visible.
- tables_detected: any table on the page — a short title/description and its column headers.
- confidentiality_marking: any confidentiality/proprietary legend text, else empty string.
- has_signature: true if a signature block or signature is visible on this page.
- notes: anything else structurally notable (e.g. "this is a diagram", "scanned form with \
numbered fields", "mostly boilerplate legal text", "image is blurry/hard to read").

If a field doesn't apply, return an empty array or empty string — do not omit fields. If the \
photo is unreadable, say so plainly in notes rather than guessing at content."""


def convert_heic_to_jpeg(heic_path: Path, out_dir: Path) -> Path:
    """Convert HEIC to JPEG using macOS's built-in `sips`, resized to control API cost."""
    out_path = out_dir / (heic_path.stem + ".jpg")
    if out_path.exists():
        return out_path
    subprocess.run(
        ["sips", "-s", "format", "jpeg", "-Z", str(MAX_IMAGE_DIM), str(heic_path),
         "--out", str(out_path)],
        check=True, capture_output=True,
    )
    return out_path


def encode_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def analyze_image(client: OpenAI, image_path: Path) -> dict:
    b64 = encode_image(image_path)
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": EXTRACTION_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]},
                ],
                response_format=PER_IMAGE_SCHEMA,
                max_tokens=1200,
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:  # noqa: BLE001 — surfaced in output for visibility
            last_err = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    return {"error": last_err}


def synthesize_graph_schema(client: OpenAI, all_insights: list) -> str:
    """Second-pass call: given all per-page insights, propose a Neo4j graph schema
    grounded in what was actually found, not a generic template."""
    summary_input = json.dumps(all_insights, indent=2)[:60000]  # keep prompt bounded
    prompt = f"""Below is structured data extracted from {len(all_insights)} pages of real \
Honeywell Aerospace contract documents (JSON array, one object per page).

Based on the actual entity types, relationships, and document patterns you see in this data \
— not a generic template — propose a Neo4j knowledge graph schema:
1. List each recommended node label with its key properties.
2. List each recommended relationship type, with its start/end node labels and its meaning.
3. Flag any patterns in this data that don't fit a typical contract schema, and call out \
anything surprising or specific to this contract set.
4. Note which fields look unreliable or low-confidence given this is a photographed (not \
digitally-native) document set, and would need re-validation against clean source PDFs.

Data:
{summary_input}
"""
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    return resp.choices[0].message.content


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="contracts", help="Folder of HEIC contract page photos")
    ap.add_argument("--output", default="contract_insights", help="Output folder")
    ap.add_argument("--limit", type=int, default=None,
                     help="Only process the first N images (use this to test before a full run)")
    args = ap.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit("Set OPENAI_API_KEY in your environment before running this script.")
    client = OpenAI(api_key=api_key)

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    jpeg_dir = output_dir / "converted_jpeg"
    per_image_dir = output_dir / "per_image"
    jpeg_dir.mkdir(parents=True, exist_ok=True)
    per_image_dir.mkdir(parents=True, exist_ok=True)

    heic_files = sorted(set(input_dir.glob("*.HEIC")) | set(input_dir.glob("*.heic")))
    if args.limit:
        heic_files = heic_files[: args.limit]
    if not heic_files:
        sys.exit(f"No .HEIC files found in {input_dir}/")
    print(f"Found {len(heic_files)} image(s) to process.")

    # Step 1 — convert HEIC -> JPEG (sequential; sips is fast and this avoids
    # spawning many subprocesses at once)
    jpeg_paths = []
    for f in heic_files:
        try:
            jpeg_paths.append(convert_heic_to_jpeg(f, jpeg_dir))
        except subprocess.CalledProcessError as e:
            print(f"  ! Failed to convert {f.name}: {e}")

    # Step 2 — analyze each image via GPT-4o mini vision (bounded parallelism)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(analyze_image, client, p): p for p in jpeg_paths}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            p = futures[future]
            data = future.result()
            data["_source_file"] = p.name
            results[p.name] = data
            (per_image_dir / f"{p.stem}.json").write_text(json.dumps(data, indent=2))
            label = data.get("document_type_guess", f"ERROR: {data.get('error')}")
            print(f"  [{i}/{len(jpeg_paths)}] {p.name} -> {label}")

    all_insights = [results[k] for k in sorted(results)]
    (output_dir / "aggregated_insights.json").write_text(json.dumps(all_insights, indent=2))

    # Step 3 — synthesize a graph schema proposal from the aggregate, grounded
    # in what was actually found across this contract set
    print("Synthesizing graph schema recommendation from aggregate insights...")
    schema_proposal = synthesize_graph_schema(client, all_insights)
    (output_dir / "proposed_graph_schema.md").write_text(schema_proposal)

    print(f"\nDone.\n  Per-page results : {per_image_dir}/\n"
          f"  Aggregated       : {output_dir}/aggregated_insights.json\n"
          f"  Graph schema     : {output_dir}/proposed_graph_schema.md")


if __name__ == "__main__":
    main()
