from pathlib import Path
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).with_name('0001_initial.sql').read_text(encoding='utf-8')
    op.get_bind().exec_driver_sql(sql)


def downgrade():
    raise RuntimeError('Destructive downgrade is intentionally unavailable; restore a backup instead')
