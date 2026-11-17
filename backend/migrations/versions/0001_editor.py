"""Campaign editor storage."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_editor"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("campaigns",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("alias", sa.String(64), nullable=False, unique=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("data", JSONB(), nullable=False))
    op.create_table("offers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(1024), nullable=False))
    op.create_table("streams",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("campaign_id", sa.BigInteger(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("name", sa.String(1024), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("schema", sa.String(32), nullable=False),
        sa.Column("missing", sa.Boolean(), nullable=False),
        sa.Column("confirmed", JSONB(), nullable=False),
        sa.Column("draft", JSONB(), nullable=True),
        sa.Column("conflict", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("publication", sa.String(32), nullable=False),
        sa.Column("pending", JSONB(), nullable=True),
        sa.Column("publishing_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_streams_campaign_id", "streams", ["campaign_id"])
    op.create_table("stream_offers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("stream_id", sa.BigInteger(), sa.ForeignKey("streams.id"), nullable=False),
        sa.Column("offer_id", sa.BigInteger(), sa.ForeignKey("offers.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("share", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("removed", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("stream_id", "offer_id", name="uq_stream_offer"))
    op.create_index("ix_stream_offers_stream_id", "stream_offers", ["stream_id"])


def downgrade():
    op.drop_table("stream_offers")
    op.drop_table("streams")
    op.drop_table("offers")
    op.drop_table("campaigns")
