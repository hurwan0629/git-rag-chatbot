from sqlalchemy import create_engine, text
from .db_models import tables


class Database:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True)
        with self.engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        self.tables = tables(self.engine)

    def close(self):
        self.engine.dispose()
