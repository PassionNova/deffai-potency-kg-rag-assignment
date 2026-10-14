"""Graph retrieval: select seeds, traverse a bounded neighborhood one hop per query, and map the traversal back
to original source passages.

Traversal policy (recorded in every trace as `traversal_limits`):
- direction: undirected (an edge may be followed from either endpoint); one edge = one hop.
- seeds: entity/role nodes whose label word-similarity to the question >= ENTITY_SEED_MIN_SIM (top N), plus the
  SourcePassage and Assertion nodes attached to the top-k vector passages.
- per hop, each frontier node expands at most `per_node_cap` current edges, ranked by edge-type weight, then by
  trigram similarity of the neighbor label to the question, then edge id.
- at most `max_hops` hops and `max_nodes` visited nodes in total.

Scoring (graph-v2): every seed is an origin. Each visited node keeps the best decayed path score per origin
(score x edge weight x hop decay per hop). A passage's graph score is the SUM over distinct origins of the best path
reaching it (through its SourcePassage node, an evidence-bearing node quoting it, or an evidence-bearing edge
quoting it). All seeds derived from one vector hit share one origin, so a passage found only by vector search
contributes its similarity once, while a passage connected to several matched concepts accumulates support.
"""

from dataclasses import asdict, dataclass, field

from potency.retrieval.vector import passage_similarities, vector_search

GRAPH_RETRIEVER_VERSION = "graph-v2"

EDGE_WEIGHTS = {
    "supported_by": 1.0,
    "contradicts": 1.0,
    "concluded_in": 1.0,
    "about": 0.9,
    "used_as": 0.9,
    "measures": 0.9,
    "proposed_in": 0.9,
    "requested_in": 0.9,
    "revised_by": 0.9,
    "supports": 0.8,
    "supersedes": 0.6,
    "cites": 0.6,
    "part_of": 0.6,
    "applies_to": 0.5,
    "next_passage": 0.4,
    "contains_redaction": 0.3,
    "contains_passage": 0.1,
    "document_subject": 0.1,
}
ENTITY_SEED_TYPES = [
    "Assay", "MeasuredAttribute", "PotencyStrategy", "RegulatoryInteraction", "ReviewConclusion", "Regulation",
    "Guidance", "AssayRole", "Product", "Application", "Indication",
]
ENTITY_SEED_MIN_SIM = 0.6
HOP_DECAY = 0.7
EVIDENCE_NODE_TYPES = ("Assertion", "Redaction")


@dataclass
class TraversalLimits:
    direction: str = "undirected"
    hop_counting: str = "one edge traversal = one hop; seeds are hop 0"
    max_hops: int = 2
    per_node_cap: int = 8
    max_nodes: int = 60
    entity_seed_limit: int = 8
    entity_seed_min_word_similarity: float = ENTITY_SEED_MIN_SIM
    vector_seed_k: int = 6
    edge_weights: dict = field(default_factory=lambda: dict(EDGE_WEIGHTS))
    hop_decay: float = HOP_DECAY
    passage_score: str = "sum over distinct seed origins of the best decayed path score reaching the passage"


@dataclass
class GraphRetrieval:
    seeds: list[dict]
    traversed_edges: list[dict]
    visited: dict[str, dict]
    candidates: dict[str, dict]
    limits: dict


_EXPAND = """
WITH frontier AS (SELECT unnest(%(frontier)s::text[]) AS node_id),
cand AS (
  SELECT e.id AS edge_id, e.type, f.node_id AS from_node,
         CASE WHEN e.src = f.node_id THEN e.dst ELSE e.src END AS to_node,
         e.evidence_bearing,
         ROW_NUMBER() OVER (
           PARTITION BY f.node_id
           ORDER BY coalesce((%(weights)s::jsonb ->> e.type)::float, 0.2) DESC,
                    similarity(n.label, %(q)s) DESC, e.id
         ) AS rn
  FROM frontier f
  JOIN kg_edge e ON (e.src = f.node_id OR e.dst = f.node_id) AND e.is_current
  JOIN kg_node n ON n.id = CASE WHEN e.src = f.node_id THEN e.dst ELSE e.src END AND n.is_current
  WHERE NOT (e.id = ANY(%(visited_edges)s::text[]))
)
SELECT c.edge_id, c.type, c.from_node, c.to_node, c.evidence_bearing, n.type AS to_type, n.attributes AS to_attrs
FROM cand c JOIN kg_node n ON n.id = c.to_node
WHERE c.rn <= %(cap)s
ORDER BY c.from_node, c.rn
"""


def graph_retrieve(conn, question: str, qvec, model_id: str, limits: TraversalLimits | None = None) -> GraphRetrieval:
    import json

    lim = limits or TraversalLimits()
    seeds: list[dict] = []
    entity_rows = conn.execute(
        """
        SELECT id, type, label, word_similarity(label, %(q)s) AS s FROM kg_node
        WHERE is_current AND type = ANY(%(types)s) AND length(label) >= 4
          AND word_similarity(label, %(q)s) >= %(min)s
        ORDER BY s DESC, id LIMIT %(n)s
        """,
        {"q": question, "types": ENTITY_SEED_TYPES, "min": lim.entity_seed_min_word_similarity,
         "n": lim.entity_seed_limit},
    ).fetchall()
    for r in entity_rows:
        seeds.append({"node_id": r["id"], "type": r["type"], "label": r["label"], "via": "entity_label_match",
                      "origin": r["id"], "score": round(float(r["s"]), 4)})
    for r in vector_search(conn, qvec, model_id, lim.vector_seed_k):
        nodes = conn.execute(
            """
            SELECT n.id, n.type FROM kg_node n WHERE n.is_current AND n.type = 'SourcePassage'
              AND n.attributes->>'passage_pk' = %(pk)s
            UNION
            SELECT e.src, 'Assertion' FROM kg_edge e JOIN kg_node n ON n.id = e.dst
            WHERE e.type = 'supported_by' AND e.is_current AND n.attributes->>'passage_pk' = %(pk)s
            """,
            {"pk": r["passage_fk"]},
        ).fetchall()
        for n in nodes:
            seeds.append({"node_id": n["id"], "type": n["type"], "via": "vector_passage", "passage": r["passage_fk"],
                          "origin": f"vector:{r['passage_fk']}", "score": round(float(r["sim"]), 4)})

    visited: dict[str, dict] = {}
    for s in seeds:
        v = visited.get(s["node_id"])
        if v is None and len(visited) < lim.max_nodes:
            visited[s["node_id"]] = {"hop": 0, "score": s["score"], "type": s["type"], "origins": {s["origin"]: s["score"]}}
        elif v is not None:
            v["origins"][s["origin"]] = max(v["origins"].get(s["origin"], 0.0), s["score"])
            v["score"] = max(v["score"], s["score"])
    traversed: list[dict] = []
    visited_edges: set[str] = set()
    frontier = list(visited)
    weights_json = json.dumps(lim.edge_weights)
    node_attrs: dict[str, dict] = {}
    edge_origin_scores: dict[str, dict[str, float]] = {}
    for hop in range(1, lim.max_hops + 1):
        if not frontier or len(visited) >= lim.max_nodes:
            break
        rows = conn.execute(
            _EXPAND,
            {"frontier": frontier, "weights": weights_json, "q": question, "visited_edges": list(visited_edges),
             "cap": lim.per_node_cap},
        ).fetchall()
        nxt = []
        for r in rows:
            if r["edge_id"] in visited_edges:
                continue
            new_node = r["to_node"] not in visited
            if new_node and len(visited) >= lim.max_nodes:
                continue
            visited_edges.add(r["edge_id"])
            factor = lim.edge_weights.get(r["type"], 0.2) * lim.hop_decay
            src = visited[r["from_node"]]
            edge_origins = {o: s * factor for o, s in src["origins"].items()}
            score = max(edge_origins.values())
            traversed.append({"edge_id": r["edge_id"], "type": r["type"], "from": r["from_node"], "to": r["to_node"],
                              "hop": hop, "evidence_bearing": r["evidence_bearing"], "score": round(score, 4)})
            edge_origin_scores[r["edge_id"]] = edge_origins
            node_attrs[r["to_node"]] = r["to_attrs"]
            if new_node:
                visited[r["to_node"]] = {"hop": hop, "score": score, "type": r["to_type"], "origins": dict(edge_origins)}
                nxt.append(r["to_node"])
            else:
                dst = visited[r["to_node"]]
                for o, s in edge_origins.items():
                    dst["origins"][o] = max(dst["origins"].get(o, 0.0), s)
                dst["score"] = max(dst["score"], score)
        frontier = nxt

    candidates: dict[str, dict] = {}

    def add(pk: str, origins: dict[str, float], quote: str | None, via: str):
        c = candidates.setdefault(pk, {"origins": {}, "quotes": [], "via": []})
        for o, s in origins.items():
            c["origins"][o] = max(c["origins"].get(o, 0.0), s)
        if quote and quote not in c["quotes"]:
            c["quotes"].append(quote)
        if via not in c["via"]:
            c["via"].append(via)

    passage_nodes = [nid for nid, v in visited.items() if v["type"] == "SourcePassage"]
    if passage_nodes:
        for r in conn.execute(
            "SELECT id, attributes->>'passage_pk' AS pk FROM kg_node WHERE id = ANY(%s)", (passage_nodes,)
        ).fetchall():
            add(r["pk"], visited[r["id"]]["origins"], None, f"passage_node@hop{visited[r['id']]['hop']}")
    evidence_nodes = [nid for nid, v in visited.items() if v["type"] in EVIDENCE_NODE_TYPES]
    evidence_edges = [t["edge_id"] for t in traversed if t["evidence_bearing"]]
    if evidence_nodes or evidence_edges:
        for r in conn.execute(
            """
            SELECT er.owner_kind, er.owner_id, er.passage_fk, er.quote FROM evidence_ref er
            JOIN passage p ON p.id = er.passage_fk AND p.is_current
            WHERE (er.owner_kind = 'node' AND er.owner_id = ANY(%s)) OR (er.owner_kind = 'edge' AND er.owner_id = ANY(%s))
            ORDER BY er.owner_id, er.passage_fk
            """,
            (evidence_nodes, evidence_edges),
        ).fetchall():
            origins = (visited[r["owner_id"]]["origins"] if r["owner_kind"] == "node"
                       else edge_origin_scores[r["owner_id"]])
            add(r["passage_fk"], origins, r["quote"], f"{r['owner_kind']}_evidence_ref")

    sims = passage_similarities(conn, qvec, model_id, list(candidates))
    for pk, c in candidates.items():
        s = sims.get(pk)
        c["vector_sim"] = round(float(s["sim"]), 4) if s else 0.0
        c["best_chunk"] = (s["char_start"], s["char_end"]) if s else None
        origins = c.pop("origins")
        c["graph_score"] = round(sum(origins.values()), 4)
        c["supporting_origins"] = len(origins)

    limits_out = asdict(lim)
    return GraphRetrieval(seeds=seeds, traversed_edges=traversed, visited=visited, candidates=candidates,
                          limits=limits_out)
