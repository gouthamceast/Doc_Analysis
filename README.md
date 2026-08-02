# Honeywell Aerospace — Contract Intelligence Platform
## Working Reference & Build Notes

**Purpose of this document:** This captures everything worked out so far on the Contract Vectorization Architecture project — the reasoning behind each design decision, the terminology needed to follow the domain, and the open questions still needing answers from other teams. It's written to be readable by a person new to contract analysis, and also structured enough to hand to an AI coding assistant (e.g. Claude Code) as project context/instructions before implementation work begins.

**How to use this with Claude Code:** Paste or reference this file at the start of a build session. Treat Sections 1–9 as design decisions already made. Treat Section 10 ("Open Questions") as unresolved — if an implementation detail depends on one of those answers, ask rather than assume, or implement behind a config flag that can be adjusted once the real answer comes back from the relevant team.

---

## 1. Project Overview

The goal is to turn Honeywell Aerospace's unstructured contract documents (PDFs, Word docs, Excel sheets, slides, sitting in repositories and file shares) into a governed, AI-queryable knowledge base — rather than static files only humans can search manually.

The source of truth for finalized contracts is **LEAP**, Honeywell's internal tool that stores contracts once they're "frozen" (finalized/executed). The target platform is built on Google Cloud:

- **Google Cloud Storage (GCS)** — landing zone for raw contract uploads
- **Vertex AI Data Store** — parsing, chunking, embedding, vector search
- **Neo4j** — knowledge graph of entities and relationships extracted from contracts
- **Workato** — integration/automation layer moving data from LEAP into GCS

This document assumes a **full production rollout** is the target (not just a proof-of-concept), being built and led in-house.

---

## 2. Architecture at a Glance

Three main layers, plus cross-cutting concerns that must be built alongside them, not bolted on afterward.

**Layer 1 — Ingestion & Vectorization.** Contracts land in the GCS landing zone, then Vertex AI Data Store handles document ingestion: parsing/OCR, chunking, and embedding into a managed vector index.

**Layer 2 — Knowledge Graph.** An LLM reads parsed contracts and extracts entities and relationships (parties, clauses, obligations, payment terms, dates) into a Neo4j graph — a structured, queryable representation of contract facts, not just similar-sounding text.

**Layer 3 — Access & Governance.** Vertex AI Data Store exposes a unified API with access control, hybrid search, and metadata/lineage tracking, so internal and external agents (copilots, chat experiences, dashboards, custom agents) can query contract intelligence securely.

**Cross-cutting (build in parallel, not after):**
- *Governance & Security* — policy enforcement, data classification, audit logging, access review. This needs to exist **before** Layer 1 goes live given the confidentiality markings already present on Honeywell contracts (e.g. "Honeywell Aerospace Confidential," FAR-restricted government content).
- *Platform Services* — identity & access management, storage, model registry/governance.
- *Observability & Continuous Improvement* — data quality monitoring, drift detection, usage analytics, feedback loops.

---

## 3. Data Flow: LEAP → Workato → GCS → Pipeline

**Workato** is an integration/automation platform (an "iPaaS," similar in category to Zapier or MuleSoft, but built for enterprise systems). Instead of custom code connecting two systems, you build "recipes": a trigger plus a sequence of actions. Here, the trigger is "LEAP freezes a contract" and the actions are "attach metadata, push the file to GCS, notify downstream."

Two separate recipes are needed:

1. **Event-driven recipe (ongoing).** Fires on each new freeze event in LEAP (ideally via webhook — confirm this is supported; polling on a schedule is the fallback but introduces lag). Pushes the newly frozen document to GCS along with metadata.
2. **Batch backfill recipe (one-time).** Walks LEAP's existing contract archive and bulk-exports everything already frozen, tagging it the same way. Run this first to establish the baseline corpus, then switch on the event-driven recipe from that cutover point forward so nothing is processed twice or missed in the handoff. Needs pagination/rate-limiting and resumability given likely archive size.

**Metadata that must travel with every file** (this is the backbone the rest of the design depends on):
- Stable **contract ID** (persists across all revisions of the same agreement)
- **Revision number**
- **Freeze timestamp**
- **Document type** (main agreement, SOW, exhibit, external ICD, etc.)
- **Confidentiality/distribution marking** (LEAP likely already classifies this — carry it through rather than re-deriving it downstream)
- Ideally, any **applicable-document links** LEAP already tracks (see Section 6 — this would be a major shortcut for reference resolution)

**Reliability requirements for both recipes:**
- **Idempotency** — tag each push with a key (contract ID + revision + freeze timestamp) so a retried webhook or a re-run backfill doesn't create duplicate ingestion events.
- **Dead-letter handling** — if a document arrives with no usable contract ID or an unreadable file, route it to a review/alert queue rather than dropping it silently.

---

## 4. Handling Contract Revisions (Confirmed: LEAP creates a new revision and freezes it)

This was confirmed rather than assumed: when a contract is amended, **LEAP re-freezes the entire document as a new revision** rather than editing in place. That shapes the whole update-handling design:

1. **Chunk-level change detection.** Since the whole document re-freezes even for a small edit, most chunks will be identical to the prior revision. Hash each chunk's normalized text at parse time. If a chunk's hash matches the prior revision, reuse its existing embedding and just relink it to the new revision — only genuinely changed chunks get re-embedded and re-extracted. This also gives you a change summary for free (the set of changed hashes tells you which sections actually differ) without a separate diffing step.
2. **Vector store.** Tag every chunk with contract ID + revision number. Mark the prior revision's chunks as superseded (don't delete — they stay queryable for history/audit) and default retrieval to the latest revision per contract ID.
3. **Knowledge graph.** Model versioning explicitly (full schema in Section 6) so re-extraction doesn't create duplicate or conflicting facts (e.g. two different termination liability percentages for the same contract with no way to know which is current).
4. **Governance/audit trail.** Log every freeze event with contract ID, revision number, changed-chunk count, and an auto-generated one-line change summary (an LLM can turn "these 3 chunks changed" into "termination liability schedule and Exhibit A pricing updated"). This is what makes the system defensible later when someone asks why an answer changed.
5. **Determinism matters.** Chunking rules must be stable across revisions — if the chunker draws boundaries differently between revision 1 and revision 2 of the same contract, unrelated chunks will falsely register as "changed," and the reprocessing-efficiency benefit is lost.

---

## 5. Contract Terminology Glossary

Written for someone new to contract analysis. Anchored to real structural patterns seen in Honeywell's own sample documents where useful.

| Term | Meaning |
|---|---|
| **Amendment** | A formal, mutually signed change to a contract *after* execution. Modifies specific terms (price, date, scope) while the rest stays in force. Usually requires the same signing authority as the original. |
| **Addendum** | Closely related to an amendment, but tends to *add* new terms rather than change existing ones. In practice the two words are often used interchangeably. |
| **Modification ("Mod")** | The government-contracting term for the same concept. Common on FAR-based contracts (e.g. the USCG contract structure). |
| **Task Order / Delivery Order** | Individual work orders issued against a master **IDIQ** (Indefinite Delivery/Indefinite Quantity) contract — each has its own scope and pricing but is governed by the master agreement's terms. |
| **Exhibit / Schedule / Annex** | A separate attachment that is legally *incorporated by reference* into the main agreement — physically separate, legally part of the same contract (e.g. "Exhibit A" as the Statement of Work, "Exhibit D" as a Termination Liability Schedule). |
| **Statement of Work (SOW)** | The document defining scope, deliverables, and milestones for the work — often itself an exhibit to a master agreement. |
| **Novation** | Replacing one of the contracting parties entirely with a new party. |
| **Assignment** | Transferring specific rights or obligations under a contract to a third party — narrower than novation, usually requires the other party's consent. |
| **Termination (for cause / for convenience)** | Ending a contract before its natural expiration. Often governed by a Termination Liability Schedule specifying what's owed at different points in the contract's life. |
| **Effective Date vs. Execution Date** | The date a contract *legally starts* vs. the date it was *actually signed* — these can differ. |
| **Precedence clause** | A clause ranking which document wins if two parts of the agreement conflict (e.g. cover sheet > Section I > Section II). This is structured business logic, not just boilerplate — worth capturing explicitly (see Section 6). |
| **Incorporation by reference** | Language like "as set forth in," "pursuant to," "attached hereto as," or "as defined in [AD1]" — this is what creates cross-document dependencies (see Section 6, reference resolution). |
| **Applicable Document (AD)** | A referenced external document (e.g. a customer's spacecraft interface control document) that the contract depends on but which may not be authored by Honeywell or even be part of your ingested corpus. |

---

## 6. Knowledge Graph Design

### Why a graph at all (not just embeddings)

Vector/embedding search is good at **fuzzy, semantic recall** — "does this contract have any exclusivity restrictions?" — where you don't know in advance which clause covers it, but wording may differ from the query. A knowledge graph is good at **precise, structured, multi-hop reasoning** — "what's the cumulative termination liability at month 9" or "which other contracts reference this same external ICD" — questions that require following exact relationships, not textual similarity. A pure vector search can retrieve a chunk that *mentions* a number, but can't reliably compute or trace a value across versions or contracts; a graph can, but only for what was explicitly extracted into it. In practice, a real query needs both: vector search finds the relevant entry point in unstructured text, then the graph expands outward along real relationships that wouldn't be textually similar at all (prior versions, referenced exhibits, related parties).

### Node types

| Node | Represents |
|---|---|
| `Contract` | The logical agreement — a stable ID that persists across all revisions. |
| `ContractVersion` | One specific revision/freeze event, with its own freeze date. Everything below hangs off this node, not off `Contract` directly. |
| `Party` | A contracting entity (buyer, seller, subcontractor). |
| `Document` | An exhibit, SOW, ICD, or other attachment — may be external / not yet ingested. |
| `Clause` | An obligation or term extracted from the contract text. |
| `Milestone` | A program milestone (e.g. ATP, PDR, CDR, delivery events) with its own schedule/date data. |
| `PaymentTerm` | A billing milestone — percentage, dollar amount, cumulative total — triggered by a `Milestone`. |
| `Unresolved` | A placeholder for a cited document that isn't in the graph yet (see reference resolution below). |

### Relationship types

| Relationship | Meaning |
|---|---|
| `Contract -[CURRENT_VERSION]-> ContractVersion` | Points to the latest revision; repointed on every new freeze. |
| `ContractVersion -[SUPERSEDES]-> ContractVersion` | Links a revision to the one it replaced, forming a version history chain. |
| `Party -[PARTY_TO]-> ContractVersion` | Who the agreement is between. |
| `ContractVersion -[INCLUDES_EXHIBIT]-> Document` | Attachments incorporated into this version. |
| `ContractVersion -[CONTAINS]-> Clause` | Terms/obligations belonging to this version. |
| `ContractVersion -[HAS_MILESTONE]-> Milestone` | Program milestones for this version. |
| `Milestone -[TRIGGERS_PAYMENT]-> PaymentTerm` | Links a milestone to the payment it triggers. |
| `Clause -[REFERENCES]-> Document` (or `Unresolved`) | Cross-document citation — the mechanism for handling "as set forth in Exhibit A" / "per AD1" language. |

### Reference resolution logic

Split references into two kinds — they resolve very differently:

- **Intra-document references** ("per Article 35," "per Exhibit A") point to something inside the same freeze event, and resolve immediately during extraction — no placeholder needed.
- **Inter-document references** ("AD1, [external document number]") point outside the current document. The extraction step should pull a structured citation — document number, revision if stated, title — not just note that a reference exists. The document number is the natural matching key.

When a citation resolves to nothing already in the graph, create an `Unresolved` placeholder keyed on the normalized document number (strip whitespace/case/punctuation so formatting variance doesn't break matching). Every newly ingested document gets checked against pending placeholders before creating a fresh `Document` node — on a match, the placeholder converts to a real node and every `REFERENCES` edge pointing at it gets relinked automatically.

Additional guardrails to build in:
- **Confidence score** on each `REFERENCES` edge from the LLM extraction step, so low-confidence citations can be reviewed rather than trusted blindly.
- **Fuzzy fallback** (title + revision similarity) if document-number matching fails, with ambiguous cases routed to human review rather than auto-matched — a wrong match on a legal citation is worse than an unresolved one.
- **Aging report** on placeholders — some references may never resolve (e.g. a customer-authored ICD delivered outside this pipeline entirely). Flag anything unresolved past a set period (e.g. 30–60 days) for legal/contracts review instead of letting it sit invisibly.

### Why this will be useful going forward

- **Audit trail** — walk `SUPERSEDES` to see exactly what a contract said before a given amendment, with the freeze date it changed.
- **Cross-contract analysis** — e.g. "which contracts reference the same external ICD," or "trace every obligation tied to Party X across all their agreements with Honeywell" — not answerable from text similarity alone.
- **Precise computable facts** — termination liability, payment milestones, and dates live as structured, queryable data rather than something an LLM has to re-read and hope to get right every time.
- **Governance/compliance queries** — e.g. surfacing every contract whose precedence clause or confidentiality marking meets a certain condition, at scale, exactly, rather than approximately.

---

## 7. Vector Storage: Neo4j vs. Vertex AI Data Store

There are two viable places to store chunk embeddings — worth deciding deliberately rather than defaulting to whichever tool comes up first.

**Option A — keep them separate (matches the original architecture diagram).** Vertex AI Data Store handles chunking, embedding, and vector search for full document text (Layer 1). Neo4j holds only the graph — `Contract`, `ContractVersion`, `Party`, `Clause`, `Milestone`, etc. (Layer 2) — cross-referenced to Vertex AI chunks by a shared chunk ID. Pro: Vertex AI's managed pipeline already does OCR/layout-parsing/chunking, so nothing is duplicated. Con: two systems to keep in sync, and a hybrid query (vector search → graph expand) requires a round trip between two services.

**Option B — native vectors in Neo4j.** Neo4j has supported vector indexes since version 5.13 (GA), and as of the 2026.01 release added a native Cypher `SEARCH` clause for querying them (the older `db.index.vector.queryNodes()` procedure still works but is deprecated as of the 2026.04 release). Embeddings get stored directly as a property on `Chunk`/`Clause` nodes and indexed. Pro: one system — a single Cypher query can combine vector similarity search *and* graph traversal, which is exactly the "vector finds the entry point, graph expands with precise relationships" pattern from Section 6. Con: Neo4j doesn't generate embeddings itself (an embedding model call is still required), and the vector index becomes something your team tunes/scales rather than something fully managed.

**Recommendation: don't treat it as all-or-nothing.** Use Vertex AI for what it's already good at — parsing and chunking raw documents at scale (rebuilding that inside Neo4j would be wasted effort). Also add a native vector index on the graph's `Chunk`/`Clause` nodes so an agent can run a single hybrid query instead of two service calls. Vertex AI can remain the primary managed vector store over full document chunks; Neo4j's index can cover a smaller, graph-native set (e.g. extracted clauses) for combined semantic + structural queries.

**Deployment.** Neo4j is cloud-agnostic, but given the rest of this stack is GCP (GCS, Vertex AI, IAM) and the ITAR/export-control sensitivity already flagged for aerospace contract content, keep Neo4j on GCP too — either AuraDB (Neo4j's managed offering, available via GCP Marketplace) or self-managed on GCP Compute/GKE. Confirm with IT Security (Section 10), but this is the sensible default.

**Creating a vector index on Neo4j nodes:**

```cypher
CREATE VECTOR INDEX chunkEmbeddings IF NOT EXISTS
FOR (c:Chunk)
ON c.embedding
OPTIONS { indexConfig: {
 `vector.dimensions`: 768,
 `vector.similarity_function`: 'cosine'
}}
```

Dimension count must match whatever embedding model generates the vectors (e.g. 768 or 1536, depending on the model — Neo4j doesn't decide this, it just needs to be told).

**Querying it (current Cypher `SEARCH` syntax):**

```cypher
MATCH (queryChunk) WHERE queryChunk.id = $inputChunkId
MATCH (c:Chunk)
  SEARCH c IN (
    VECTOR INDEX chunkEmbeddings
    FOR queryChunk.embedding
    LIMIT 10
  ) SCORE AS score
RETURN c.text, c.contractId, score
```

Extend this in the same query with a graph traversal (e.g. `MATCH (c)-[:CONTAINS]-(cl:Clause)`) to pull in related structured facts in one call — the actual payoff of putting vectors in the graph rather than a fully separate store.

**Keep these three steps distinct when building this — they are not one LLM call on a raw document:**
1. **Structural parsing** (deterministic) — Vertex AI's layout parser/Document AI turns a PDF into headings/tables/paragraphs as distinct blocks.
2. **Embedding generation** (embedding model, not a generative LLM) — takes chunk text, returns a fixed-length vector.
3. **Entity/relationship extraction** (LLM, scoped to individual chunks) — pulls parties/clauses/milestones/payment terms into the graph, per the schema in Section 6.

Feeding a whole raw document straight to an LLM and asking it to produce both the graph and the embeddings in one shot skips step 1 and is what causes the table-splitting and number-hallucination problems described in Section 8 (Chunking Strategy).

**Docs to check:**
- [Vector indexes — Cypher Manual](https://neo4j.com/docs/cypher-manual/current/indexes/semantic-indexes/vector-indexes/)
- [Vector search with filters in Neo4j](https://neo4j.com/blog/genai/vector-search-with-filters-in-neo4j-v2026-01-preview/)
- [Vector optimization — Neo4j Aura](https://neo4j.com/docs/aura/managing-instances/vector-optimization/)

---

## 8. Chunking Strategy

### Why naive fixed-size chunking fails for contracts

Contracts aren't uniform prose — they mix numbered clauses, tables, forms, and signature blocks. A fixed-size ("every N tokens") chunker has no idea a table row shouldn't be split from its header. A termination liability table row split from its header can produce a chunk like "15%, $X cumulative" with no indication of which contract, which version, or which month it belongs to — retrieval can serve it confidently and be wrong. The same failure applies to milestone tables, pricing schedules, and rate tables — anywhere a number's meaning depends on its row/column position, not the words around it.

### Structure-aware chunking principles

- **Chunk by structure, not size.** Boundaries should follow numbered sections/articles, table boundaries, and exhibit boundaries — not an arbitrary character count.
- **Tables stay atomic.** Keep a table as one chunk where possible; if it's too large (e.g. a multi-page rate table), split by row groups but always carry the column headers with every group so an isolated fragment stays interpretable.
- **Forms need field-aware extraction, not text chunking.** A structured form (numbered fields/blocks) chunked as flowing text usually scrambles reading order — extract as key-value pairs at the parsing stage instead.
- **Signature blocks go to structured extraction, not embeddings.** Signer name/title/date/party should feed directly into the graph (`Party -[SIGNED]-> ContractVersion` with a date property) — nobody searches semantically for "who signed this."
- **Boilerplate gets flagged, not ignored.** Standard clauses (Entire Agreement, Governing Law, Counterparts) repeat near-verbatim across contracts. Tag them distinctly so they don't dominate retrieval for substantive questions, and so they can later be compared against a standard-language library to flag deviations.

### Metadata required on every chunk

Contract ID, revision number, document type, section/article number + heading, page number, and a content-type flag (prose / table / form-field / signature / boilerplate). This is what lets the vector store filter to "latest revision only" by default, lets the chunk-hash comparison (Section 4) work, and gives the LLM extraction step enough context to know exactly where a fact came from.

### Tie-back to revision handling

Tables carrying financial/schedule data are good candidates for **structured extraction straight into the graph** (as `PaymentTerm`/`Milestone` nodes) rather than relying on vector similarity alone to "remember" a number correctly — the chunk/embedding remains useful as traceable source text, but shouldn't be the only place an exact figure lives.

---

## 9. Vertex AI Data Store — Does It Chunk Automatically?

**Short answer: yes, but with caveats worth verifying against your own documents before relying on it.**

Vertex AI Search (the product underlying "Vertex AI Data Store" in the architecture, part of what Google is now also branding under "Agent Search") supports **automatic document chunking**, and offers a **layout parser** specifically recommended when documents have rich structure — sections, paragraphs, tables, lists — which is exactly the Honeywell contract case. As of recent updates, the layout parser produces **context-aware chunks that respect document layout**: a chunk's content stays within a single layout entity (heading, subheading, list, table) rather than crossing boundaries — which lines up well with the structure-aware approach in Section 7.

Configurable parameters exist for tuning this — including chunk size and an "include ancestor headings" option (carrying a chunk's parent section heading along with it), which is close to the metadata-per-chunk approach recommended above. Support for parsing DOCX/PPTX/XLSX (not just PDF) via the layout parser has reached General Availability as of recent releases; PDF table recognition and reading-order accuracy have specifically been called out as improved when using Gemini-based layout parsing.

**What this does NOT do automatically — still your team's build:**
- Domain-specific metadata (contract ID, revision number, confidentiality marking, content-type flags for signature/boilerplate) — Vertex AI's chunker doesn't know what LEAP knows.
- The chunk-hash-based change-detection/reprocessing logic from Section 4.
- Routing table data into structured graph extraction rather than just embedding it.
- Field-aware extraction for forms and structured signature capture.

**What to verify directly in the documentation (and ideally test against your own sample SOW/contract with its multi-page tables) before finalizing the pipeline:**
- Whether the layout parser is Generally Available or still Public Preview for the specific file types you'll ingest.
- The actual default and maximum configurable chunk size for your data store type.
- Whether "include ancestor headings" and built-in table handling satisfy the "keep header attached to table row" requirement on their own, or whether post-processing is still needed.
- Whether chunking is deterministic/stable across re-ingestion of a revised document (needed for the hash-diffing approach in Section 4 to work correctly).

**Docs to check:**
- [Parse and chunk documents — Agent Search, Google Cloud](https://docs.cloud.google.com/generative-ai-app-builder/docs/parse-chunk-documents)
- [Use Document AI layout parser with Vertex AI RAG Engine](https://cloud.google.com/vertex-ai/generative-ai/docs/layout-parser-integration)
- [Prepare data for ingesting — Agent Search](https://docs.cloud.google.com/generative-ai-app-builder/docs/prepare-data)
- [Vertex AI Search release notes](https://docs.cloud.google.com/generative-ai-app-builder/docs/release-notes?hl=en) — check for the current GA/Preview status of any feature above before committing to it in production.

---

## 10. Open Questions & Who to Reach Out To

A consolidated list of everything flagged as needing confirmation from another team before implementation can be finalized.

**LEAP admin / IT team:**
- Does LEAP support outbound webhooks on freeze events, or only a pollable export/query API?
- Exact metadata fields exposed per document: contract ID, revision number, freeze timestamp, document type, confidentiality/distribution marking.
- Does LEAP already track applicable-document (AD) links between a contract and its referenced external documents? (Would remove a lot of reliance on LLM-based reference matching — see Section 6.)
- Authentication model and API rate limits for the backfill job.
- Does Workato have a native LEAP connector, or does this need to be built on Workato's generic REST/HTTP connector?

**Workato team:**
- Recipe design review for both the event-driven and batch-backfill recipes.
- Idempotency key strategy and dead-letter/alerting setup.

**IT Security / Data Governance:**
- Data classification tiers and how they map to access control in the Access & Governance layer.
- ITAR/export-control handling for aerospace/defense-related contract content.
- Confirm the confidentiality markings already used in LEAP/contracts map cleanly onto whatever access model gets built.

**Legal / Contracts team:**
- Confirm Honeywell's actual internal usage of "amendment" vs. "addendum" vs. "modification" (Section 5 reflects general industry usage — worth checking against internal convention).
- Own the review process for the "unresolved reference" aging queue (Section 6).

**GCP / Vertex AI:**
- Confirm current GA/Preview status of the layout parser for your specific file types, and test chunk-size/table-handling behavior directly against sample documents before finalizing (Section 8).

**Neo4j / graph modeling:**
- Validate the proposed schema (Section 6) against real extraction output from a pilot batch before committing to it at scale — some node/relationship types may need adjusting once real data is seen.

---

## 11. Suggested Build Order

1. **Phase 0 — Scoping & pilot set.** Confirm LEAP's webhook/metadata capabilities. Pick a small, deliberately messy pilot corpus covering signature pages, tables, multi-exhibit SOWs, government FAR contracts.
2. **Phase 1 — Ingestion & vectorization.** Stand up GCS landing zone with least-privilege IAM, build/validate the Vertex AI Data Store pipeline (parsing, structure-aware chunking, embedding) against the pilot set.
3. **Phase 2 — Knowledge graph.** Implement the schema in Section 6, build LLM extraction with an accuracy eval set (ground-truth answers for dollar figures, dates, percentages), then layer in reference resolution.
4. **Phase 3 — Access & governance.** Unified API, access control tied to identity and confidentiality classification, hybrid search, metadata/lineage.
5. **Cross-cutting, throughout:** governance/security and audit logging from day one; observability layered in before wide consumer rollout.
6. **Phase 4 — Consumer rollout.** Roll out to one business unit or contract type at a time, with a feedback loop back into the eval set.

---

*This document reflects design decisions and open questions as of the working session it was generated from. Update it as answers come back from the teams listed in Section 9, and before treating any item there as decided.*
