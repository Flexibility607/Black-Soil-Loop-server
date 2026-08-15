"""建立 B01/B02 五 schema 初始数据库

Revision ID: 20260806_0001
Revises:
Create Date: 2026-08-06
"""

from alembic import op

from app.shared import models  # noqa: F401
from app.shared.database import SCHEMAS, Base

revision = "20260806_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for schema in SCHEMAS:
            # migrations/env.py has already selected the isolated owner role for
            # the target dataset.  CURRENT_USER therefore resolves to
            # blacksoil_owner for the live database and
            # blacksoil_showcase_owner for the fixed showcase database.
            op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}" AUTHORIZATION CURRENT_USER')
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
    if bind.dialect.name == "postgresql":
        for schema in reversed(SCHEMAS):
            op.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
