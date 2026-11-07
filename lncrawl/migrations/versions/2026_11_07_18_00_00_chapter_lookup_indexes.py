"""Indexes for looking up a volume's chapters and for per-novel chapter counts

Revision ID: c4d8e2f6a913
Revises: a7c3e9b1d025
Create Date: 2026-11-07 18:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d8e2f6a913"
down_revision: Union[str, Sequence[str], None] = "a7c3e9b1d025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    A volume's chapters were found by reading every chapter in the library: the
    one index with volume_id leads with novel_id. Measured on 120,000 chapters,
    8.4 ms -> 0.1 ms, and the old cost grew with the library. The second index
    covers the per-novel latest-chapter and downloaded-count queries the library
    page makes, so they read it instead of every chapter row (14 -> 7 ms and
    16 -> 5 ms on the same library).
    """
    op.create_index("ix_chapter_volume_serial", "chapters", ["volume_id", "serial"])
    op.create_index(
        "ix_chapter_novel_done_created", "chapters", ["novel_id", "is_done", "created_at"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_chapter_novel_done_created", table_name="chapters")
    op.drop_index("ix_chapter_volume_serial", table_name="chapters")
