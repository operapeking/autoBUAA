"""business automation configuration

Revision ID: c37d15a29b08
Revises: a1b2c3d4e5f6
"""

from alembic import op
import sqlalchemy as sa

revision = "c37d15a29b08"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("boya_courses_cache") as batch:
        batch.drop_column("select_failed_attempts")
        batch.drop_column("select_blocked_reason")
    op.drop_table("uploaded_photos")


def downgrade() -> None:
    with op.batch_alter_table("boya_courses_cache") as batch:
        batch.add_column(
            sa.Column(
                "select_failed_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(sa.Column("select_blocked_reason", sa.Text(), nullable=True))
    op.create_table(
        "uploaded_photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
