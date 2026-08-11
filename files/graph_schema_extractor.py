"""
graph_schema_extractor.py

Purpose
-------
Manual-graph-design helper for the contract characterization platform.

Given one or more contract PDFs, this script:

  1. Extracts text page-by-page from each PDF.
  2. Sends each page to GPT-4o-mini with a structured-extraction prompt that
     is aware of BOTH:
       (a) your existing structured metadata schema (the 54-column
           dataframe: Contract Number, SBGs/SBEs/SBUs, dates, status,
           parent/legacy IDs, etc.) -- these become node TYPES/PROPERTIES
           the model should recognize when it sees them in the narrative
           text, and
       (b) a seed ontology for things that only live in free text
           (Requirement, Deliverable, Standard, Initiative, Milestone,
           Section, Goal, Site, Party, Person, Role, PricingTrack,
           LaborCategory).
     NOTE: per current instructions, this version always attempts full
     extraction from text regardless of whether a metadata column already
     has the value. The "check metadata first, only extract if missing"
     optimization is a follow-up -- see TODO below.
  3. Aggregates + dedupes results ACROSS ALL input documents (not just
     per-doc), so you get one combined view of what topics/relationships
     actually show up across your contract population.
  4. Auto-renders two diagrams so you can see the shape of the graph
     before building the real database:
       - schema_diagram.png     : node TYPES + relationship TYPES (meta-model)
       - instance_graph.png     : actual extracted entities/edges, sampled
                                   down if huge, so it's still readable

TODO (next iteration): metadata-presence check. Before sending a page to
the LLM, look up whether the corresponding contract's dataframe row already
has a value for a given field (e.g. `Sales Contract Expiration Date`). If
present, skip re-extracting it via LLM and just materialize the property
directly onto the Contract node. If absent, fall back to LLM extraction
from the narrative text. Hook point: `metadata_lookup()` below is a stub
for this -- currently always returns None (i.e. "always extract").

Usage
-----
    export OPENAI_API_KEY=sk-...
    uv add openai pypdf networkx matplotlib

    uv run graph_schema_extractor.py \
        --pdf "SmallBusinessSubcontractingPlan.pdf" \
        --pdf "EMSDefenseTrack1FE.pdf" \
        --out ./graph_recon
"""

import argparse
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from openai import OpenAI
from pypdf import PdfReader

MODEL = "gpt-4o-mini"

# ---------------------------------------------------------------------------
# Structured metadata fields (from your existing 54-column dataframe).
# Grouped so the model treats them as properties on Contract / ContractRecord
# nodes rather than inventing new node types for them, and so lineage fields
# become explicit relationships instead of flat properties.
# ---------------------------------------------------------------------------
METADATA_FIELD_GROUPS = {
    "identifiers": [
        "ID", "General Contract ID", "Sales Contract ID", "Legacy Contract ID",
        "Legacy Unique Identifier", "Previous Document ID",
        "General Contract Number", "Sales Contract Number",
        "Legacy Contract Number", "Legacy Contract Customer Contract Number",
        "SFDC Opportunity Number",
    ],
    "lineage_relationships": [
        # These should become EDGES, not properties:
        # Related To          -> RELATED_TO
        # Sales Contract Parent Agreement ID / Parent Contract Number -> PARENT_OF (inverse: CHILD_OF)
        # Previous Document ID / Legacy Contract ID -> SUPERSEDES / SAME_AS
        "Related To", "Sales Contract Parent Agreement ID",
        "Sales Contract Parent Contract Number",
    ],
    "classification": [
        "Document Type", "Status", "Sales Contract Status",
        "Sales Contract Type", "Sales Contract Subtype",
        "General Contract Confidential", "Sales Contract is Confidential",
    ],
    "org_hierarchy": [
        "General Contract Managing Unit", "Sales Contract Managing Unit",
        "General Contract SBEs", "General Contract SBGs", "General Contract SBUs",
        "Sales Contract SBEs", "Sales Contract SBUs", "Sales Contract SBGs",
        "Legacy Contract SBGs",
    ],
    "dates": [
        "Date Created", "Date Updated",
        "General Contract Expiration Date",
        "Legacy Contract Effective Date", "Legacy Contract Expiration Date",
        "Sales Contract Effective Date", "Sales Contract Expiration Date",
        "Sales Contract Date Created",
    ],
    "financial": [
        "Sales Contract or Amendment Value", "Sales Contract or Amendment Value USD",
        "Sales Contract Currency Code",
    ],
    "party": [
        "General Contract Party Name", "Sales Contract Company Name",
        "Sales Contract Region", "Sales Contract Business Model",
    ],
    "pipeline_bookkeeping_EXCLUDE": [
        # Not graph-worthy -- ingestion/QA metadata only.
        "Attached File", "Document Source", "File is Already OCRd",
        "ID Text", "Bot Status", "RPA TimeStamp", "LeapFileName",
        "Annotation_Tool_Status",
    ],
}

SEED_NODE_TYPES = [
    "Contract", "ContractRecord", "Party", "Person", "Role", "Section",
    "Requirement", "Standard", "Deliverable", "Site", "BusinessCategory",
    "Initiative", "Milestone", "PricingTrack", "LaborCategory", "Goal", "OrgUnit",
]

SEED_EDGE_TYPES = [
    "HAS_SECTION", "CONTAINS_REQUIREMENT", "REFERENCES_STANDARD",
    "HAS_DELIVERABLE", "DUE_TO", "SIGNED_BY", "HAS_ROLE", "COVERS_SITE",
    "HAS_GOAL", "RUNS_INITIATIVE", "HAS_MILESTONE", "SUPPORTS_CATEGORY",
    "PRICED_UNDER", "USES_LABOR_CATEGORY", "MANAGED_BY",
    "PARTY_TO", "PARENT_OF", "SUPERSEDES", "SAME_AS", "RELATED_TO",
]

SYSTEM_PROMPT = f"""You are helping design a knowledge graph schema for a
government contract intelligence platform. You will be given the text of
ONE page from a contract document.

The platform already has a STRUCTURED metadata table with these fields
(grouped by category): {json.dumps(METADATA_FIELD_GROUPS, indent=2)}

When you see values on this page that correspond to those fields, extract
them as PROPERTIES on a Contract or ContractRecord node (do not invent a
separate node type for e.g. a date or a currency code). Fields under
"lineage_relationships" should instead become EDGES (e.g. PARENT_OF,
SUPERSEDES, SAME_AS, RELATED_TO) between two Contract/ContractRecord nodes.
Ignore fields under "pipeline_bookkeeping_EXCLUDE" entirely.

For everything else on the page (obligations, deliverables, standards,
initiatives, sites, roles, etc.) use these node types where they fit:
{SEED_NODE_TYPES}
And these relationship types where they fit: {SEED_EDGE_TYPES}

Rules:
- Only extract things actually stated on the page. Do not infer facts not present.
- Give each node a short canonical "name" and a "type".
- Give each edge a "source" name, "relation", "target" name.
- Attach useful "properties" when explicit in the text (dollar amounts,
  percentages, dates, CAGE/UEI codes, section numbers).
- If something doesn't fit any listed type, still extract it and list the
  type under "suggested_new_types" with a one-line reason.
- If the page has nothing graph-worthy, return empty lists.

Return STRICT JSON:
{{
  "nodes": [{{"name": "...", "type": "...", "properties": {{}}}}],
  "edges": [{{"source": "...", "relation": "...", "target": "...", "properties": {{}}}}],
  "suggested_new_types": [{{"type": "...", "reason": "..."}}]
}}
"""


def metadata_lookup(contract_id: str, field: str):
    """
    TODO (next iteration): look up `field` for `contract_id` in the
    structured metadata dataframe. Return the value if present, else None.
    Currently a stub that always returns None, i.e. this version always
    falls through to LLM extraction from the page text.
    """
    return None


def extract_pages_text(pdf_path: str) -> list[str]:
    reader = PdfReader(pdf_path)
    return [page.extract_text() or "" for page in reader.pages]


def call_model(client: OpenAI, page_text: str, page_num: int, doc_name: str) -> dict:
    if not page_text.strip():
        return {"nodes": [], "edges": [], "suggested_new_types": []}

    user_prompt = f"Document: {doc_name}\nPage: {page_num}\n\nPAGE TEXT:\n{page_text}"
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            print(f"  [warn] page {page_num} attempt {attempt+1} failed: {e}")
            time.sleep(2 * (attempt + 1))
    return {"nodes": [], "edges": [], "suggested_new_types": []}


def process_pdf(client: OpenAI, pdf_path: str, out_dir: Path) -> list[dict]:
    doc_name = Path(pdf_path).stem
    print(f"\n=== Processing {doc_name} ===")
    pages = extract_pages_text(pdf_path)
    print(f"  {len(pages)} pages extracted")

    per_page_results = []
    for i, page_text in enumerate(pages, start=1):
        print(f"  -> page {i}/{len(pages)}")
        result = call_model(client, page_text, i, doc_name)
        result["page"] = i
        result["doc"] = doc_name
        per_page_results.append(result)

    raw_out = out_dir / f"{doc_name}_graph_candidates.json"
    raw_out.write_text(json.dumps(per_page_results, indent=2))
    print(f"  Wrote raw candidates -> {raw_out}")
    return per_page_results


def summarize(all_results: list[dict]) -> dict:
    """Dedupe + count node/edge types ACROSS ALL documents."""
    node_type_examples = defaultdict(set)
    edge_type_examples = defaultdict(set)
    suggested_types = defaultdict(set)
    node_count = defaultdict(int)
    edge_count = defaultdict(int)

    for page in all_results:
        for n in page.get("nodes", []):
            t = n.get("type", "Unknown")
            node_count[t] += 1
            if len(node_type_examples[t]) < 10:
                node_type_examples[t].add(n.get("name", ""))
        for e in page.get("edges", []):
            r = e.get("relation", "UNKNOWN")
            edge_count[r] += 1
            if len(edge_type_examples[r]) < 10:
                edge_type_examples[r].add(f'{e.get("source","?")} -> {e.get("target","?")}')
        for s in page.get("suggested_new_types", []):
            suggested_types[s.get("type", "?")].add(s.get("reason", ""))

    return {
        "node_type_counts": dict(sorted(node_count.items(), key=lambda x: -x[1])),
        "node_type_examples": {k: sorted(v) for k, v in node_type_examples.items()},
        "edge_type_counts": dict(sorted(edge_count.items(), key=lambda x: -x[1])),
        "edge_type_examples": {k: sorted(v) for k, v in edge_type_examples.items()},
        "suggested_new_types": {k: sorted(v) for k, v in suggested_types.items()},
    }


def render_diagrams(all_results: list[dict], out_dir: Path) -> None:
    """Auto-render schema-level and instance-level graphs from real extractions."""
    import networkx as nx
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    # ---- Schema-level (types only) ----
    schema_g = nx.DiGraph()
    for page in all_results:
        for n in page.get("nodes", []):
            schema_g.add_node(n.get("type", "Unknown"))
        for e in page.get("edges", []):
            # look up types by re-scanning this page's node list
            node_types = {nd.get("name"): nd.get("type") for nd in page.get("nodes", [])}
            s_type = node_types.get(e.get("source"), "Unknown")
            t_type = node_types.get(e.get("target"), "Unknown")
            schema_g.add_edge(s_type, t_type, label=e.get("relation", ""))

    if schema_g.number_of_nodes() > 0:
        pos = nx.spring_layout(schema_g, seed=7, k=1.6)
        plt.figure(figsize=(15, 11))
        nx.draw_networkx_nodes(schema_g, pos, node_size=2400, node_color="#4C78A8",
                                edgecolors="black")
        nx.draw_networkx_labels(schema_g, pos, font_size=9, font_weight="bold")
        nx.draw_networkx_edges(schema_g, pos, arrowstyle="-|>", arrowsize=14,
                                connectionstyle="arc3,rad=0.08", edge_color="#888888")
        edge_labels = {(s, t): d["label"] for s, t, d in schema_g.edges(data=True)}
        nx.draw_networkx_edge_labels(schema_g, pos, edge_labels=edge_labels, font_size=7,
                                      bbox=dict(alpha=0))
        plt.title("Schema (Node & Relationship Types) — derived from actual extractions")
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(out_dir / "schema_diagram.png", dpi=150)
        plt.close()
        print(f"  Wrote {out_dir / 'schema_diagram.png'}")

    # ---- Instance-level (sampled if large) ----
    inst_g = nx.DiGraph()
    node_types = {}
    for page in all_results:
        for n in page.get("nodes", []):
            name = n.get("name")
            if name:
                inst_g.add_node(name)
                node_types[name] = n.get("type", "Unknown")
        for e in page.get("edges", []):
            s, t, r = e.get("source"), e.get("target"), e.get("relation")
            if s and t:
                inst_g.add_edge(s, t, label=r)

    MAX_NODES = 120
    if inst_g.number_of_nodes() > MAX_NODES:
        # keep highest-degree nodes for readability
        top_nodes = sorted(inst_g.degree, key=lambda x: -x[1])[:MAX_NODES]
        inst_g = inst_g.subgraph([n for n, _ in top_nodes]).copy()
        print(f"  [note] instance graph sampled down to top {MAX_NODES} nodes by degree")

    if inst_g.number_of_nodes() > 0:
        palette = ["#4C78A8", "#F58518", "#E45756", "#72B7B2", "#54A24B",
                   "#EECA3B", "#B279A2", "#9D755D", "#BAB0AC"]
        types_present = sorted(set(node_types.get(n, "Unknown") for n in inst_g.nodes))
        color_map = {t: palette[i % len(palette)] for i, t in enumerate(types_present)}
        node_colors = [color_map[node_types.get(n, "Unknown")] for n in inst_g.nodes]

        pos = nx.spring_layout(inst_g, seed=3, k=0.9)
        plt.figure(figsize=(18, 13))
        nx.draw_networkx_nodes(inst_g, pos, node_size=1500, node_color=node_colors,
                                edgecolors="black", linewidths=0.6)
        nx.draw_networkx_labels(inst_g, pos, font_size=6)
        nx.draw_networkx_edges(inst_g, pos, arrowstyle="-|>", arrowsize=8,
                                edge_color="#aaaaaa", width=0.8)
        legend_handles = [mpatches.Patch(color=c, label=t) for t, c in color_map.items()]
        plt.legend(handles=legend_handles, loc="lower left", fontsize=7, ncol=2, title="Node type")
        plt.title("Instance graph — actual entities/relationships extracted from your contracts")
        plt.axis("off")
        plt.tight_layout()
        plt.savefig(out_dir / "instance_graph.png", dpi=150)
        plt.close()
        print(f"  Wrote {out_dir / 'instance_graph.png'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", action="append", required=True, help="Path to a contract PDF (repeatable)")
    parser.add_argument("--out", default="./graph_recon", help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENAI_API_KEY in your environment first.")
    client = OpenAI(api_key=api_key)

    all_results = []
    for pdf_path in args.pdf:
        all_results.extend(process_pdf(client, pdf_path, out_dir))

    summary = summarize(all_results)
    summary_out = out_dir / "combined_graph_summary.json"
    summary_out.write_text(json.dumps(summary, indent=2))
    print(f"\nWrote combined summary -> {summary_out}")

    print("\nRendering diagrams...")
    render_diagrams(all_results, out_dir)

    print("\nDone. Review combined_graph_summary.json + schema_diagram.png + "
          "instance_graph.png before locking in your Neo4j schema.")


if __name__ == "__main__":
    main()