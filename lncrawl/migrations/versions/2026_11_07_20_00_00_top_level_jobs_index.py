"""Index for listing top-level jobs (the Downloads page)

Revision ID: e9a1b7c3d520
Revises: c4d8e2f6a913
Create Date: 2026-11-07 20:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9a1b7c3d520"
down_revision: Union[str, Sequence[str], None] = "c4d8e2f6a913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Every chapter is a job, so finished jobs pile up by the hundred thousand
    until the scrubber clears them. The Downloads page lists top-level jobs,
    which by is_done alone meant reading all of those: 20.9 ms -> 0.2 ms for its
    finished list (6.4 ms -> under 0.1 ms for the active one) among 200,000
    finished jobs, polled every few seconds while the page is open.
    """
    op.create_index("ix_jobs_top_level", "jobs", ["parent_job_id", "is_done", "updated_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_jobs_top_level", table_name="jobs")
