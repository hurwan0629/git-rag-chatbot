CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE repositories (
 repo_id uuid PRIMARY KEY,
 repo text NOT NULL UNIQUE CHECK (repo=lower(repo) AND repo ~ '^[a-z0-9-]+/[a-z0-9_.-]+$'),
 repo_url text NOT NULL CHECK(repo_url='https://github.com/' || repo),
 active_run_id text,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE embedding_profiles (
 profile_id text PRIMARY KEY CHECK(profile_id ~ '^[a-f0-9]{64}$'),
 model_id text NOT NULL,
 revision text NOT NULL CHECK(revision ~ '^[a-f0-9]{40}$'),
 dimension integer NOT NULL CHECK(dimension=384),
 passage_prefix text NOT NULL CHECK(passage_prefix='passage: '),
 query_prefix text NOT NULL CHECK(query_prefix='query: '),
 pooling text NOT NULL CHECK(pooling='masked_mean'),
 normalize boolean NOT NULL CHECK(normalize),
 dtype text NOT NULL CHECK(dtype='float32'),
 max_input_tokens integer NOT NULL CHECK(max_input_tokens=512),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ingestion_runs (
 run_id text PRIMARY KEY CHECK(run_id ~ '^[0-9]{8}T[0-9]{6}Z_[a-f0-9]{32}$'),
 repo_id uuid NOT NULL REFERENCES repositories ON DELETE RESTRICT,
 profile_id text NOT NULL REFERENCES embedding_profiles ON DELETE RESTRICT,
 schema_version text NOT NULL CHECK(schema_version='2.0'),
 mode text NOT NULL CHECK(mode IN ('embed','collect_only')),
 status text NOT NULL CHECK(status IN ('pending','running','completed','collected_only','partial','failed')),
 ref_requested text,
 commit_sha text CHECK(commit_sha ~ '^[a-f0-9]{40}$'),
 tree_sha text CHECK(tree_sha ~ '^[a-f0-9]{40}$'),
 scope_path text NOT NULL DEFAULT '',
 inventory_complete boolean NOT NULL DEFAULT false,
 auth_used boolean NOT NULL DEFAULT false,
 filter_settings jsonb NOT NULL CHECK(jsonb_typeof(filter_settings)='object'),
 chunking_settings jsonb NOT NULL CHECK(jsonb_typeof(chunking_settings)='object'),
 counts jsonb NOT NULL CHECK(jsonb_typeof(counts)='object'),
 timings_sec jsonb NOT NULL CHECK(jsonb_typeof(timings_sec)='object'),
 execution_settings jsonb NOT NULL CHECK(jsonb_typeof(execution_settings)='object'),
 errors jsonb NOT NULL CHECK(jsonb_typeof(errors)='array'),
 libraries jsonb NOT NULL CHECK(jsonb_typeof(libraries)='object'),
 source_manifest jsonb CHECK(jsonb_typeof(source_manifest)='object'),
 created_at timestamptz NOT NULL DEFAULT now(),
 started_at timestamptz,
 finished_at timestamptz,
 UNIQUE(repo_id,run_id), UNIQUE(run_id,profile_id),
 CHECK(status <> 'completed' OR mode='embed'),
 CHECK(status <> 'collected_only' OR mode='collect_only'),
 CHECK(status NOT IN ('completed','collected_only') OR
  (commit_sha IS NOT NULL AND tree_sha IS NOT NULL AND started_at IS NOT NULL
   AND finished_at IS NOT NULL AND inventory_complete)),
 CHECK(started_at IS NULL OR started_at>=created_at),
 CHECK(finished_at IS NULL OR (finished_at>=created_at AND (started_at IS NULL OR finished_at>=started_at))),
 CHECK((status='pending' AND started_at IS NULL AND finished_at IS NULL)
   OR (status='running' AND started_at IS NOT NULL AND finished_at IS NULL)
   OR (status IN ('completed','collected_only','partial','failed') AND finished_at IS NOT NULL))
);
ALTER TABLE repositories ADD CONSTRAINT fk_repo_active_run FOREIGN KEY(repo_id,active_run_id)
 REFERENCES ingestion_runs(repo_id,run_id) ON DELETE RESTRICT;
CREATE TABLE files (
 run_id text NOT NULL REFERENCES ingestion_runs ON DELETE RESTRICT,
 path text NOT NULL CHECK(path<>''),
 entry_type text NOT NULL, mode text NOT NULL,
 git_object_sha text NOT NULL CHECK(git_object_sha ~ '^[a-f0-9]{40}$'),
 size_bytes bigint CHECK(size_bytes>=0),
 status text NOT NULL CHECK(status IN ('collected','excluded','failed')),
 reason_code text, reason text,
 PRIMARY KEY(run_id,path),
 CHECK((status='collected' AND reason_code IS NULL AND reason IS NULL)
   OR (status IN ('excluded','failed') AND reason_code IS NOT NULL AND reason_code<>''))
);
CREATE TABLE documents (
 run_id text NOT NULL, document_id text NOT NULL CHECK(document_id ~ '^[a-f0-9]{64}$'),
 path text NOT NULL, blob_sha text NOT NULL CHECK(blob_sha ~ '^[a-f0-9]{40}$'),
 content_sha256 text NOT NULL CHECK(content_sha256 ~ '^[a-f0-9]{64}$'),
 size_bytes bigint NOT NULL CHECK(size_bytes>=0), encoding text NOT NULL CHECK(encoding='utf-8'),
 text text NOT NULL, line_count integer NOT NULL CHECK(line_count>=0),
 PRIMARY KEY(run_id,document_id), UNIQUE(run_id,path),
 FOREIGN KEY(run_id,path) REFERENCES files(run_id,path) ON DELETE RESTRICT,
 CHECK(octet_length(text)=size_bytes)
);
CREATE TABLE chunks (
 run_id text NOT NULL, chunk_id text NOT NULL CHECK(chunk_id ~ '^[a-f0-9]{64}$'),
 document_id text NOT NULL, chunk_index integer NOT NULL CHECK(chunk_index>=0),
 start_offset integer NOT NULL CHECK(start_offset>=0), end_offset integer NOT NULL,
 start_line integer NOT NULL CHECK(start_line>=1), end_line integer NOT NULL,
 text text NOT NULL, token_count integer NOT NULL CHECK(token_count BETWEEN 1 AND 512),
 embedding_status text NOT NULL CHECK(embedding_status IN ('not_run','embedded','failed')),
 embedding_error text,
 PRIMARY KEY(run_id,chunk_id), UNIQUE(run_id,document_id,chunk_index),
 FOREIGN KEY(run_id,document_id) REFERENCES documents(run_id,document_id) ON DELETE RESTRICT,
 CHECK(end_offset>start_offset AND char_length(text)=end_offset-start_offset),
 CHECK(end_line>=start_line),
 CHECK((embedding_status='failed' AND embedding_error IS NOT NULL)
  OR (embedding_status<>'failed' AND embedding_error IS NULL))
);
CREATE TABLE embeddings (
 run_id text NOT NULL, chunk_id text NOT NULL, profile_id text NOT NULL REFERENCES embedding_profiles ON DELETE RESTRICT,
 embedding vector(384) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,chunk_id,profile_id),
 FOREIGN KEY(run_id,chunk_id) REFERENCES chunks(run_id,chunk_id) ON DELETE RESTRICT,
 FOREIGN KEY(run_id,profile_id) REFERENCES ingestion_runs(run_id,profile_id) ON DELETE RESTRICT
);
CREATE TABLE jobs (
 job_id uuid PRIMARY KEY, run_id text NOT NULL UNIQUE, repo_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind='ingestion'),
 status text NOT NULL CHECK(status IN ('queued','running','completed','failed','interrupted')),
 phase text NOT NULL CHECK(phase IN ('queued','listing','collecting','chunking','embedding','validating','publishing','finished')),
 progress_unit text CHECK(progress_unit IN ('files','chunks','steps')),
 completed_units bigint NOT NULL DEFAULT 0 CHECK(completed_units>=0),
 total_units bigint CHECK(total_units>=0),
 error jsonb CHECK(jsonb_typeof(error)='object'),
 created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz,
 FOREIGN KEY(repo_id,run_id) REFERENCES ingestion_runs(repo_id,run_id) ON DELETE RESTRICT,
 CHECK(total_units IS NULL OR completed_units<=total_units),
 CHECK(progress_unit IS NOT NULL OR (completed_units=0 AND total_units IS NULL)),
 CHECK((status IN ('failed','interrupted') AND error IS NOT NULL)
   OR (status NOT IN ('failed','interrupted') AND error IS NULL)),
 CHECK(started_at IS NULL OR started_at>=created_at),
 CHECK(finished_at IS NULL OR (finished_at>=created_at AND (started_at IS NULL OR finished_at>=started_at))),
 CHECK((status='queued' AND started_at IS NULL AND finished_at IS NULL)
   OR (status='running' AND started_at IS NOT NULL AND finished_at IS NULL)
   OR (status IN ('completed','failed','interrupted') AND finished_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_repo_active_job ON jobs(repo_id) WHERE status IN ('queued','running');
CREATE INDEX ix_files_status_path ON files(run_id,status,path);
CREATE INDEX ix_runs_repo_created ON ingestion_runs(repo_id,created_at);
CREATE INDEX ix_jobs_status_created ON jobs(status,created_at);
