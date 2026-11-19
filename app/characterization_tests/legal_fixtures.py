"""Infraestructura compartida para los tests nuevos de spec 162 (gestión de
documentos legales) — no es código de producción. Amplía el patrón ya
establecido por `cart_fixtures.py` (SQLite en memoria + `shared` adjunta como
segunda base en memoria, `schema_translate_map={"tenant": None}`) con las
tablas nuevas de esta spec: `platform_legal_documents`/`master_legal_templates`
(schema `shared`) y `tenant_legal_documents`/`client_consent_acceptances`
(schema `tenant`).

Cubre exactamente los tres invariantes que `plan.md` (Principio X,
"Verificación obligatoria") exige probar: la máquina de estados
borrador/publicado/superado (T041), la validación de variables de la
plantilla (T042), y la atomicidad/gate de FR-016/FR-018 en
`cart/service.py::open_session` (T043/T044).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.characterization_tests import cart_fixtures
from app.characterization_tests.cart_fixtures import (  # noqa: F401 - reexport
    make_dining_table,
    make_table_session,
    make_participant,
)
from app.core.models import Base, MasterLegalTemplate, PlatformLegalDocument, Tenant
from app.models.client_consent_acceptance import ClientConsentAcceptance
from app.models.plan import Plan
from app.models.tenant_legal_document import TenantLegalDocument

_EXTRA_TENANT_TABLE_NAMES = ["tenant_legal_documents", "client_consent_acceptances"]
_SHARED_LEGAL_TABLE_NAMES = ["platform_legal_documents", "master_legal_templates"]

_TABLE_NAMES = cart_fixtures._TABLE_NAMES + _EXTRA_TENANT_TABLE_NAMES


def new_session() -> Session:
    """Sesión SQLAlchemy real sobre SQLite en memoria: mismo esquema que
    `cart_fixtures.new_session()` (mesas/sesión/carrito, schema `tenant` ->
    default) más `tenant_legal_documents`/`client_consent_acceptances`, y con
    `platform_legal_documents`/`master_legal_templates` creadas en la base
    `shared` adjunta (mismo truco que `attach_shared_tenants`)."""
    cart_fixtures._remove_partial_unique_indexes()
    cart_fixtures._patch_sqlite_incompatible_server_defaults()
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None})
    conn.connection.dbapi_connection.create_aggregate(
        "bool_or", 1, cart_fixtures._BoolOr
    )
    Base.metadata.create_all(bind=conn, tables=tables)

    conn.execute(text("ATTACH DATABASE ':memory:' AS shared"))
    Tenant.__table__.create(bind=conn)
    Plan.__table__.create(bind=conn)
    PlatformLegalDocument.__table__.create(bind=conn)
    MasterLegalTemplate.__table__.create(bind=conn)

    conn.commit()
    return Session(bind=conn)


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def make_master_template(db: Session, **kw) -> MasterLegalTemplate:
    kw.setdefault("id", _uid())
    kw.setdefault("version", 1)
    kw.setdefault("contenido_markdown", "Hola {{nombre_negocio}}")
    kw.setdefault("estado", "publicado")
    kw.setdefault("content_hash", "x" * 32)
    kw.setdefault("published_at", datetime.now(timezone.utc))
    obj = MasterLegalTemplate(**kw)
    db.add(obj)
    db.flush()
    return obj


def make_tenant_legal_document(db: Session, *, master_template_id=None, **kw) -> TenantLegalDocument:
    kw.setdefault("id", _uid())
    kw.setdefault("version", 1)
    kw["master_template_id"] = master_template_id or _uid()
    kw.setdefault("master_template_version", 1)
    kw.setdefault("contenido_renderizado", "contenido de prueba")
    kw.setdefault("content_hash", "y" * 32)
    kw.setdefault("estado", "publicado")
    obj = TenantLegalDocument(**kw)
    db.add(obj)
    db.flush()
    return obj
