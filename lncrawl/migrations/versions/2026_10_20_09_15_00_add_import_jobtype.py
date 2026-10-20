"""Add the IMPORT_ARCHIVE job type

Revision ID: a7c3e9b1d025
Revises: f3b0a1c2d4e5
Create Date: 2026-10-20 09:15:00.000000
"""

from typing import Sequence, Union

from alembic import op

from lncrawl.enums import JobType

# revision identifiers, used by Alembic.
revision: str = "a7c3e9b1d025"
down_revision: Union[str, Sequence[str], None] = "f3b0a1c2d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

try:
    dialect = op.get_context().dialect.name
except Exception:
    dialect = ""


def upgrade() -> None:
    """Upgrade schema.

    `jobs.type` is a native enum on PostgreSQL, so a value the type does not
    list is rejected on insert however well the Python side knows it. SQLite
    stores the column as text without a check constraint, which is why this is
    only needed for one dialect — and why the gap would otherwise go unnoticed
    until an import job was queued against a real PostgreSQL database.
    """
    if dialect == "postgresql":
        names = ", ".join(f"'{m.name}'" for m in JobType)
        op.execute("ALTER TABLE jobs ALTER COLUMN type TYPE varchar USING type::varchar")
        op.execute("DROP TYPE jobtype")
        op.execute(f"CREATE TYPE jobtype AS ENUM ({names})")
        op.execute("ALTER TABLE jobs ALTER COLUMN type TYPE jobtype USING type::jobtype")


def downgrade() -> None:
    """Downgrade schema."""
    upgrade()
