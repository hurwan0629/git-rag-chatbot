"""SQLAlchemy Core tables; the versioned SQL migration is the schema source."""
from sqlalchemy import MetaData
from pgvector.sqlalchemy import Vector  # Registers vector type reflection.

TABLE_NAMES = ('repositories', 'embedding_profiles', 'ingestion_runs', 'files',
               'documents', 'chunks', 'embeddings', 'jobs')


def tables(engine):
    metadata = MetaData()
    metadata.reflect(bind=engine, only=TABLE_NAMES)
    # SQL NULL must stay distinct from the JSON literal null for nullable JSON columns.
    metadata.tables['ingestion_runs'].c.source_manifest.type.none_as_null = True
    metadata.tables['jobs'].c.error.type.none_as_null = True
    return metadata.tables
