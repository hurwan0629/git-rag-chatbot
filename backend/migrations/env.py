from alembic import context
from sqlalchemy import create_engine
from app.config import Settings

if context.is_offline_mode():
    context.configure(url=Settings().database_url, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(Settings().database_url)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
