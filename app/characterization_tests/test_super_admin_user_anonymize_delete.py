"""Tests de la nueva funcionalidad — spec 159, User Story 5: anonimizar o
eliminar permanentemente a un usuario (FR-023 a FR-025, FR-027, research.md
Decisión 7/8).

No son characterization tests: anonimizar/eliminar son comportamiento nuevo
de esta spec. Invoca las funciones de endpoint directamente (mismo patrón
que `test_super_admin_user_actions.py`).

`user_retention_service.find_retention_tenants` abre su propia sesión por
tenant vía `app.core.db.with_db` (mismo patrón que
`test_auth_login_platform_isolation.py`) -- se parchea
`app.services.user_retention_service.with_db` para devolver, sin importar el
schema pedido, una sesión SQLite que SÍ registra `invoices`/`customer_orders`
(fuera de `plan_fixtures._TABLE_NAMES`, que no las necesita para el resto del
paquete).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_super_admin_user_anonymize_delete -v
"""
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import DefaultClause

from app.characterization_tests import plan_fixtures as fx
from app.characterization_tests import super_admin_http_fixtures as sa_fx
from app.api.v1.super_admin import users_router
from app.api.v1.super_admin.schemas import ConfirmEmailRequest
from app.core.models import Base
from app.models.admin_audit_log import AdminAuditLog
from app.models.invoice import Invoice
from app.models.tenant_membership import TenantMembership


class _FakeRequest:
    def __init__(self, ip: str | None = "10.0.0.1"):
        self.client = SimpleNamespace(host=ip) if ip is not None else None


_RETENTION_TABLE_NAMES = fx._TABLE_NAMES + ["invoices", "customer_orders"]

# Mismo parche que `cart_fixtures.py`/`orders_fixtures.py`: `applied_promotions`
# usa `server_default=text("'[]'::jsonb")`, que SQLite no entiende (el cast
# `::jsonb` dentro de un DEFAULT de columna). Solo afecta el metadata en
# memoria de este módulo de test.
_SQLITE_INCOMPATIBLE_DEFAULTS = {"invoices": ["applied_promotions"], "customer_orders": ["applied_promotions"]}


def _new_retention_capable_session() -> Session:
    """Como `fx.new_session()`, pero también crea `invoices`/`customer_orders`
    (necesarias para `find_retention_tenants`, que esta suite SÍ ejercita)."""
    for table in Base.metadata.tables.values():
        for col_name in _SQLITE_INCOMPATIBLE_DEFAULTS.get(table.name, ()):
            col = table.c.get(col_name)
            if col is not None:
                col.server_default = DefaultClause(text("'[]'"))

    tables = [t for t in Base.metadata.tables.values() if t.name in _RETENTION_TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(
        schema_translate_map={"tenant": None, "shared": None}
    )
    Base.metadata.create_all(bind=conn, tables=tables)
    conn.commit()
    return Session(bind=conn)


@contextmanager
def _same_db_with_db(db: Session, _schema):
    """Sustituye a `app.core.db.with_db`: en estos tests, todos los tenants
    viven en la misma sesión SQLite (no hay separación real de esquemas),
    así que cualquier `tenant_schema` pedido devuelve la misma `db`."""
    yield db


class TestAnonymizeUser(unittest.TestCase):
    def test_requiere_confirm_email_exacto(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, name="Ana", email="ana@example.invalid", status="active")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            users_router.anonymize_user(
                user.id,
                ConfirmEmailRequest(confirm_email="otro@example.invalid"),
                _FakeRequest(),
                db=db,
                actor=actor,
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_anonimiza_de_forma_irreversible_y_registra_auditoria(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        sa_fx.make_super_admin(db)  # un segundo Super Admin activo (Decisión 8)
        user = fx.make_user(
            db, t, name="Ana", email="ana@example.invalid", status="active"
        )
        original_password_hash = user.password_hash
        db.commit()

        result = users_router.anonymize_user(
            user.id,
            ConfirmEmailRequest(confirm_email="ana@example.invalid"),
            _FakeRequest(),
            db=db,
            actor=actor,
        )

        self.assertEqual(result.status, "anonymized")
        self.assertNotEqual(user.email, "ana@example.invalid")
        self.assertTrue(user.email.endswith("@anonimizado.invalid"))
        self.assertNotEqual(user.name, "Ana")
        self.assertNotEqual(user.password_hash, original_password_hash)
        self.assertFalse(user.active)
        self.assertIsNotNone(user.anonymized_at)

        log = db.query(AdminAuditLog).filter_by(affected_user_id=user.id).one()
        self.assertEqual(log.action, "anonymized")
        # La bitácora conserva el correo ORIGINAL, no el anonimizado (FR-038).
        self.assertEqual(log.affected_user_email_snapshot, "ana@example.invalid")

    def test_bloquea_si_ya_esta_anonimizado(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, status="anonymized", active=False)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            users_router.anonymize_user(
                user.id,
                ConfirmEmailRequest(confirm_email=user.email),
                _FakeRequest(),
                db=db,
                actor=actor,
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_bloquea_si_es_el_ultimo_super_admin_activo(self):
        db = fx.new_session()
        actor = sa_fx.make_super_admin(db)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            users_router.anonymize_user(
                actor.id,
                ConfirmEmailRequest(confirm_email=actor.email),
                _FakeRequest(),
                db=db,
                actor=actor,
            )
        self.assertEqual(ctx.exception.status_code, 409)


class TestDeleteUser(unittest.TestCase):
    def test_requiere_confirm_email_exacto(self):
        db = fx.new_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, email="ana@example.invalid")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            users_router.delete_user(
                user.id,
                ConfirmEmailRequest(confirm_email="mal@example.invalid"),
                _FakeRequest(),
                db=db,
                actor=actor,
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_bloquea_por_retencion_y_ofrece_anonimizar(self):
        db = _new_retention_capable_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, email="ana@example.invalid")
        fx.make_tenant_membership(db, user, t)
        db.add(Invoice(
            sale_id=uuid4(), prefix="A", number=1, user_id=user.id, status="issued",
        ))
        db.commit()

        with patch(
            "app.services.user_retention_service.with_db",
            lambda schema: _same_db_with_db(db, schema),
        ):
            with self.assertRaises(HTTPException) as ctx:
                users_router.delete_user(
                    user.id,
                    ConfirmEmailRequest(confirm_email="ana@example.invalid"),
                    _FakeRequest(),
                    db=db,
                    actor=actor,
                )
        self.assertEqual(ctx.exception.status_code, 409)
        body = ctx.exception.detail
        self.assertEqual(body["retention_tenants"][0]["tenant_id"], t.id)
        self.assertIn("anonimiz", body["reason"])

        # No tocó la factura existente (Principio VII).
        self.assertEqual(db.query(Invoice).filter_by(user_id=user.id).count(), 1)

    def test_elimina_sin_obligacion_de_retencion_y_la_auditoria_sobrevive(self):
        db = _new_retention_capable_session()
        t = fx.make_tenant(db, name="Pizzeria")
        actor = sa_fx.make_super_admin(db)
        user = fx.make_user(db, t, email="ana@example.invalid")
        fx.make_tenant_membership(db, user, t)
        user_id = user.id
        db.commit()

        with patch(
            "app.services.user_retention_service.with_db",
            lambda schema: _same_db_with_db(db, schema),
        ):
            result = users_router.delete_user(
                user_id,
                ConfirmEmailRequest(confirm_email="ana@example.invalid"),
                _FakeRequest(),
                db=db,
                actor=actor,
            )

        self.assertIn("message", result)
        self.assertIsNone(db.get(type(user), user_id))
        self.assertEqual(
            db.query(TenantMembership).filter_by(user_id=user_id).count(), 0
        )

        log = db.query(AdminAuditLog).filter_by(affected_user_id=user_id).one()
        self.assertEqual(log.action, "deleted")
        self.assertEqual(log.affected_user_email_snapshot, "ana@example.invalid")

    def test_bloquea_si_es_el_ultimo_super_admin_activo(self):
        db = _new_retention_capable_session()
        actor = sa_fx.make_super_admin(db)
        db.commit()

        with patch(
            "app.services.user_retention_service.with_db",
            lambda schema: _same_db_with_db(db, schema),
        ):
            with self.assertRaises(HTTPException) as ctx:
                users_router.delete_user(
                    actor.id,
                    ConfirmEmailRequest(confirm_email=actor.email),
                    _FakeRequest(),
                    db=db,
                    actor=actor,
                )
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
