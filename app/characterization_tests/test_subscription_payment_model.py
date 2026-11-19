"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, `data-model.md § 2`
(FR-036): la tabla `shared.subscription_payments` aplica por sí misma las reglas de integridad que
el resto de la funcionalidad da por hechas.

No son characterization tests — tabla enteramente nueva. Dos bloques:

- comportamiento sobre SQLite en memoria (las suites del proyecto no abren PostgreSQL): defaults,
  `CHECK`, referencia única, y los dos índices únicos PARCIALES — un solo pago `pending` por tenant
  (FR-016) y una transacción de Wompi no puede ligarse a dos pagos (research.md Decisión 3);
- el DDL que PostgreSQL recibiría de verdad: los índices parciales deben llevar su `WHERE`.

    python -m unittest app.characterization_tests.test_subscription_payment_model -v
"""
from __future__ import annotations

import unittest
import uuid
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

from app.characterization_tests import plan_fixtures as fx
from app.core.timezone import utc_now_naive
from app.models.subscription_payment import SubscriptionPayment


def _pago(db, tenant, plan, **kw) -> SubscriptionPayment:
    """Pago mínimo válido; cada prueba cambia solo lo que le interesa."""
    payment_id = kw.pop("id", uuid.uuid4())
    kw.setdefault("gateway_reference", f"SKP-{payment_id.hex}")
    pago = SubscriptionPayment(
        id=payment_id,
        tenant_id=tenant.id,
        plan_id=plan.id,
        amount=kw.pop("amount", Decimal("89900.00")),
        payment_link_url=kw.pop("payment_link_url", "https://checkout.wompi.co/p/?x=1"),
        payment_link_expires_at=kw.pop("payment_link_expires_at", utc_now_naive() + timedelta(hours=24)),
        **kw,
    )
    db.add(pago)
    db.flush()
    return pago


class SubscriptionPaymentModelTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.plan = fx.make_plan(self.db)
        self.tenant = fx.make_tenant(self.db, plan=self.plan)
        self.otro_tenant = fx.make_tenant(self.db, plan=self.plan)
        self.db.commit()

    # ---------------------------------------------------------- defaults

    def test_defaults_estado_pending_pasarela_wompi_ciclo_mensual(self):
        pago = _pago(self.db, self.tenant, self.plan)
        self.db.commit()
        self.db.refresh(pago)

        self.assertEqual(pago.status, "pending")
        self.assertEqual(pago.gateway, "wompi")
        self.assertEqual(pago.ciclo_facturacion, "mensual")
        self.assertIsNone(pago.gateway_transaction_id)
        self.assertIsNone(pago.requested_by_user_id)
        for campo in ("period_start", "period_end", "paid_at"):
            self.assertIsNone(getattr(pago, campo), campo)
        self.assertIsNotNone(pago.created_at)

    def test_el_monto_conserva_los_decimales(self):
        pago = _pago(self.db, self.tenant, self.plan, amount=Decimal("89900.50"))
        self.db.commit()

        guardado = self.db.execute(select(SubscriptionPayment).where(SubscriptionPayment.id == pago.id)).scalar_one()
        self.assertEqual(guardado.amount, Decimal("89900.50"))

    # ---------------------------------------------------------- CHECK

    def test_status_fuera_del_catalogo_se_rechaza(self):
        with self.assertRaises(IntegrityError):
            _pago(self.db, self.tenant, self.plan, status="refunded")

    def test_los_cinco_estados_del_catalogo_se_aceptan(self):
        # `pending` va solo para respetar el índice de un único pendiente por tenant.
        for estado in ("approved", "rejected", "expired", "voided"):
            with self.subTest(estado):
                _pago(self.db, self.tenant, self.plan, status=estado)
        _pago(self.db, self.tenant, self.plan, status="pending")

    def test_monto_no_positivo_se_rechaza(self):
        for monto in (Decimal("0"), Decimal("-1.00")):
            with self.subTest(monto=monto):
                with self.assertRaises(IntegrityError):
                    _pago(self.db, self.tenant, self.plan, amount=monto)
                self.db.rollback()

    # ---------------------------------------------------------- unicidad

    def test_la_referencia_de_pasarela_es_unica(self):
        _pago(self.db, self.tenant, self.plan, gateway_reference="SKP-IGUAL", status="approved")

        with self.assertRaises(IntegrityError):
            _pago(self.db, self.otro_tenant, self.plan, gateway_reference="SKP-IGUAL", status="approved")

    def test_un_solo_pago_pending_por_tenant(self):
        _pago(self.db, self.tenant, self.plan, status="pending")

        with self.assertRaises(IntegrityError):
            _pago(self.db, self.tenant, self.plan, status="pending")

    def test_el_indice_de_pending_es_por_tenant_y_no_global(self):
        _pago(self.db, self.tenant, self.plan, status="pending")

        _pago(self.db, self.otro_tenant, self.plan, status="pending")  # no lanza

    def test_el_indice_de_pending_no_limita_los_demas_estados(self):
        _pago(self.db, self.tenant, self.plan, status="pending")
        for estado in ("approved", "approved", "rejected", "rejected", "expired", "voided"):
            _pago(self.db, self.tenant, self.plan, status=estado)  # historial ilimitado

    def test_un_pending_nuevo_cabe_cuando_el_anterior_ya_no_esta_pending(self):
        anterior = _pago(self.db, self.tenant, self.plan, status="pending")
        anterior.status = "expired"
        self.db.flush()

        _pago(self.db, self.tenant, self.plan, status="pending")  # no lanza

    def test_una_transaccion_de_wompi_no_puede_ligarse_a_dos_pagos(self):
        _pago(self.db, self.tenant, self.plan, status="approved", gateway_transaction_id="1234-1610641025-49201")

        with self.assertRaises(IntegrityError):
            _pago(self.db, self.otro_tenant, self.plan, status="approved", gateway_transaction_id="1234-1610641025-49201")

    def test_varios_pagos_sin_transaccion_no_chocan_entre_si(self):
        for estado in ("approved", "rejected", "expired"):
            _pago(self.db, self.tenant, self.plan, status=estado, gateway_transaction_id=None)  # NULL no es un valor


class SubscriptionPaymentDdlTests(unittest.TestCase):
    """El DDL de PostgreSQL, que es el que corre en producción: sin el `WHERE` los índices dejarían de ser
    parciales y un tenant no podría tener historial."""

    def _ddl_indices(self):
        tabla = SubscriptionPayment.__table__
        return {ix.name: str(CreateIndex(ix).compile(dialect=postgresql.dialect())) for ix in tabla.indexes}

    def test_la_tabla_vive_en_el_esquema_shared(self):
        self.assertEqual(SubscriptionPayment.__table__.schema, "shared")

    def test_indice_unico_parcial_de_pending_por_tenant(self):
        ddl = self._ddl_indices()["ux__subscription_payments__tenant_id__pending"]

        self.assertIn("CREATE UNIQUE INDEX", ddl)
        self.assertIn("(tenant_id)", ddl)
        self.assertIn("WHERE status = 'pending'", ddl)

    def test_indice_unico_parcial_de_transaccion_de_pasarela(self):
        ddl = self._ddl_indices()["ux__subscription_payments__gateway_transaction_id"]

        self.assertIn("CREATE UNIQUE INDEX", ddl)
        self.assertIn("(gateway_transaction_id)", ddl)
        self.assertIn("WHERE gateway_transaction_id IS NOT NULL", ddl)

    def test_indice_del_historial_por_tenant_y_fecha(self):
        ddl = self._ddl_indices()["ix__subscription_payments__tenant_id__created_at"]

        self.assertNotIn("UNIQUE", ddl)
        self.assertIn("(tenant_id, created_at)", ddl)

    def test_create_table_declara_los_check_con_los_nombres_de_la_convencion(self):
        ddl = str(CreateTable(SubscriptionPayment.__table__).compile(dialect=postgresql.dialect()))

        self.assertIn("CONSTRAINT ck__subscription_payments__status CHECK", ddl)
        self.assertIn("CONSTRAINT ck__subscription_payments__amount_positive CHECK (amount > 0)", ddl)
        self.assertIn("CONSTRAINT uq__subscription_payments__gateway_reference UNIQUE (gateway_reference)", ddl)
        self.assertIn("ON DELETE SET NULL", ddl)  # requested_by_user_id: el pago sobrevive al borrado del usuario


if __name__ == "__main__":
    unittest.main()
