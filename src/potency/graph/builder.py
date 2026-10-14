"""Deterministic graph (re)build for one scope from stored passages and validated extractions.

Layers:
- structural (actor system:*): SourceDocument, SourcePassage, contains_passage, next_passage, supersedes,
  document_subject. Not evidence-bearing; they establish no scientific claim.
- rule-derived (actor rule:redaction-v1): one Redaction placeholder per "(b)(4)" occurrence, scoped to its passage.
- extracted (actor llm:<model>): Assertion nodes, entity nodes, and relation edges; review_state=unreviewed.
- rule-derived (actor rule:contradiction-v1): contradicts edges between current assertions with opposite polarity.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from psycopg.types.json import Jsonb

from potency import ids
from potency.db import scoped_session
from potency.extraction.validator import normalize_application_id
from potency.tenancy import Scope

BUILDER_VERSION = "graph-builder-v1"
STRUCT_ACTOR = "system:structure-v1"
META_ACTOR = "system:document-metadata-v1"
SOURCE_MGMT_ACTOR = "system:source-management-v1"
REDACTION_ACTOR = "rule:redaction-v1"
CONTRADICTION_ACTOR = "rule:contradiction-v1"
NOT_APPLICABLE = "not_applicable"
UNREVIEWED = "unreviewed"
MAX_ENTITY_REFS = 60

_REDACTION = re.compile(r"\(b\)\s*\(\d\)")


@dataclass
class Node:
    id: str
    type: str
    label: str
    attributes: dict
    source_refs: list
    evidence_bearing: bool
    actor: str
    version: str
    created_at: datetime
    review_state: str
    is_current: bool = True
    aliases: list = field(default_factory=list)
    quotes: list = field(default_factory=list)  # (passage_pk, quote) for evidence_ref


@dataclass
class Edge:
    id: str
    type: str
    src: str
    dst: str
    status: str
    attributes: dict
    source_refs: list
    evidence_bearing: bool
    actor: str
    version: str
    created_at: datetime
    review_state: str
    is_current: bool = True
    quotes: list = field(default_factory=list)


class GraphAccumulator:
    def __init__(self, scope: Scope):
        self.scope = scope
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, Edge] = {}
        self.alias_index: dict[tuple[str, str], str] = {}

    def nid(self, type_: str, key: str) -> str:
        return f"{self.scope.prefix}/{type_}/{key}"

    def add_node(self, node: Node) -> Node:
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            for a in [node.label, *node.aliases]:
                self.alias_index.setdefault((node.type, ids.slug(a)), node.id)
            return node
        return existing

    def add_edge(self, edge: Edge) -> Edge:
        return self.edges.setdefault(edge.id, edge)


def _ref(scope: Scope, p: dict, quote: str | None = None) -> dict:
    ref = {
        "scope": scope.as_dict(),
        "source_id": p["source_id"],
        "source_version": p["source_version"],
        "passage_id": p["passage_id"],
    }
    if quote is not None:
        ref["quote"] = quote
    return ref


def build_graph(scope: Scope, extractor_version: str | None = None, model: str | None = None) -> dict:
    with scoped_session(scope) as conn:
        docs = conn.execute("SELECT * FROM source_document ORDER BY id").fetchall()
        versions = conn.execute("SELECT * FROM source_version ORDER BY id").fetchall()
        passages = conn.execute(
            "SELECT id, source_id, source_version, passage_id, version_id, ordinal, text, text_sha256, page, section, "
            "authority, status, corpus_snapshot, is_current, first_imported_at FROM passage ORDER BY version_id, ordinal"
        ).fetchall()
        run = None
        if extractor_version is None:
            run = conn.execute(
                "SELECT id, extractor_version, model, prompt_version FROM extraction_run "
                "WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if run:
                extractor_version, model = run["extractor_version"], run["model"]
        else:
            run = conn.execute(
                "SELECT id, extractor_version, model, prompt_version FROM extraction_run WHERE extractor_version=%s "
                "AND model=%s ORDER BY started_at DESC LIMIT 1",
                (extractor_version, model),
            ).fetchone()
        extractions = []
        if extractor_version:
            extractions = conn.execute(
                "SELECT id, passage_fk, text_sha256, accepted, created_at FROM extraction_result "
                "WHERE status='ok' AND extractor_version=%s AND model=%s ORDER BY passage_fk",
                (extractor_version, model),
            ).fetchall()

    g = GraphAccumulator(scope)
    psg_by_pk = {p["id"]: p for p in passages}
    versions_by_source = defaultdict(list)
    for v in versions:
        versions_by_source[v["source_id"]].append(v)

    # Structural layer
    for d in docs:
        g.add_node(Node(
            id=g.nid("SourceDocument", d["source_id"]), type="SourceDocument", label=d["title"] or d["source_id"],
            attributes={
                "source_id": d["source_id"], "title": d["title"], "source_type": d["source_type"],
                "document_authority": d["authority"], "document_status": d["status"], "issuer": d["issuer"],
                "source_url": d["source_url"], "filename": d["filename"], "corpus_layer": d["corpus_layer"],
                "document_subject_metadata": d["subject_scope"],
                "versions": [
                    {"source_version": v["source_version"], "lifecycle": v["lifecycle"],
                     "corpus_snapshot": v["corpus_snapshot"], "issued": v["issued"],
                     "superseded_by": v["superseded_by"]}
                    for v in versions_by_source[d["source_id"]]
                ],
            },
            source_refs=[], evidence_bearing=False, actor=STRUCT_ACTOR, version=BUILDER_VERSION,
            created_at=d["first_imported_at"], review_state=NOT_APPLICABLE,
            is_current=any(v["lifecycle"] == "current" for v in versions_by_source[d["source_id"]]),
        ))
        _metadata_subjects(g, d)

    prev_in_version: dict[str, Node] = {}
    for p in passages:
        pnode = g.add_node(Node(
            id=passage_node_id(scope, p), type="SourcePassage", label=f"{p['source_id']} {p['passage_id']}",
            attributes={
                "passage_pk": p["id"], "source_id": p["source_id"], "source_version": p["source_version"],
                "passage_id": p["passage_id"], "page": p["page"], "section": p["section"],
                "document_authority": p["authority"],
                "document_status": p["status"], "text_sha256": p["text_sha256"],
                "corpus_snapshot": p["corpus_snapshot"], "lifecycle": "current" if p["is_current"] else "superseded",
            },
            source_refs=[_ref(scope, p)], evidence_bearing=False, actor=STRUCT_ACTOR, version=BUILDER_VERSION,
            created_at=p["first_imported_at"], review_state=NOT_APPLICABLE, is_current=p["is_current"],
        ))
        doc_node = g.nid("SourceDocument", p["source_id"])
        g.add_edge(_sys_edge(g, "contains_passage", doc_node, pnode.id, p["first_imported_at"], p["is_current"]))
        prev = prev_in_version.get(p["version_id"])
        if prev is not None:
            g.add_edge(_sys_edge(g, "next_passage", prev.id, pnode.id, p["first_imported_at"], p["is_current"]))
        prev_in_version[p["version_id"]] = pnode
        for n, m in enumerate(_REDACTION.finditer(p["text"]), start=1):
            rid = g.nid("Redaction", f"{ids.short_hash(p['id'])}-{n}")
            g.add_node(Node(
                id=rid, type="Redaction", label=f"redaction #{n} in {p['passage_id']}",
                attributes={"marker": m.group(0), "occurrence": n, "placeholder_scope": "passage",
                            "note": "Withheld content; not linked to redactions in other passages."},
                source_refs=[_ref(scope, p, m.group(0))], evidence_bearing=True, actor=REDACTION_ACTOR,
                version=BUILDER_VERSION, created_at=p["first_imported_at"], review_state=UNREVIEWED,
                is_current=p["is_current"], quotes=[(p["id"], m.group(0))],
            ))
            g.add_edge(Edge(
                id=f"{scope.prefix}/edge/{ids.short_hash('contains_redaction', pnode.id, rid)}",
                type="contains_redaction", src=pnode.id, dst=rid, status="redacted", attributes={},
                source_refs=[_ref(scope, p, m.group(0))], evidence_bearing=True, actor=REDACTION_ACTOR,
                version=BUILDER_VERSION, created_at=p["first_imported_at"], review_state=UNREVIEWED,
                is_current=p["is_current"], quotes=[(p["id"], m.group(0))],
            ))

    # Declared supersession between passage revisions
    for v in versions:
        if v["superseded_by"]:
            for p in passages:
                if p["version_id"] != v["id"]:
                    continue
                newer = next((q for q in passages if q["version_id"] == v["superseded_by"]
                              and q["passage_id"] == p["passage_id"]), None)
                if newer:
                    g.add_edge(Edge(
                        id=f"{scope.prefix}/edge/{ids.short_hash('supersedes', newer['id'], p['id'])}",
                        type="supersedes", src=passage_node_id(scope, newer), dst=passage_node_id(scope, p),
                        status="system", attributes={"declared": True, "superseded_at": str(v["superseded_at"])},
                        source_refs=[_ref(scope, newer), _ref(scope, p)], evidence_bearing=False,
                        actor=SOURCE_MGMT_ACTOR, version=BUILDER_VERSION, created_at=v["superseded_at"],
                        review_state=NOT_APPLICABLE, is_current=True,
                    ))

    # Extracted layer
    actor = f"llm:{model}" if model and not str(model).startswith("rule-") else f"rule:{model}"
    ex_version = f"{extractor_version}/{run['prompt_version'] if run else 'unknown'}"
    relation_index: dict[tuple, list[tuple[str, bool, dict]]] = defaultdict(list)
    n_assertions = 0
    for ex in extractions:
        p = psg_by_pk.get(ex["passage_fk"])
        if p is None or p["text_sha256"] != ex["text_sha256"]:
            continue
        pnode_id = passage_node_id(scope, p)
        for idx, a in enumerate(ex["accepted"]):
            n_assertions += 1
            quote = a["exact_quote"]
            ref = _ref(scope, p, quote)
            aid = g.nid("Assertion", ids.short_hash(p["id"], p["text_sha256"], extractor_version, model, idx))
            g.add_node(Node(
                id=aid, type="Assertion", label=a["summary"][:160],
                attributes={
                    "assertion_type": a["assertion_type"], "assertion_state": a["assertion_state"],
                    "speaker": a["speaker"], "applies_to": a["applies_to"], "event_date": a.get("event_date"),
                    "summary": a["summary"], "quote": quote, "document_authority": p["authority"],
                    "document_status": p["status"], "passage_has_redaction": a.get("passage_has_redaction", False),
                    "extraction_result_id": ex["id"], "lifecycle": "current" if p["is_current"] else "superseded",
                    "conflict_state": None,
                },
                source_refs=[ref], evidence_bearing=True, actor=actor, version=ex_version,
                created_at=ex["created_at"], review_state=UNREVIEWED, is_current=p["is_current"],
                quotes=[(p["id"], quote)],
            ))
            g.add_edge(_ev_edge(g, "supported_by", aid, pnode_id, a, ref, actor, ex_version, ex["created_at"],
                                p["is_current"], (p["id"], quote), key=(aid, pnode_id)))
            key_to_node: dict[str, str] = {}
            for ent in a["entities"]:
                en = _entity_node(g, ent, aid, ref, actor, ex_version, ex["created_at"], p, quote)
                key_to_node[ent["key"]] = en
                g.add_edge(_ev_edge(g, "about", aid, en, a, ref, actor, ex_version, ex["created_at"], p["is_current"],
                                    (p["id"], quote), key=(aid, en)))
            if a["applies_to"].get("basis") == "explicit_in_passage":
                for t, vals in (("Product", a["applies_to"]["products"]), ("Application", a["applies_to"]["application_ids"]),
                                ("Indication", a["applies_to"]["indications"])):
                    for v in vals:
                        tn = _subject_node(g, t, v, ex["created_at"])
                        g.add_edge(_ev_edge(g, "applies_to", aid, tn, a, ref, actor, ex_version, ex["created_at"],
                                            p["is_current"], (p["id"], quote), key=(aid, tn)))
            for ridx, rel in enumerate(a["relations"]):
                src = key_to_node.get(rel["subject"])
                if src is None:
                    continue
                if rel["type"] == "used_as":
                    dst = _role_node(g, rel["role"], ex["created_at"])
                else:
                    dst = key_to_node.get(rel["object"])
                    if dst is None:
                        continue
                attrs = {"assertion_id": aid, "role": rel.get("role"), "negated": rel.get("negated", False),
                         "speaker": a["speaker"], "assertion_type": a["assertion_type"]}
                e = g.add_edge(Edge(
                    id=f"{scope.prefix}/edge/{ids.short_hash(rel['type'], aid, ridx)}", type=rel["type"], src=src,
                    dst=dst, status=a["assertion_state"], attributes=attrs, source_refs=[ref], evidence_bearing=True,
                    actor=actor, version=ex_version, created_at=ex["created_at"], review_state=UNREVIEWED,
                    is_current=p["is_current"], quotes=[(p["id"], quote)],
                ))
                if e.is_current:
                    relation_index[(src, rel["type"], dst, rel.get("role"))].append(
                        (aid, bool(rel.get("negated")), {"authority": p["authority"], "passage": p["id"]}))

    n_conflicts = _contradictions(g, relation_index)
    stats = _write(scope, g, run["id"] if run else None)
    stats.update({"assertions": n_assertions, "contradictions": n_conflicts, "extractor_version": extractor_version,
                  "extraction_model": model})
    return stats


def passage_node_id(scope: Scope, p: dict) -> str:
    return f"{scope.prefix}/SourcePassage/{p['source_id']}@{p['source_version']}#{p['passage_id']}"


def _sys_edge(g: GraphAccumulator, type_: str, src: str, dst: str, created_at, is_current: bool) -> Edge:
    return Edge(
        id=f"{g.scope.prefix}/edge/{ids.short_hash(type_, src, dst)}", type=type_, src=src, dst=dst, status="system",
        attributes={}, source_refs=[], evidence_bearing=False, actor=STRUCT_ACTOR, version=BUILDER_VERSION,
        created_at=created_at, review_state=NOT_APPLICABLE, is_current=is_current,
    )


def _ev_edge(g, type_, src, dst, a, ref, actor, version, created_at, is_current, quote, key) -> Edge:
    return Edge(
        id=f"{g.scope.prefix}/edge/{ids.short_hash(type_, *key)}", type=type_, src=src, dst=dst,
        status=a["assertion_state"], attributes={"speaker": a["speaker"], "assertion_type": a["assertion_type"]},
        source_refs=[ref], evidence_bearing=True, actor=actor, version=version, created_at=created_at,
        review_state=UNREVIEWED, is_current=is_current, quotes=[quote],
    )


def _subject_key(type_: str, value: str) -> str:
    if type_ == "Application":
        norm = normalize_application_id(value)
        return ids.slug(norm) if norm else ids.slug(value)
    return ids.slug(value)


def _subject_node(g: GraphAccumulator, type_: str, value: str, created_at, aliases=()) -> str:
    key = _subject_key(type_, value)
    existing = g.alias_index.get((type_, key))
    if existing:
        return existing
    label = normalize_application_id(value) if type_ == "Application" and normalize_application_id(value) else value
    node = g.add_node(Node(
        id=g.nid(type_, key), type=type_, label=label, attributes={"basis": "document_metadata_or_extraction"},
        source_refs=[], evidence_bearing=False, actor=META_ACTOR, version=BUILDER_VERSION, created_at=created_at,
        review_state=NOT_APPLICABLE, aliases=[a for a in aliases if a],
    ))
    return node.id


def _metadata_subjects(g: GraphAccumulator, d: dict) -> None:
    subj = d["subject_scope"] or {}
    doc_node = g.nid("SourceDocument", d["source_id"])
    targets = []
    product = subj.get("product") or subj.get("product_id")
    if product:
        targets.append(_subject_node(g, "Product", product, d["first_imported_at"],
                                     aliases=[subj.get("product_id"), subj.get("proper_name"), subj.get("product")]))
    if subj.get("application_id"):
        targets.append(_subject_node(g, "Application", subj["application_id"], d["first_imported_at"]))
    if subj.get("indication"):
        targets.append(_subject_node(g, "Indication", subj["indication"], d["first_imported_at"]))
    for t in targets:
        g.add_edge(Edge(
            id=f"{g.scope.prefix}/edge/{ids.short_hash('document_subject', doc_node, t)}", type="document_subject",
            src=doc_node, dst=t, status="system", attributes={"basis": "document-level subject_scope metadata"},
            source_refs=[], evidence_bearing=False, actor=META_ACTOR, version=BUILDER_VERSION,
            created_at=d["first_imported_at"], review_state=NOT_APPLICABLE,
        ))


def _role_node(g: GraphAccumulator, role: str, created_at) -> str:
    node = g.add_node(Node(
        id=g.nid("AssayRole", role), type="AssayRole", label=role, attributes={"role": role}, source_refs=[],
        evidence_bearing=False, actor=STRUCT_ACTOR, version=BUILDER_VERSION, created_at=created_at,
        review_state=NOT_APPLICABLE,
    ))
    return node.id


def _entity_node(g, ent, aid, ref, actor, version, created_at, p, quote) -> str:
    type_, name = ent["type"], ent["name"].strip()
    if ent.get("redacted"):
        key = f"redacted-{ids.short_hash(aid, ent['key'])}"
        node = g.add_node(Node(
            id=g.nid(type_, key), type=type_, label=f"[redacted {type_}] {name}"[:160],
            attributes={"redacted": True, "placeholder_scope": "passage", "visible_description": name,
                        "mention_count": 0},
            source_refs=[], evidence_bearing=True, actor=actor, version=version, created_at=created_at,
            review_state=UNREVIEWED, is_current=False,
        ))
    elif type_ in ("Product", "Application", "Indication"):
        nid = _subject_node(g, type_, name, created_at)
        node = g.nodes[nid]
    else:
        key = _subject_key(type_, name)
        nid = g.alias_index.get((type_, key)) or g.nid(type_, key)
        node = g.add_node(Node(
            id=nid, type=type_, label=name[:160], attributes={"redacted": False, "mention_count": 0},
            source_refs=[], evidence_bearing=True, actor=actor, version=version, created_at=created_at,
            review_state=UNREVIEWED, is_current=False,
        ))
    if node.evidence_bearing:
        node.attributes["mention_count"] = node.attributes.get("mention_count", 0) + 1
        node.is_current = node.is_current or p["is_current"]
        if len(node.source_refs) < MAX_ENTITY_REFS and ref not in node.source_refs:
            node.source_refs.append(ref)
            node.quotes.append((p["id"], quote))
        node.created_at = min(node.created_at, created_at)
    return node.id


def _contradictions(g: GraphAccumulator, relation_index: dict) -> int:
    n = 0
    for (src, rtype, dst, role), items in sorted(relation_index.items(), key=lambda kv: tuple(x or "" for x in kv[0])):
        pos = [i for i in items if not i[1]]
        neg = [i for i in items if i[1]]
        for a_id, _, a_meta in pos:
            for b_id, _, b_meta in neg:
                if a_id == b_id:
                    continue
                a, b = g.nodes[a_id], g.nodes[b_id]
                eid = f"{g.scope.prefix}/edge/{ids.short_hash('contradicts', a_id, b_id)}"
                g.add_edge(Edge(
                    id=eid, type="contradicts", src=a_id, dst=b_id, status="unresolved",
                    attributes={
                        "rule": "same subject, relation, object and role asserted with opposite polarity",
                        "relation": {"subject": src, "type": rtype, "object": dst, "role": role},
                        "equal_document_authority": a_meta["authority"] == b_meta["authority"],
                        "resolution": "unresolved: no declared supersession; issue dates are not used as an override",
                    },
                    source_refs=a.source_refs + b.source_refs, evidence_bearing=True, actor=CONTRADICTION_ACTOR,
                    version=BUILDER_VERSION, created_at=max(a.created_at, b.created_at), review_state=UNREVIEWED,
                    is_current=True, quotes=a.quotes + b.quotes,
                ))
                for x, y in ((a, b_id), (b, a_id)):
                    x.attributes["conflict_state"] = "unresolved_conflict"
                    x.attributes.setdefault("conflicts_with", [])
                    if y not in x.attributes["conflicts_with"]:
                        x.attributes["conflicts_with"].append(y)
                n += 1
    return n


def _write(scope: Scope, g: GraphAccumulator, extraction_run_id: str | None) -> dict:
    build_id = "graph-" + ids.short_hash(scope.prefix, BUILDER_VERSION, *sorted(g.nodes), *sorted(g.edges), n=12)
    node_rows = [
        (n.id, scope.tenant_id, scope.project_id, n.type, n.label, n.aliases, Jsonb(n.attributes), Jsonb(n.source_refs),
         n.evidence_bearing, n.actor, n.version, n.created_at, n.review_state, n.is_current, build_id)
        for n in g.nodes.values()
    ]
    edge_rows = [
        (e.id, scope.tenant_id, scope.project_id, e.type, e.src, e.dst, e.status, Jsonb(e.attributes),
         Jsonb(e.source_refs), e.evidence_bearing, e.actor, e.version, e.created_at, e.review_state, e.is_current,
         build_id)
        for e in g.edges.values()
    ]
    ev_rows = set()
    for kind, items in (("node", g.nodes.values()), ("edge", g.edges.values())):
        for it in items:
            for pk, quote in it.quotes:
                ev_rows.add((kind, it.id, pk, quote, scope.tenant_id, scope.project_id))
    with scoped_session(scope) as conn:
        conn.execute("DELETE FROM evidence_ref")
        conn.execute("DELETE FROM kg_edge")
        conn.execute("DELETE FROM kg_node")
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO kg_node (id, tenant_id, project_id, type, label, aliases, attributes, source_refs, "
            "evidence_bearing, actor, version, created_at, review_state, is_current, graph_build_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            node_rows,
        )
        cur.executemany(
            "INSERT INTO kg_edge (id, tenant_id, project_id, type, src, dst, status, attributes, source_refs, "
            "evidence_bearing, actor, version, created_at, review_state, is_current, graph_build_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            edge_rows,
        )
        cur.executemany(
            "INSERT INTO evidence_ref (owner_kind, owner_id, passage_fk, quote, tenant_id, project_id) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            sorted(ev_rows),
        )
        type_counts = defaultdict(int)
        for n in g.nodes.values():
            type_counts[n.type] += 1
        etype_counts = defaultdict(int)
        for e in g.edges.values():
            etype_counts[e.type] += 1
        stats = {"graph_build_id": build_id, "nodes": len(node_rows), "edges": len(edge_rows),
                 "evidence_refs": len(ev_rows), "node_types": dict(sorted(type_counts.items())),
                 "edge_types": dict(sorted(etype_counts.items()))}
        conn.execute(
            "INSERT INTO graph_build (id, tenant_id, project_id, builder_version, extraction_run_id, stats) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET stats=EXCLUDED.stats, created_at=now()",
            (build_id, scope.tenant_id, scope.project_id, BUILDER_VERSION, extraction_run_id, Jsonb(stats)),
        )
    return stats


def latest_graph_build(conn) -> str | None:
    row = conn.execute("SELECT graph_build_id FROM kg_node LIMIT 1").fetchone()
    return row["graph_build_id"] if row else None
