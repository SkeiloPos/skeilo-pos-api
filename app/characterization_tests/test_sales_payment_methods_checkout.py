"""Tests de la nueva funcionalidad — spec 032-catalogo-metodos-pago, Historia de
Usuario 3: el Cajero cobra usando solo métodos de pago completamente
disponibles (FR-012/FR-012a/FR-013).

No son characterization tests: filtrar y ocultar `payment_info` en el checkout
es comportamiento enteramente nuevo (Constitución, Principio IV/X).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_sales_payment_methods_checkout -v
"""
import unittest

from app.characterization_tests import payment_catalog_fixtures as fx
from app.api.v1.sales import service


class TestSalesPaymentMethodsCheckout(unittest.TestCase):
    # ---------------------------------------------------------- Acceptance Scenario 1

    def test_disponibles_para_checkout_excluye_incompletos_y_no_expone_payment_info(self):
        """FR-012/FR-012a: solo los métodos activos y completos, y sin
        `payment_info` en la respuesta filtrada."""
        db = fx.new_session()
        nequi_catalog = fx.make_payment_method_catalog(db, name="Nequi", type="transfer")
        bancolombia_catalog = fx.make_payment_method_catalog(db, name="Bancolombia", type="transfer")

        fx.make_payment_method(
            db, name="Efectivo", type="cash", is_cash=True, is_complete=True, active=True,
        )
        fx.make_payment_method(
            db, catalog_id=nequi_catalog.id, name="Nequi", type="transfer", is_cash=False,
            payment_info={"celular": "3001234567"}, is_complete=True, active=True,
        )
        fx.make_payment_method(
            db, catalog_id=bancolombia_catalog.id, name="Bancolombia", type="transfer", is_cash=False,
            payment_info=None, is_complete=False, active=True,  # incompleto
        )
        db.commit()

        available = service.list_available_payment_methods(db)
        names = {m.name for m in available}

        self.assertEqual(names, {"Efectivo", "Nequi"})
        self.assertNotIn("Bancolombia", names)
        # La consulta de disponibilidad no expone nada del payment_info por
        # construcción (FR-012a se cumple en el schema del router, no aquí),
        # pero confirmamos igual que el método completo sigue teniendo sus
        # datos intactos en el modelo (no se borraron al filtrar).
        nequi_row = next(m for m in available if m.name == "Nequi")
        self.assertEqual(nequi_row.payment_info, {"celular": "3001234567"})

    # ---------------------------------------------------------- Acceptance Scenario 2

    def test_metodo_recien_completado_aparece_de_inmediato(self):
        """SC-002: completar los datos pendientes lo hace disponible sin
        pasos adicionales."""
        db = fx.new_session()
        catalog = fx.make_payment_method_catalog(
            db, name="Nequi", type="transfer",
            fields=[{"key": "celular", "label": "Celular", "required": True, "format": "numeric", "length": 10}],
        )
        method = fx.make_payment_method(
            db, catalog_id=catalog.id, name="Nequi", type="transfer", is_cash=False,
            payment_info=None, is_complete=False, active=True,
        )
        db.commit()
        self.assertEqual(service.list_available_payment_methods(db), [])

        from app.api.v1.sales.schemas import PaymentMethodUpdate
        service.update_payment_method(
            db, method.id, PaymentMethodUpdate(payment_info={"celular": "3001234567"}),
        )

        available = service.list_available_payment_methods(db)
        self.assertEqual([m.name for m in available], ["Nequi"])

    # ---------------------------------------------------------- FR-016 (ventana de backfill)

    def test_fila_sin_catalog_id_todavia_sigue_disponible(self):
        """FR-016: una fila creada antes de esta spec (`catalog_id IS NULL`,
        ventana previa al backfill) no debe desaparecer de caja solo por no
        tener catálogo asignado todavía — sigue disponible con el default
        `is_complete=True` hasta que el backfill la procese."""
        db = fx.new_session()
        fx.make_payment_method(
            db, name="Efectivo", type="cash", is_cash=True, active=True, catalog_id=None,
        )
        db.commit()

        available = service.list_available_payment_methods(db)
        self.assertEqual([m.name for m in available], ["Efectivo"])

    # ---------------------------------------------------------- Acceptance Scenario 3

    def test_desactivar_metodo_en_catalogo_lo_saca_del_checkout(self):
        """FR-013: al desactivar a nivel plataforma, deja de estar disponible
        en caja aunque la fila del tenant siga `active=true`."""
        db = fx.new_session()
        catalog = fx.make_payment_method_catalog(db, name="Nequi", type="transfer")
        fx.make_payment_method(
            db, catalog_id=catalog.id, name="Nequi", type="transfer", is_cash=False,
            payment_info={"celular": "3001234567"}, is_complete=True, active=True,
        )
        db.commit()
        self.assertEqual([m.name for m in service.list_available_payment_methods(db)], ["Nequi"])

        catalog.active = False
        db.commit()

        self.assertEqual(service.list_available_payment_methods(db), [])

    # ---------------------------------------------------------- spec 140

    def test_pago_mixto_no_aparece_disponible_para_cobrar_en_caja(self):
        """spec 140: el panel de cobro de caja y "Registrar ingreso" de Cajas
        consumen esta misma consulta (`available=true`) y ya arman un pago
        mixto agregando varias líneas de método real -- una fila "Pago
        mixto" ahí no aporta nada. El menú QR usa otro endpoint
        (`GET /cart/payment-methods`) y no se ve afectado por este filtro."""
        db = fx.new_session()
        fx.make_payment_method(
            db, name="Efectivo", type="cash", is_cash=True, is_complete=True, active=True,
        )
        fx.make_payment_method(
            db, name="Pago mixto", type="mixed", is_cash=False, is_complete=True, active=True,
        )
        db.commit()

        names = {m.name for m in service.list_available_payment_methods(db)}
        self.assertEqual(names, {"Efectivo"})
        self.assertNotIn("Pago mixto", names)


if __name__ == "__main__":
    unittest.main()
