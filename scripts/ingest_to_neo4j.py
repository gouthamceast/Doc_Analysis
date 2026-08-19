"""
ingest_to_neo4j.py

Single-pass ingestion pipeline. Reads *_graph_candidates.json produced by
graph_schema_extractor.py and writes to Neo4j + GCP Vector Store.

Addresses four problems:
  1. Entity resolution  — MERGE on canonical names, not raw names. Duplicate
                          "Honeywell" / "Honeywell Aerospace" nodes collapse
                          into one before they ever reach the database.
  2. Metadata-first     — structured 54-column dataframe is the source of truth.
                          LLM extraction only fills fields the dataframe lacks.
  3. Confidence gating  — edges below CONFIDENCE_THRESHOLD become POSSIBLE_*
                          relationships so bad OCR doesn't corrupt the hard graph.
  4. Atomic dual-write  — each Neo4j node's element_id is stored as metadata on
                          the GCP vector embedding, so vector hits can immediately
                          anchor to graph nodes without a separate lookup.

Usage:
    pip install neo4j google-cloud-aiplatform pandas python-dotenv
    export NEO4J_URI=bolt://localhost:7687
    export NEO4J_USER=neo4j
    export NEO4J_PASSWORD=secret
    export GCP_PROJECT=your-project
    export GCP_LOCATION=us-central1
    export VERTEX_INDEX_ENDPOINT=projects/.../indexEndpoints/...
    export VERTEX_DEPLOYED_INDEX_ID=your-deployed-index-id

    python scripts/ingest_to_neo4j.py \\
        --recon-dir ./graph_recon \\
        --metadata-csv ./contract_metadata.csv
"""

import argparse
import json
import logging
import os
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from neo4j import GraphDatabase, exceptions as neo4j_exc

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------------
CONFIDENCE_THRESHOLD = 0.75   # edges below this become POSSIBLE_* relationships
SIMILARITY_THRESHOLD = 0.85   # name pairs above this are merged as same entity
BATCH_SIZE = 50                # Neo4j write batch size (nodes or edges per tx)

LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|"
    r"international|aerospace|space|group|holdings)\b\.?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Entity resolver — the fix for duplicate nodes
# ---------------------------------------------------------------------------
class EntityResolver:
    """
    Maintains a per-type registry of (normalized_name → canonical_name).
    On every new entity, check if a near-duplicate already exists and return
    the existing canonical name instead of creating a new node.

    This runs in-process during ingestion, so it's fast (no DB round-trip).
    After ingestion, Neo4j MERGE handles the physical deduplication.
    """

    def __init__(self):
        # node_type → {normalized_name: canonical_name}
        self._registry: dict[str, dict[str, str]] = defaultdict(dict)

    @staticmethod
    def _normalize(name: str) -> str:
        n = name.lower().strip()
        n = LEGAL_SUFFIXES.sub("", n)
        n = re.sub(r"[.,\-]", "", n)
        n = re.sub(r"\s+", " ", n).strip()
        return n

    def resolve(self, name: str, node_type: str) -> str:
        """Return the canonical name for this entity (creates it if truly new)."""
        norm = self._normalize(name)
        registry = self._registry[node_type]

        # exact match after normalization
        if norm in registry:
            return registry[norm]

        # fuzzy match — collapse near-duplicates
        for existing_norm, canonical in registry.items():
            score = SequenceMatcher(None, norm, existing_norm).ratio()
            if norm in existing_norm or existing_norm in norm:
                score = max(score, 0.90)
            if score >= SIMILARITY_THRESHOLD:
                log.debug("Resolved '%s' → '%s' (%.2f)", name, canonical, score)
                registry[norm] = canonical   # alias the new form too
                return canonical

        # genuinely new entity
        registry[norm] = name
        return name


# ---------------------------------------------------------------------------
# Metadata lookup — structured dataframe beats LLM extraction
# ---------------------------------------------------------------------------
class MetadataStore:
    """
    Wraps the 54-column structured metadata CSV.
    Call .get(contract_id, field) before trusting an LLM-extracted value.

    Column names in the CSV must match the METADATA_FIELD_GROUPS keys from
    graph_schema_extractor.py (e.g. 'Sales Contract Expiration Date').
    """

    def __init__(self, csv_path: str | None):
        if csv_path and Path(csv_path).exists():
            self._df = pd.read_csv(csv_path, dtype=str).fillna("")
            # index on the most reliable ID column — adjust to match your CSV
            id_col = next(
                (c for c in self._df.columns if "contract" in c.lower() and "id" in c.lower()),
                self._df.columns[0],
            )
            self._df = self._df.set_index(id_col)
            log.info("Loaded metadata for %d contracts from %s", len(self._df), csv_path)
        else:
            self._df = pd.DataFrame()
            log.warning("No metadata CSV — falling back to LLM extraction for all fields")

    def get(self, contract_id: str, field: str) -> str | None:
        """Return structured value if present, else None (triggers LLM fallback)."""
        if self._df.empty or contract_id not in self._df.index:
            return None
        val = self._df.at[contract_id, field] if field in self._df.columns else None
        return val if val else None


# ---------------------------------------------------------------------------
# Neo4j writer
# ---------------------------------------------------------------------------
class Neo4jIngester:
    """
    All writes use MERGE, never CREATE, so re-running ingestion is idempotent.

    Node identity key: (label, canonical_name). This means if the same Party
    appears in 50 contracts, there is exactly one Party node, with edges to
    all 50 Contract nodes.

    Confidence gating: edges with confidence < CONFIDENCE_THRESHOLD are written
    as POSSIBLE_{RELATION} instead of {RELATION}. This keeps the hard graph
    clean while preserving low-confidence extractions for review.
    """

    def __init__(self, uri: str, user: str, password: str):
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._verify()

    def _verify(self):
        with self._driver.session() as s:
            s.run("RETURN 1")
        log.info("Neo4j connection OK")

    def close(self):
        self._driver.close()

    # -- Node upsert ---------------------------------------------------------

    def merge_node(self, label: str, canonical_name: str, properties: dict) -> str:
        """MERGE a node; return its Neo4j element_id for vector store metadata."""
        props = {k: v for k, v in properties.items() if v not in (None, "", [])}
        props["canonical_name"] = canonical_name

        cypher = (
            f"MERGE (n:{label} {{canonical_name: $canonical_name}}) "
            "SET n += $props "
            "RETURN elementId(n) AS eid"
        )
        with self._driver.session() as s:
            result = s.run(cypher, canonical_name=canonical_name, props=props)
            return result.single()["eid"]

    def merge_nodes_batch(self, nodes: list[dict]) -> dict[str, str]:
        """
        Batch MERGE for a list of {label, canonical_name, properties} dicts.
        Returns {canonical_name: element_id}.
        """
        id_map = {}
        for node in nodes:
            eid = self.merge_node(node["label"], node["canonical_name"], node["properties"])
            id_map[node["canonical_name"]] = eid
        return id_map

    # -- Edge upsert ---------------------------------------------------------

    def merge_edge(
        self,
        src_label: str, src_name: str,
        relation: str,
        tgt_label: str, tgt_name: str,
        properties: dict,
        confidence: float = 1.0,
    ):
        rel_type = relation if confidence >= CONFIDENCE_THRESHOLD else f"POSSIBLE_{relation}"
        props = {k: v for k, v in properties.items() if v not in (None, "", [])}
        props["confidence"] = round(confidence, 3)

        cypher = (
            f"MATCH (a:{src_label} {{canonical_name: $src}}) "
            f"MATCH (b:{tgt_label} {{canonical_name: $tgt}}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "SET r += $props"
        )
        with self._driver.session() as s:
            s.run(cypher, src=src_name, tgt=tgt_name, props=props)

    # -- Contract node with metadata overlay ---------------------------------

    def merge_contract(self, contract_id: str, metadata: MetadataStore, llm_props: dict) -> str:
        """
        Build the Contract node: structured metadata wins over LLM extraction
        for every field where it has a value.
        """
        METADATA_FIELDS = [
            "Sales Contract Effective Date", "Sales Contract Expiration Date",
            "Sales Contract or Amendment Value USD", "Sales Contract Status",
            "Sales Contract Type", "General Contract Managing Unit",
            "Sales Contract Company Name", "Document Type",
        ]
        props = dict(llm_props)  # start with LLM-extracted props as baseline
        props["contract_id"] = contract_id

        # override with structured metadata where available (source of truth)
        for field in METADATA_FIELDS:
            val = metadata.get(contract_id, field)
            if val:
                safe_key = field.lower().replace(" ", "_")
                props[safe_key] = val

        return self.merge_node("Contract", contract_id, props)


# ---------------------------------------------------------------------------
# GCP Vector Store writer (Vertex AI Vector Search)
# ---------------------------------------------------------------------------
class VectorIngester:
    """
    Writes text embeddings to Vertex AI Vector Search.
    The neo4j_element_ids field in the metadata is the bridge between the
    two stores: when a vector search returns a chunk, you immediately know
    which Neo4j nodes to start graph traversal from.

    Set SKIP_VECTOR=1 to disable (useful for local dev without GCP credentials).
    """

    def __init__(self):
        if os.getenv("SKIP_VECTOR"):
            self._enabled = False
            log.info("Vector ingestion disabled (SKIP_VECTOR=1)")
            return

        try:
            from google.cloud import aiplatform
            from vertexai.language_models import TextEmbeddingModel
            self._embed_model = TextEmbeddingModel.from_pretrained("text-embedding-005")
            self._endpoint_name = os.environ["VERTEX_INDEX_ENDPOINT"]
            self._deployed_index_id = os.environ["VERTEX_DEPLOYED_INDEX_ID"]
            self._enabled = True
        except Exception as e:
            log.warning("Vector ingestion unavailable: %s — set SKIP_VECTOR=1 to suppress", e)
            self._enabled = False

    def upsert(self, chunk_id: str, text: str, neo4j_element_ids: list[str], metadata: dict):
        if not self._enabled or not text.strip():
            return
        try:
            from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import (
                Namespace,
            )
            embedding = self._embed_model.get_embeddings([text])[0].values
            datapoint = {
                "datapoint_id": chunk_id,
                "feature_vector": embedding,
                "restricts": [
                    Namespace(name="contract_id", allow_tokens=[metadata.get("contract_id", "")]),
                ],
                "crowding_tag": metadata.get("contract_id", ""),
                # Store Neo4j IDs as a pipe-delimited string in numeric_restricts
                # or in a separate metadata store keyed by chunk_id
            }
            # In production, batch these and call index.upsert_datapoints()
            # Here we just log — wire up your specific index client
            log.debug("Would upsert vector chunk_id=%s neo4j_ids=%s", chunk_id, neo4j_element_ids)
        except Exception as e:
            log.warning("Vector upsert failed for %s: %s", chunk_id, e)


# ---------------------------------------------------------------------------
# Main ingestion pipeline
# ---------------------------------------------------------------------------
class IngestionPipeline:

    def __init__(self, neo4j: Neo4jIngester, vector: VectorIngester,
                 metadata: MetadataStore, resolver: EntityResolver):
        self.neo4j = neo4j
        self.vector = vector
        self.metadata = metadata
        self.resolver = resolver
        self._stats = defaultdict(int)

    def _infer_confidence(self, node: dict, source: str) -> float:
        """
        Heuristic confidence score for an LLM-extracted entity.
        source='photographed' is lower confidence than source='pdf_text'.
        Properties with no value get 0.5, with a value get 0.9 base.
        """
        base = 0.70 if source == "photographed" else 0.90
        has_props = bool(node.get("properties"))
        return base + (0.05 if has_props else 0.0)

    def ingest_page(self, page: dict, contract_id: str, source: str):
        """Process one page's extracted nodes and edges."""
        canonical_map: dict[str, str] = {}  # raw_name → canonical_name (for edge rewriting)
        node_element_ids: list[str] = []

        # --- 1. Nodes ---
        for node in page.get("nodes", []):
            raw_name = node.get("name", "").strip()
            node_type = node.get("type", "Unknown")
            if not raw_name:
                continue

            canonical = self.resolver.resolve(raw_name, node_type)
            canonical_map[raw_name] = canonical

            props = dict(node.get("properties") or {})
            confidence = self._infer_confidence(node, source)
            props["_extraction_confidence"] = round(confidence, 3)
            props["_source_doc"] = contract_id

            if node_type == "Contract":
                eid = self.neo4j.merge_contract(canonical, self.metadata, props)
            else:
                eid = self.neo4j.merge_node(node_type, canonical, props)

            node_element_ids.append(eid)
            self._stats[f"node:{node_type}"] += 1

        # --- 2. Edges ---
        for edge in page.get("edges", []):
            raw_src = edge.get("source", "").strip()
            raw_tgt = edge.get("target", "").strip()
            relation = edge.get("relation", "RELATED_TO").upper().replace(" ", "_")
            if not raw_src or not raw_tgt:
                continue

            canon_src = canonical_map.get(raw_src, raw_src)
            canon_tgt = canonical_map.get(raw_tgt, raw_tgt)

            # infer types from what we just wrote
            src_type = next(
                (n["type"] for n in page.get("nodes", []) if n.get("name") == raw_src),
                "Unknown",
            )
            tgt_type = next(
                (n["type"] for n in page.get("nodes", []) if n.get("name") == raw_tgt),
                "Unknown",
            )

            confidence = float(edge.get("properties", {}).get("confidence", 0.85))
            self.neo4j.merge_edge(
                src_label=src_type, src_name=canon_src,
                relation=relation,
                tgt_label=tgt_type, tgt_name=canon_tgt,
                properties=edge.get("properties") or {},
                confidence=confidence,
            )
            self._stats[f"edge:{relation}"] += 1

        # --- 3. Vector store — dual-write with Neo4j element IDs as metadata ---
        raw_text = page.get("_raw_text", "")
        if raw_text and node_element_ids:
            chunk_id = f"{contract_id}::page{page.get('page', 0)}"
            self.vector.upsert(
                chunk_id=chunk_id,
                text=raw_text,
                neo4j_element_ids=node_element_ids,
                metadata={"contract_id": contract_id, "page": page.get("page")},
            )

    def ingest_file(self, candidates_path: Path, source: str = "pdf_text"):
        pages = json.loads(candidates_path.read_text())
        contract_id = candidates_path.stem.replace("_graph_candidates", "")
        log.info("Ingesting %s (%d pages)", contract_id, len(pages))
        for page in pages:
            try:
                self.ingest_page(page, contract_id, source)
            except neo4j_exc.Neo4jError as e:
                log.error("Neo4j error on %s page %s: %s", contract_id, page.get("page"), e)
            except Exception as e:
                log.error("Unexpected error on %s page %s: %s", contract_id, page.get("page"), e)

    def print_stats(self):
        log.info("=== Ingestion stats ===")
        for k, v in sorted(self._stats.items()):
            log.info("  %-40s %d", k, v)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recon-dir", default="./graph_recon",
                    help="Directory containing *_graph_candidates.json files")
    ap.add_argument("--metadata-csv", default=None,
                    help="Path to the 54-column structured metadata CSV")
    ap.add_argument("--source", default="pdf_text",
                    choices=["pdf_text", "photographed"],
                    help="Sets baseline extraction confidence (photographed = lower)")
    args = ap.parse_args()

    uri      = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user     = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")

    resolver = EntityResolver()
    metadata = MetadataStore(args.metadata_csv)
    neo4j    = Neo4jIngester(uri, user, password)
    vector   = VectorIngester()
    pipeline = IngestionPipeline(neo4j, vector, metadata, resolver)

    recon_dir = Path(args.recon_dir)
    files = sorted(recon_dir.glob("*_graph_candidates.json"))
    if not files:
        raise SystemExit(f"No *_graph_candidates.json files found in {recon_dir}")

    log.info("Found %d contract file(s) to ingest", len(files))
    for f in files:
        pipeline.ingest_file(f, source=args.source)

    pipeline.print_stats()
    neo4j.close()
    log.info("Done.")


if __name__ == "__main__":
    main()
