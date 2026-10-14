"""Initial evidence store: sources, passages, embeddings, graph, runs, row-level security.

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-14
"""

import os

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "potency_app"

SCOPED_TABLES = [
    "source_document",
    "source_version",
    "passage",
    "passage_text_revision",
    "import_run",
    "import_event",
    "index_version",
    "passage_chunk",
    "embedding_cache",
    "extraction_run",
    "extraction_result",
    "extraction_coverage",
    "graph_build",
    "kg_node",
    "kg_edge",
    "evidence_ref",
    "run_record",
    "trace",
    "answer",
    "usage_ledger",
]

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE source_document (
  id                text PRIMARY KEY,
  tenant_id         text NOT NULL,
  project_id        text NOT NULL,
  source_id         text NOT NULL,
  title             text,
  source_type       text,
  authority         text,
  status            text,
  issuer            text,
  source_url        text,
  filename          text,
  corpus_layer      text,
  subject_scope     jsonb NOT NULL DEFAULT '{}',
  first_imported_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, project_id, source_id)
);

CREATE TABLE source_version (
  id              text PRIMARY KEY,
  tenant_id       text NOT NULL,
  project_id      text NOT NULL,
  source_id       text NOT NULL,
  source_version  text NOT NULL,
  document_id     text NOT NULL REFERENCES source_document(id),
  corpus_snapshot text NOT NULL,
  issued          text,
  retrieved_at    text,
  authority       text,
  status          text,
  lifecycle       text NOT NULL DEFAULT 'current' CHECK (lifecycle IN ('current', 'superseded')),
  superseded_by   text REFERENCES source_version(id),
  superseded_at   timestamptz,
  import_run_id   text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, project_id, source_id, source_version)
);

CREATE TABLE passage (
  id                 text PRIMARY KEY,
  tenant_id          text NOT NULL,
  project_id         text NOT NULL,
  source_id          text NOT NULL,
  source_version     text NOT NULL,
  passage_id         text NOT NULL,
  version_id         text NOT NULL REFERENCES source_version(id),
  corpus_snapshot    text NOT NULL,
  ordinal            integer NOT NULL,
  text               text NOT NULL,
  text_sha256        text NOT NULL,
  locator            jsonb NOT NULL,
  page               integer,
  section            text,
  authority          text,
  status             text,
  subject_scope      jsonb NOT NULL DEFAULT '{}',
  record             jsonb NOT NULL,
  is_current         boolean NOT NULL DEFAULT true,
  tsv                tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
  first_imported_at  timestamptz NOT NULL DEFAULT now(),
  last_import_run_id text NOT NULL,
  UNIQUE (tenant_id, project_id, source_id, source_version, passage_id)
);
CREATE INDEX passage_tsv_idx ON passage USING gin (tsv);
CREATE INDEX passage_scope_current_idx ON passage (tenant_id, project_id) WHERE is_current;

CREATE TABLE passage_text_revision (
  passage_fk    text NOT NULL REFERENCES passage(id),
  tenant_id     text NOT NULL,
  project_id    text NOT NULL,
  text_sha256   text NOT NULL,
  text          text NOT NULL,
  replaced_at   timestamptz NOT NULL DEFAULT now(),
  import_run_id text NOT NULL,
  PRIMARY KEY (passage_fk, text_sha256)
);

CREATE TABLE import_run (
  id              text PRIMARY KEY,
  tenant_id       text NOT NULL,
  project_id      text NOT NULL,
  corpus_snapshot text,
  started_at      timestamptz NOT NULL,
  finished_at     timestamptz,
  inputs          jsonb NOT NULL DEFAULT '[]',
  counts          jsonb NOT NULL DEFAULT '{}',
  report          jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE import_event (
  run_id      text NOT NULL REFERENCES import_run(id),
  tenant_id   text NOT NULL,
  project_id  text NOT NULL,
  record_key  text NOT NULL,
  source_id   text,
  outcome     text NOT NULL CHECK (outcome IN ('imported', 'unchanged', 'updated', 'failed', 'omitted')),
  reason      text,
  PRIMARY KEY (run_id, record_key)
);

CREATE TABLE index_version (
  id              text PRIMARY KEY,
  tenant_id       text NOT NULL,
  project_id      text NOT NULL,
  embedding_model text NOT NULL,
  dims            integer NOT NULL,
  chunker_version text NOT NULL,
  corpus_snapshot text,
  status          text NOT NULL CHECK (status IN ('active', 'inactive')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  stats           jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE passage_chunk (
  id                text PRIMARY KEY,
  tenant_id         text NOT NULL,
  project_id        text NOT NULL,
  passage_fk        text NOT NULL REFERENCES passage(id),
  chunker_version   text NOT NULL,
  chunk_index       integer NOT NULL,
  char_start        integer NOT NULL,
  char_end          integer NOT NULL,
  embed_text_sha256 text NOT NULL,
  UNIQUE (passage_fk, chunker_version, chunk_index)
);
CREATE INDEX passage_chunk_sha_idx ON passage_chunk (embed_text_sha256);

CREATE TABLE embedding_cache (
  tenant_id   text NOT NULL,
  project_id  text NOT NULL,
  model_id    text NOT NULL,
  text_sha256 text NOT NULL,
  embedding   vector(768) NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, project_id, model_id, text_sha256)
);
CREATE INDEX embedding_cache_hnsw ON embedding_cache USING hnsw (embedding vector_cosine_ops);

CREATE TABLE extraction_run (
  id                text PRIMARY KEY,
  tenant_id         text NOT NULL,
  project_id        text NOT NULL,
  extractor_version text NOT NULL,
  model             text NOT NULL,
  prompt_version    text NOT NULL,
  policy            jsonb NOT NULL,
  started_at        timestamptz NOT NULL,
  finished_at       timestamptz,
  stats             jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE extraction_result (
  id                text PRIMARY KEY,
  tenant_id         text NOT NULL,
  project_id        text NOT NULL,
  passage_fk        text NOT NULL REFERENCES passage(id),
  text_sha256       text NOT NULL,
  extractor_version text NOT NULL,
  model             text NOT NULL,
  run_id            text NOT NULL,
  status            text NOT NULL CHECK (status IN ('ok', 'error')),
  raw               jsonb,
  accepted          jsonb NOT NULL DEFAULT '[]',
  rejected          jsonb NOT NULL DEFAULT '[]',
  usage             jsonb NOT NULL DEFAULT '{}',
  error             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (passage_fk, text_sha256, extractor_version, model)
);

CREATE TABLE extraction_coverage (
  run_id          text NOT NULL REFERENCES extraction_run(id),
  tenant_id       text NOT NULL,
  project_id      text NOT NULL,
  passage_fk      text NOT NULL REFERENCES passage(id),
  source_id       text NOT NULL,
  relevance_score double precision NOT NULL,
  selected        boolean NOT NULL,
  outcome         text NOT NULL,
  reason          text,
  PRIMARY KEY (run_id, passage_fk)
);

CREATE TABLE graph_build (
  id                text PRIMARY KEY,
  tenant_id         text NOT NULL,
  project_id        text NOT NULL,
  builder_version   text NOT NULL,
  extraction_run_id text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  stats             jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE kg_node (
  id               text PRIMARY KEY,
  tenant_id        text NOT NULL,
  project_id       text NOT NULL,
  type             text NOT NULL,
  label            text NOT NULL,
  aliases          text[] NOT NULL DEFAULT '{}',
  attributes       jsonb NOT NULL DEFAULT '{}',
  source_refs      jsonb NOT NULL DEFAULT '[]',
  evidence_bearing boolean NOT NULL,
  actor            text NOT NULL,
  version          text NOT NULL,
  created_at       timestamptz NOT NULL,
  review_state     text NOT NULL,
  is_current       boolean NOT NULL DEFAULT true,
  graph_build_id   text NOT NULL
);
CREATE INDEX kg_node_label_trgm ON kg_node USING gin (label gin_trgm_ops);
CREATE INDEX kg_node_type_idx ON kg_node (tenant_id, project_id, type);

CREATE TABLE kg_edge (
  id               text PRIMARY KEY,
  tenant_id        text NOT NULL,
  project_id       text NOT NULL,
  type             text NOT NULL,
  src              text NOT NULL REFERENCES kg_node(id),
  dst              text NOT NULL REFERENCES kg_node(id),
  status           text NOT NULL,
  attributes       jsonb NOT NULL DEFAULT '{}',
  source_refs      jsonb NOT NULL DEFAULT '[]',
  evidence_bearing boolean NOT NULL,
  actor            text NOT NULL,
  version          text NOT NULL,
  created_at       timestamptz NOT NULL,
  review_state     text NOT NULL,
  is_current       boolean NOT NULL DEFAULT true,
  graph_build_id   text NOT NULL
);
CREATE INDEX kg_edge_src_idx ON kg_edge (src) WHERE is_current;
CREATE INDEX kg_edge_dst_idx ON kg_edge (dst) WHERE is_current;

CREATE TABLE evidence_ref (
  owner_kind text NOT NULL CHECK (owner_kind IN ('node', 'edge')),
  owner_id   text NOT NULL,
  passage_fk text NOT NULL REFERENCES passage(id),
  quote      text NOT NULL,
  tenant_id  text NOT NULL,
  project_id text NOT NULL,
  PRIMARY KEY (owner_kind, owner_id, passage_fk, quote)
);
CREATE INDEX evidence_ref_passage_idx ON evidence_ref (passage_fk);
CREATE INDEX evidence_ref_owner_idx ON evidence_ref (owner_id);

CREATE TABLE run_record (
  id         text PRIMARY KEY,
  tenant_id  text NOT NULL,
  project_id text NOT NULL,
  kind       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  meta       jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE trace (
  id          text PRIMARY KEY,
  tenant_id   text NOT NULL,
  project_id  text NOT NULL,
  run_id      text NOT NULL,
  question_id text NOT NULL,
  mode        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  payload     jsonb NOT NULL
);

CREATE TABLE answer (
  id          text PRIMARY KEY,
  tenant_id   text NOT NULL,
  project_id  text NOT NULL,
  trace_id    text NOT NULL REFERENCES trace(id),
  run_id      text NOT NULL,
  question_id text NOT NULL,
  mode        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  payload     jsonb NOT NULL
);

CREATE TABLE usage_ledger (
  id                text PRIMARY KEY,
  tenant_id         text NOT NULL,
  project_id        text NOT NULL,
  run_id            text,
  purpose           text NOT NULL,
  model             text NOT NULL,
  input_tokens      integer NOT NULL DEFAULT 0,
  output_tokens     integer NOT NULL DEFAULT 0,
  cache_read_tokens integer NOT NULL DEFAULT 0,
  cache_write_tokens integer NOT NULL DEFAULT 0,
  estimated_usd     double precision NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- Global spend counter for the budget guard. Holds numbers only, never evidence or scope content.
CREATE TABLE model_spend (
  id            text PRIMARY KEY,
  purpose       text NOT NULL,
  model         text NOT NULL,
  estimated_usd double precision NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
"""


def upgrade() -> None:
    op.execute(SCHEMA)

    password = os.environ.get("POTENCY_APP_DB_PASSWORD", "potency_app_dev").replace("'", "''")
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS;
          END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")

    for table in SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY scope_isolation ON {table}
              USING (tenant_id = current_setting('app.tenant_id', true)
                     AND project_id = current_setting('app.project_id', true))
              WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
                          AND project_id = current_setting('app.project_id', true))
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON model_spend TO {APP_ROLE}")


def downgrade() -> None:
    for table in reversed(SCOPED_TABLES + ["model_spend"]):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
