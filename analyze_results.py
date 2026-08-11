"""
analyze_combined_graph.py

Purpose
-------
Deeper analysis of the extracted contract knowledge graph, run AFTER
graph_schema_extractor.py has produced a ./graph_recon/ folder containing
one <doc>_graph_candidates.json per PDF (raw per-page node/edge instances)
and combined_graph_summary.json (type-level counts/examples).

combined_graph_summary.json is a type-level rollup only -- it can't tell you
which specific nodes are hubs, which contracts are disconnected from the
rest of the graph, or which Party nodes are actually the same real-world
entity under different names. This script reconstructs the full instance
graph from the *_graph_candidates.json files and answers those questions.

What it computes
-----------------
1. Graph reconstruction: merges every page's nodes/edges across every
   document into one networkx.MultiDiGraph (multi-edge, since e.g. two
   different Standards can both be REFERENCES_STANDARD from the same
   Contract).
2. Degree / centrality: which nodes are the biggest hubs (top-N by
   in+out degree) -- these are usually your most important entities
   (major Parties, frequently-cited Standards) and are worth reviewing
   first when validating the graph.
3. Connected components: how many disconnected clusters exist. In a
   contract graph you'd generally expect ~1 cluster per contract family
   that shares no parties/standards with others; an unexpectedly large
   number of singleton/tiny components can indicate extraction gaps
   (e.g. a page where the model didn't link back to the parent Contract).
4. Isolated nodes: nodes with zero edges -- usually signals a missed
   relationship on that page, worth spot-checking.
5. Likely-duplicate detection: within each node type, flags name pairs
   that are probably the same real-world entity under different surface
   forms (e.g. "Honeywell" / "Honeywell Aerospace" / "Honeywell
   International Inc."). Uses stdlib difflib (no extra dependency) plus
   a normalization pass (case-fold, strip legal suffixes like Inc./LLC/
   Corporation, collapse whitespace) before fuzzy-matching what's left.
   This directly targets the entity-resolution risk flagged in the team
   report.
6. Per-type edge sanity check: recomputes edge_type_counts from the raw
   files and diffs them against combined_graph_summary.json, as a
   consistency check that the summary wasn't stale or partial.

Outputs (written to --out, default ./graph_recon/analysis/)
-------------------------------------------------------------
  hub_nodes.csv            top-N nodes by degree, with type and degree
  connected_components.json  component count + size distribution
  isolated_nodes.json       nodes with zero edges
  duplicate_candidates.csv  likely-duplicate name pairs per type, with a
                             similarity score, for manual review/merge
  edge_count_diff.json      raw-recount vs. summary-file edge counts

Usage
-----
    uv add networkx
    uv run analyze_combined_graph.py --recon-dir ./graph_recon --out ./graph_recon/analysis

    # tune duplicate-detection sensitivity (0-1, higher = stricter match required)
    uv run analyze_combined_graph.py --recon-dir ./graph_recon --dup-threshold 0.82
"""

import argparse
import csv
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

import networkx as nx

LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|corp|corporation|co|company|ltd|limited|"
    r"international|aerospace|space|group|holdings)\b\.?",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    """Strip legal-entity suffixes/punctuation so 'Honeywell International Inc.'
    and 'Honeywell' have a fighting chance of matching."""
    n = name.lower().strip()
    n = LEGAL_SUFFIXES.sub("", n)
    n = re.sub(r"[.,]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def load_instance_graph(recon_dir: Path) -> tuple[nx.MultiDiGraph, dict]:
    """Load every *_graph_candidates.json in recon_dir and merge into one graph."""
    G = nx.MultiDiGraph()
    node_types = {}
    raw_edge_counts = defaultdict(int)

    candidate_files = sorted(recon_dir.glob("*_graph_candidates.json"))
    if not candidate_files:
        raise SystemExit(
            f"No *_graph_candidates.json files found in {recon_dir}. "
            "Run graph_schema_extractor.py first -- this script analyzes its output."
        )

    for f in candidate_files:
        pages = json.loads(f.read_text())
        for page in pages:
            for n in page.get("nodes", []):
                name = n.get("name")
                if not name:
                    continue
                G.add_node(name)
                node_types[name] = n.get("type", "Unknown")
            for e in page.get("edges", []):
                s, t, r = e.get("source"), e.get("target"), e.get("relation", "UNKNOWN")
                if s and t:
                    G.add_edge(s, t, relation=r)
                    raw_edge_counts[r] += 1

    nx.set_node_attributes(G, node_types, "type")
    print(f"Loaded {len(candidate_files)} document(s), "
          f"{G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    return G, dict(raw_edge_counts)


def hub_nodes(G: nx.MultiDiGraph, top_n: int) -> list[dict]:
    degrees = sorted(G.degree, key=lambda x: -x[1])[:top_n]
    return [
        {"name": name, "type": G.nodes[name].get("type", "Unknown"), "degree": deg}
        for name, deg in degrees
    ]


def component_analysis(G: nx.MultiDiGraph) -> dict:
    undirected = G.to_undirected()
    components = list(nx.connected_components(undirected))
    sizes = sorted((len(c) for c in components), reverse=True)
    return {
        "total_components": len(components),
        "largest_component_size": sizes[0] if sizes else 0,
        "size_distribution": sizes,
        "num_singleton_components": sum(1 for s in sizes if s == 1),
    }


def isolated_nodes(G: nx.MultiDiGraph) -> list[dict]:
    return [
        {"name": n, "type": G.nodes[n].get("type", "Unknown")}
        for n in G.nodes if G.degree(n) == 0
    ]


def find_duplicate_candidates(G: nx.MultiDiGraph, threshold: float) -> list[dict]:
    """Within each node type, flag name pairs likely to be the same entity."""
    by_type = defaultdict(list)
    for n, data in G.nodes(data=True):
        by_type[data.get("type", "Unknown")].append(n)

    candidates = []
    for node_type, names in by_type.items():
        if len(names) < 2:
            continue
        normalized = {n: normalize_name(n) for n in names}
        for a, b in combinations(names, 2):
            na, nb = normalized[a], normalized[b]
            if not na or not nb:
                continue
            # quick exact-after-normalization match
            if na == nb:
                score = 1.0
            else:
                score = SequenceMatcher(None, na, nb).ratio()
                # also check substring containment (e.g. "honeywell" in "honeywell aerospace")
                if na in nb or nb in na:
                    score = max(score, 0.9)
            if score >= threshold:
                candidates.append({
                    "type": node_type, "name_a": a, "name_b": b,
                    "similarity": round(score, 3),
                    "degree_a": G.degree(a), "degree_b": G.degree(b),
                })
    candidates.sort(key=lambda x: -x["similarity"])
    return candidates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recon-dir", default="./graph_recon",
                         help="Directory containing *_graph_candidates.json and combined_graph_summary.json")
    parser.add_argument("--out", default=None, help="Output dir (default: <recon-dir>/analysis)")
    parser.add_argument("--top-n-hubs", type=int, default=30)
    parser.add_argument("--dup-threshold", type=float, default=0.78,
                         help="Similarity threshold (0-1) for flagging duplicate node names")
    args = parser.parse_args()

    recon_dir = Path(args.recon_dir)
    out_dir = Path(args.out) if args.out else recon_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    G, raw_edge_counts = load_instance_graph(recon_dir)

    # ---- 1. Hub nodes ----
    hubs = hub_nodes(G, args.top_n_hubs)
    with open(out_dir / "hub_nodes.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "type", "degree"])
        writer.writeheader()
        writer.writerows(hubs)
    print(f"Wrote {out_dir / 'hub_nodes.csv'} ({len(hubs)} rows)")

    # ---- 2. Connected components ----
    comp_stats = component_analysis(G)
    (out_dir / "connected_components.json").write_text(json.dumps(comp_stats, indent=2))
    print(f"Wrote {out_dir / 'connected_components.json'} "
          f"({comp_stats['total_components']} components, "
          f"{comp_stats['num_singleton_components']} singletons)")

    # ---- 3. Isolated nodes ----
    isolated = isolated_nodes(G)
    (out_dir / "isolated_nodes.json").write_text(json.dumps(isolated, indent=2))
    print(f"Wrote {out_dir / 'isolated_nodes.json'} ({len(isolated)} isolated nodes)")

    # ---- 4. Duplicate candidates ----
    dupes = find_duplicate_candidates(G, args.dup_threshold)
    with open(out_dir / "duplicate_candidates.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["type", "name_a", "name_b", "similarity", "degree_a", "degree_b"])
        writer.writeheader()
        writer.writerows(dupes)
    print(f"Wrote {out_dir / 'duplicate_candidates.csv'} ({len(dupes)} candidate pairs "
          f"at threshold {args.dup_threshold})")

    # ---- 5. Edge count sanity check vs. combined_graph_summary.json ----
    summary_path = recon_dir / "combined_graph_summary.json"
    diff = {}
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        summary_edges = summary.get("edge_type_counts", {})
        all_relations = set(raw_edge_counts) | set(summary_edges)
        for rel in sorted(all_relations):
            raw_c = raw_edge_counts.get(rel, 0)
            sum_c = summary_edges.get(rel, 0)
            if raw_c != sum_c:
                diff[rel] = {"raw_recount": raw_c, "summary_file": sum_c}
        (out_dir / "edge_count_diff.json").write_text(json.dumps(diff, indent=2))
        if diff:
            print(f"  [warn] {len(diff)} relation type(s) differ between raw files and "
                  f"combined_graph_summary.json -- see edge_count_diff.json")
        else:
            print("  Edge counts match combined_graph_summary.json exactly.")
    else:
        print(f"  [note] {summary_path} not found -- skipped sanity check")

    # ---- Console summary ----
    print("\n=== Summary ===")
    print(f"Nodes: {G.number_of_nodes()} | Edges: {G.number_of_edges()}")
    print(f"Top 5 hubs: {[(h['name'], h['degree']) for h in hubs[:5]]}")
    print(f"Components: {comp_stats['total_components']} "
          f"(largest = {comp_stats['largest_component_size']} nodes)")
    print(f"Isolated nodes: {len(isolated)}")
    print(f"Likely duplicate pairs (>= {args.dup_threshold} similarity): {len(dupes)}")
    if dupes[:5]:
        print("  Top candidates:")
        for d in dupes[:5]:
            print(f"    [{d['type']}] \"{d['name_a']}\" <-> \"{d['name_b']}\" "
                  f"(sim={d['similarity']})")


if __name__ == "__main__":
    main()