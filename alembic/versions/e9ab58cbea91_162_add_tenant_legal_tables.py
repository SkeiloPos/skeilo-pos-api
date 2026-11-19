"""spec 162: documentos legales del tenant (tenant.tenant_legal_profiles,
tenant.tenant_legal_documents, tenant.client_consent_acceptances)

Revision ID: e9ab58cbea91
Revises: e27442deb15b
Create Date: 2026-11-18 00:00:01.000000

"""
from typing import Sequence, Union

from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import text

revision: str = 'e9ab58cbea91'
down_revision: Union[str, Sequence[str], None] = 'e27442deb15b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    """Tres tablas nuevas por schema de tenant (data-model.md tablas 4-6), sin
    backfill: ningún tenant existente tiene hoy perfil legal ni documento
    publicado (ver research.md Decisión 7 sobre el impacto de esto en FR-016)."""
    # Ancla: si no existe 'products' el schema no está inicializado (scratch).
    if not _has_table(schema, "products"):
        return

    if not _has_table(schema, "tenant_legal_profiles"):
        op.create_table(
            "tenant_legal_profiles",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("razon_social", sa.String(length=255), nullable=True),
            sa.Column("identificacion_legal", sa.String(length=50), nullable=True),
            sa.Column("direccion_legal", sa.String(length=255), nullable=True),
            sa.Column("ciudad", sa.String(length=100), nullable=True),
            sa.Column("correo_contacto", sa.String(length=255), nullable=True),
            sa.Column("telefono_contacto", sa.String(length=30), nullable=True),
            sa.Column("dias_retencion_datos", sa.Integer(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id", name=op.f("pk__tenant_legal_profiles")),
            schema=schema,
        )

    if not _has_table(schema, "tenant_legal_documents"):
        op.create_table(
            "tenant_legal_documents",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("master_template_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("master_template_version", sa.Integer(), nullable=False),
            sa.Column("contenido_renderizado", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(length=32), nullable=False),
            sa.Column("estado", sa.String(length=12), nullable=False),
            sa.Column("published_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.CheckConstraint(
                "estado IN ('publicado', 'superado')",
                name=op.f("ck__tenant_legal_documents__ck_tenant_legal_documents_estado"),
            ),
            sa.ForeignKeyConstraint(
                ["master_template_id"], ["shared.master_legal_templates.id"],
                name=op.f("fk__tenant_legal_documents__master_template_id__master_legal_templates"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk__tenant_legal_documents")),
            schema=schema,
        )
        op.create_index(
            "idx_tenant_legal_document_vigente",
            "tenant_legal_documents", ["estado"],
            unique=True, schema=schema,
            postgresql_where=sa.text("estado = 'publicado'"),
        )

    if not _has_table(schema, "client_consent_acceptances"):
        op.create_table(
            "client_consent_acceptances",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("session_participant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_legal_document_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("ip_hash", sa.String(length=64), nullable=False),
            sa.Column("accepted_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(
                ["session_participant_id"], [f"{schema}.session_participants.id"],
                name=op.f("fk__client_consent_acceptances__session_participant_id__session_participants"),
            ),
            sa.ForeignKeyConstraint(
                ["tenant_legal_document_id"], [f"{schema}.tenant_legal_documents.id"],
                name=op.f("fk__client_consent_acceptances__tenant_legal_document_id__tenant_legal_documents"),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk__client_consent_acceptances")),
            schema=schema,
        )
        op.create_index(
            "idx_client_consent_acceptance_participant",
            "client_consent_acceptances", ["session_participant_id"], schema=schema,
        )
        op.create_index(
            "idx_client_consent_acceptance_document",
            "client_consent_acceptances", ["tenant_legal_document_id"], schema=schema,
        )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "products"):
        return
    if _has_table(schema, "client_consent_acceptances"):
        op.drop_table("client_consent_acceptances", schema=schema)
    if _has_table(schema, "tenant_legal_documents"):
        op.drop_table("tenant_legal_documents", schema=schema)
    if _has_table(schema, "tenant_legal_profiles"):
        op.drop_table("tenant_legal_profiles", schema=schema)
