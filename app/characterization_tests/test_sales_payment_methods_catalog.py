"""Tests de la nueva funcionalidad — spec 032-catalogo-metodos-pago, Historia de
Usuario 2: el Tenant Admin activa y configura métodos de pago para su negocio
(FR-005/FR-006/FR-007/FR-008/FR-009/FR-010/FR-011/FR-017).

No son characterization tests: activar un método vía `catalog_id` es
comportamiento enteramente nuevo (Constitución, Principio IV/X).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_sales_payment_methods_catalog -v
"""
import unittest

from fastapi import HTTPException

from app.characterization_tests import payment_catalog_fixtures as fx
from app.api.v1.sales import service
from app.api.v1.sales.schemas import PaymentMethodCreate, PaymentMethodUpdate
from app.models.payment import PaymentMethod


class TestSalesPaymentMethodsCatalog(unittest.TestCase):
    # ---------------------------------------------------------- Acceptance Scenario 1

    def test_catalogo_para_tenant_solo_muestra_activos_y_ya_activados(self):
        """FR-005/FR-006: el tenant ve el catálogo activo, más lo que ya
        activó aunque el Super Admin lo haya desactivado después."""
        db = fx.new_session()
        nequi = fx.make_payment_method_catalog(db, name="Nequi", type="transfer")
        daviplata = fx.make_payment_method_catalog(db, name="Daviplata", type="transfer", active=False)
        bancolombia = fx.make_payment_method_catalog(db, name="Bancolombia", type="transfer", active=False)
        fx.make_payment_method(db, catalog_id=bancolombia.id, name="Bancolombia", type="transfer", is_cash=False)
        db.commit()

        options = service.list_catalog_for_tenant(db)
        names = {o.name: o for o in options}

        self.assertIn("Nequi", names)
        self.assertFalse(names["Nequi"].already_activated)
        self.assertNotIn("Daviplata", names)  # inactivo y nunca activado: no aparece
        self.assertIn("Bancolombia", names)  # inactivo, pero ya activado: aparece marcado
        self.assertFalse(names["Bancolombia"].active)
        self.assertTrue(names["Bancolombia"].already_activated)

    # ---------------------------------------------------------- Acceptance Scenario 2

    def test_activar_con_campos_completos_queda_configurado(self):
        """FR-007/FR-008/FR-009: activar Nequi con el celular completo deja
        `is_complete=True` de inmediato."""
        db = fx.new_session()
        catalog = fx.make_payment_method_catalog(
            db, name="Nequi", type="transfer",
            fields=[{"key": "celular", "label": "Celular", "required": True, "format": "numeric", "length": 10}],
        )
        db.commit()

        method = service.create_payment_method(
            db, PaymentMethodCreate(catalog_id=catalog.id, payment_info={"celular": "3001234567"}),
        )
        self.assertTrue(method.active)
        self.assertTrue(method.is_complete)
        self.assertEqual(method.name, "Nequi")
        self.assertEqual(method.type, "transfer")

    # ---------------------------------------------------------- Acceptance Scenario 3

    def test_activar_dejando_campo_obligatorio_vacio_queda_incompleto(self):
        db = fx.new_session()
        catalog = fx.make_payment_method_catalog(
            db, name="Nequi", type="transfer",
            fields=[{"key": "celular", "label": "Celular", "required": True, "format": "numeric", "length": 10}],
        )
        db.commit()

        method = service.create_payment_method(
            db, PaymentMethodCreate(catalog_id=catalog.id, payment_info=None),
        )
        self.assertTrue(method.active)
        self.assertFalse(method.is_complete)

    def test_payment_info_con_formato_invalido_es_422(self):
        """FR-009/clarificación 2026-08-24 #3: un celular que no cumple el
        formato numérico de 10 dígitos es rechazado, no solo "incompleto"."""
        db = fx.new_session()
        catalog = fx.make_payment_method_catalog(
            db, name="Nequi", type="transfer",
            fields=[{"key": "celular", "label": "Celular", "required": True, "format": "numeric", "length": 10}],
        )
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_method(
                db, PaymentMethodCreate(catalog_id=catalog.id, payment_info={"celular": "abc"}),
            )
        self.assertEqual(ctx.exception.status_code, 422)

    # ---------------------------------------------------------- Acceptance Scenario 4

    def test_activar_metodo_sin_campos_queda_disponible_de_inmediato(self):
        """FR-004/FR-009: Efectivo, sin campos, no pide nada — completo desde
        el primer momento."""
        db = fx.new_session()
        efectivo = fx.make_payment_method_catalog(db, name="Efectivo", type="cash", fields=[])
        db.commit()

        method = service.create_payment_method(db, PaymentMethodCreate(catalog_id=efectivo.id))
        self.assertTrue(method.is_complete)
        self.assertTrue(method.is_cash)

    # ---------------------------------------------------------- Acceptance Scenario 5

    def test_desactivar_metodo_no_borra_ni_afecta_configuracion(self):
        db = fx.new_session()
        catalog = fx.make_payment_method_catalog(db, name="Nequi", type="transfer")
        method = fx.make_payment_method(
            db, catalog_id=catalog.id, name="Nequi", type="transfer", is_cash=False,
            payment_info={"celular": "3001234567"},
        )
        fx.make_payment_method(db, name="Efectivo", is_cash=True)  # queda al menos uno activo
        db.commit()

        updated = service.update_payment_method(db, method.id, PaymentMethodUpdate(active=False))
        self.assertFalse(updated.active)
        self.assertIsNotNone(db.get(PaymentMethod, method.id))
        self.assertEqual(updated.payment_info, {"celular": "3001234567"})

    # ---------------------------------------------------------- FR-017

    def test_activar_el_mismo_catalogo_dos_veces_es_409(self):
        db = fx.new_session()
        catalog = fx.make_payment_method_catalog(db, name="Nequi", type="transfer", fields=[])
        service.create_payment_method(db, PaymentMethodCreate(catalog_id=catalog.id))

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_method(db, PaymentMethodCreate(catalog_id=catalog.id))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_reactivar_via_patch_conserva_payment_info_ya_capturado(self):
        """FR-017 (research.md Decisión 9): reactivar es PATCH sobre la misma
        fila — si no se manda `payment_info` nuevo, conserva el anterior."""
        db = fx.new_session()
        catalog = fx.make_payment_method_catalog(
            db, name="Nequi", type="transfer",
            fields=[{"key": "celular", "label": "Celular", "required": True, "format": "numeric", "length": 10}],
        )
        method = service.create_payment_method(
            db, PaymentMethodCreate(catalog_id=catalog.id, payment_info={"celular": "3001234567"}),
        )
        fx.make_payment_method(db, name="Efectivo", is_cash=True)  # queda al menos uno activo
        db.commit()
        service.update_payment_method(db, method.id, PaymentMethodUpdate(active=False))

        # Reactivar creando una fila nueva: 409 (research.md Decisión 9).
        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_method(db, PaymentMethodCreate(catalog_id=catalog.id))
        self.assertEqual(ctx.exception.status_code, 409)

        # El camino correcto: PATCH sobre la fila existente.
        reactivated = service.update_payment_method(db, method.id, PaymentMethodUpdate(active=True))
        self.assertTrue(reactivated.active)
        self.assertEqual(reactivated.payment_info, {"celular": "3001234567"})
        self.assertTrue(reactivated.is_complete)

    def test_activar_metodo_desactivado_en_catalogo_es_409(self):
        db = fx.new_session()
        catalog = fx.make_payment_method_catalog(db, name="Daviplata", type="transfer", active=False)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_method(db, PaymentMethodCreate(catalog_id=catalog.id))
        self.assertEqual(ctx.exception.status_code, 409)

    # ---------------------------------------------------------- FR-011

    def test_activar_catalog_id_inexistente_es_404(self):
        """FR-011: un tenant no puede crear métodos fuera del catálogo — el
        único punto de entrada es un `catalog_id` real."""
        db = fx.new_session()
        import uuid

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_method(db, PaymentMethodCreate(catalog_id=uuid.uuid4()))
        self.assertEqual(ctx.exception.status_code, 404)


class TestActivarPagoMixto130(unittest.TestCase):
    """Spec 130, FR-004: "Pago mixto" solo se activa si el tenant ya tiene
    efectivo y transferencia activos. No es characterization test: 'mixed' es
    un tipo de catálogo enteramente nuevo (Constitución, Principio IV/X)."""

    def test_sin_efectivo_ni_transferencia_activos_rechaza_409(self):
        db = fx.new_session()
        mixto = fx.make_payment_method_catalog(db, name="Pago mixto", type="mixed")
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_method(db, PaymentMethodCreate(catalog_id=mixto.id))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("efectivo", ctx.exception.detail)
        self.assertIn("transferencia", ctx.exception.detail)

    def test_con_solo_transferencia_activa_sigue_rechazando_409(self):
        db = fx.new_session()
        mixto = fx.make_payment_method_catalog(db, name="Pago mixto", type="mixed")
        fx.make_payment_method(db, name="Nequi", type="transfer", is_cash=False)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_method(db, PaymentMethodCreate(catalog_id=mixto.id))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_con_solo_efectivo_activo_sigue_rechazando_409(self):
        db = fx.new_session()
        mixto = fx.make_payment_method_catalog(db, name="Pago mixto", type="mixed")
        fx.make_payment_method(db, name="Efectivo", type="cash", is_cash=True)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_method(db, PaymentMethodCreate(catalog_id=mixto.id))
        self.assertEqual(ctx.exception.status_code, 409)

    def test_con_efectivo_y_transferencia_activos_se_acepta(self):
        db = fx.new_session()
        mixto = fx.make_payment_method_catalog(db, name="Pago mixto", type="mixed")
        fx.make_payment_method(db, name="Efectivo", type="cash", is_cash=True)
        fx.make_payment_method(db, name="Nequi", type="transfer", is_cash=False)
        db.commit()

        method = service.create_payment_method(db, PaymentMethodCreate(catalog_id=mixto.id))
        self.assertTrue(method.active)
        self.assertEqual(method.type, "mixed")

    def test_efectivo_o_transferencia_inactivos_no_cuentan(self):
        """La validación mira métodos ACTIVOS, no solo existentes."""
        db = fx.new_session()
        mixto = fx.make_payment_method_catalog(db, name="Pago mixto", type="mixed")
        fx.make_payment_method(db, name="Efectivo", type="cash", is_cash=True, active=False)
        fx.make_payment_method(db, name="Nequi", type="transfer", is_cash=False)
        db.commit()

        with self.assertRaises(HTTPException) as ctx:
            service.create_payment_method(db, PaymentMethodCreate(catalog_id=mixto.id))
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
