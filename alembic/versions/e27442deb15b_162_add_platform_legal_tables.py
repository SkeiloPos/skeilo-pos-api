"""spec 162: documentos legales de plataforma (shared.platform_legal_documents,
shared.master_legal_templates, shared.tenant_admin_platform_acceptances)

Revision ID: e27442deb15b
Revises: 8d09a031cba3
Create Date: 2026-11-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import text

revision: str = 'e27442deb15b'
down_revision: Union[str, Sequence[str], None] = '8d09a031cba3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


def upgrade() -> None:
    """Tres tablas nuevas en `shared` (data-model.md tablas 1-3), sin backfill:
    nacen vacías para todo tenant/documento existente (ninguno tiene hoy un
    documento de plataforma ni una plantilla maestra)."""
    if not _has_table("shared", "platform_legal_documents"):
        op.create_table(
            "platform_legal_documents",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tipo", sa.String(length=20), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("contenido_markdown", sa.Text(), nullable=False),
            sa.Column("estado", sa.String(length=12), nullable=False, server_default="borrador"),
            sa.Column("content_hash", sa.String(length=32), nullable=False),
            sa.Column("fecha_vigencia", sa.DateTime(), nullable=True),
            sa.Column("published_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.CheckConstraint(
                "tipo IN ('privacidad', 'terminos')",
                name=op.f("ck__platform_legal_documents__ck_platform_legal_documents_tipo"),
            ),
            sa.CheckConstraint(
                "estado IN ('borrador', 'publicado', 'superado')",
                name=op.f("ck__platform_legal_documents__ck_platform_legal_documents_estado"),
            ),
            sa.ForeignKeyConstraint(
                ["published_by_user_id"], ["shared.users.id"],
                name=op.f("fk__platform_legal_documents__published_by_user_id__users"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk__platform_legal_documents")),
            schema="shared",
        )
        op.create_index(
            "idx_platform_legal_document_vigente_por_tipo",
            "platform_legal_documents", ["tipo"],
            unique=True, schema="shared",
            postgresql_where=sa.text("estado = 'publicado'"),
        )
        op.create_index(
            "idx_platform_legal_document_tipo_version",
            "platform_legal_documents", ["tipo", "version"],
            schema="shared",
        )

    if not _has_table("shared", "master_legal_templates"):
        op.create_table(
            "master_legal_templates",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("contenido_markdown", sa.Text(), nullable=False),
            sa.Column("estado", sa.String(length=12), nullable=False, server_default="borrador"),
            sa.Column("content_hash", sa.String(length=32), nullable=False),
            sa.Column("fecha_vigencia", sa.DateTime(), nullable=True),
            sa.Column("published_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.CheckConstraint(
                "estado IN ('borrador', 'publicado', 'superado')",
                name=op.f("ck__master_legal_templates__ck_master_legal_templates_estado"),
            ),
            sa.ForeignKeyConstraint(
                ["published_by_user_id"], ["shared.users.id"],
                name=op.f("fk__master_legal_templates__published_by_user_id__users"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk__master_legal_templates")),
            schema="shared",
        )
        op.create_index(
            "idx_master_legal_template_vigente",
            "master_legal_templates", ["estado"],
            unique=True, schema="shared",
            postgresql_where=sa.text("estado = 'publicado'"),
        )

    if not _has_table("shared", "tenant_admin_platform_acceptances"):
        op.create_table(
            "tenant_admin_platform_acceptances",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", sa.Integer(), nullable=False),
            sa.Column("platform_document_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(
                ["user_id"], ["shared.users.id"],
                name=op.f("fk__tenant_admin_platform_acceptances__user_id__users"),
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["shared.tenants.id"],
                name=op.f("fk__tenant_admin_platform_acceptances__tenant_id__tenants"),
            ),
            sa.ForeignKeyConstraint(
                ["platform_document_id"], ["shared.platform_legal_documents.id"],
                name=op.f(
                    "fk__tenant_admin_platform_acceptances__platform_document_id__platform_legal_documents"
                ),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk__tenant_admin_platform_acceptances")),
            sa.UniqueConstraint(
                "user_id", "platform_document_id",
                name="uq_tenant_admin_platform_acceptance_user_document",
            ),
            schema="shared",
        )
        op.create_index(
            op.f("ix__tenant_admin_platform_acceptances__tenant_id"),
            "tenant_admin_platform_acceptances", ["tenant_id"], schema="shared",
        )


def downgrade() -> None:
    """Downgrade schema."""
    if _has_table("shared", "tenant_admin_platform_acceptances"):
        op.drop_table("tenant_admin_platform_acceptances", schema="shared")
    if _has_table("shared", "master_legal_templates"):
        op.drop_table("master_legal_templates", schema="shared")
    if _has_table("shared", "platform_legal_documents"):
        op.drop_table("platform_legal_documents", schema="shared")
