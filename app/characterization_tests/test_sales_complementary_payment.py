"""La función pública de `app/api/v1/sales/complementary.py` (spec 123,
Historias 3 y 4) — cobrar el saldo pendiente generado por adiciones
agregadas después de que un pedido ya tenía una `Sale` emitida.

Dominio enteramente nuevo (greenfield): antes de esta spec no existía ningún
concepto de "pago complementario" en ningún repositorio.

El gating por módulo de inventario se controla con un mock (mismo criterio
que `test_orders_additions.py`): la mecánica de "¿el tenant tiene el
módulo?" ya está probada exhaustivamente en `test_plan_module_access.py`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_sales_complementary_payment -v
"""
from decimal import Decimal
import unittest
from unittest import mock

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import additions, service
from app.api.v1.sales import complementary
from app.api.v1.sales.schemas import ComplementaryPaymentIn, PaymentIn
from app.models.order_item import OrderItemOption
from app.models.sale import Sale

PRECIO = Decimal("15000")
PRECIO_ADICION = Decimal("2000")


class TestSettlePendingBalance(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.table = fx.make_dining_table(self.db)
        self.ts = fx.make_table_session(self.db, table=self.table)
        self.order = fx.make_customer_order(self.db, self.ts, dining_table_id=self.table.id)
        self.variant = fx.make_variant(self.db, price=PRECIO)
        self.group = fx.make_option_group(self.db, selection_mode="cantidad")
        fx.link_variant_group(self.db, self.variant, self.group)
        self.insumo = fx.make_inventory_item(self.db, current_stock=Decimal("10"))
        self.option = fx.make_option(
            self.db, group=self.group, extra_price=PRECIO_ADICION,
            inventory_item_id=self.insumo.id, item_quantity=Decimal("1"),
        )
        self.user = fx.make_user_double()
        self.tenant = fx.make_tenant_double()
        fx.make_sale(self.db, order=self.order, total=Decimal("22000"))

        self.register = fx.make_cash_register(self.db)
        self.shift = fx.make_cash_shift(self.db, register=self.register)
        self.method = fx.make_payment_method(self.db)

    def _agregar_adicion(self, *, con_inventario=True):
        from app.api.v1.catalog.schemas import OptionSelectionIn
        item = fx.make_order_item(
            self.db, self.order, self.variant, quantity=1,
            unit_price=PRECIO, estado_cocina="pendiente",
        )
        body = OptionSelectionIn(option_id=self.option.id, quantity=1)
        with mock.patch.object(additions, "has_module_access", return_value=con_inventario):
            additions.add_addition_to_order_item(self.db, item.id, body, self.user, self.tenant)
        return item

    def _cobrar(self, monto, *, con_inventario=True):
        body = ComplementaryPaymentIn(
            cash_shift_id=self.shift.id,
            payments=[PaymentIn(payment_method_id=self.method.id, amount=monto)],
        )
        with mock.patch.object(complementary, "has_module_access", return_value=con_inventario):
            return complementary.settle_pending_balance(self.db, self.order.id, body, self.user, self.tenant)

    # --------------------------------------------------------------- Historia 3

    def test_cobro_exitoso_genera_sale_complementaria_y_descuenta_el_insumo(self):
        self._agregar_adicion()
        # Pedido ya pagado (`setUp` sembró la Sale original antes de agregar
        # la adición): el insumo NO se descontó al agregar -- se difiere a
        # este cobro (FR-012).
        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("10"))

        sale = self._cobrar(PRECIO_ADICION)

        self.assertEqual(sale.kind, "complementario")
        self.assertEqual(sale.customer_order_id, self.order.id)
        self.assertEqual(sale.total, PRECIO_ADICION)
        [opcion] = self.db.query(OrderItemOption).all()
        self.assertEqual(opcion.complementary_sale_id, sale.id)
        self.assertEqual(service.order_pending_balance(self.db, self.order.id), Decimal("0"))
        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("9"))

        original = self.db.query(Sale).filter(Sale.kind == "original").one()
        self.assertEqual(original.total, Decimal("22000"))

    def test_mas_de_un_pago_rechaza_422(self):
        self._agregar_adicion()
        body = ComplementaryPaymentIn(
            cash_shift_id=self.shift.id,
            payments=[
                PaymentIn(payment_method_id=self.method.id, amount=Decimal("1000")),
                PaymentIn(payment_method_id=self.method.id, amount=Decimal("1000")),
            ],
        )
        with mock.patch.object(complementary, "has_module_access", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                complementary.settle_pending_balance(self.db, self.order.id, body, self.user, self.tenant)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_monto_distinto_al_saldo_pendiente_rechaza_422(self):
        self._agregar_adicion()
        with self.assertRaises(HTTPException) as ctx:
            self._cobrar(Decimal("1"))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_saldo_pendiente_cero_rechaza_409(self):
        with self.assertRaises(HTTPException) as ctx:
            self._cobrar(Decimal("0.01"))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "No hay saldo pendiente por cobrar")

    def test_dos_ciclos_sucesivos_generan_dos_sales_complementarias_independientes(self):
        self._agregar_adicion()
        sale1 = self._cobrar(PRECIO_ADICION)

        self._agregar_adicion()
        sale2 = self._cobrar(PRECIO_ADICION)

        self.assertNotEqual(sale1.id, sale2.id)
        self.assertEqual(
            self.db.query(Sale).filter(Sale.kind == "complementario").count(), 2
        )
        self.db.refresh(sale1)
        self.assertEqual(sale1.total, PRECIO_ADICION)  # la primera no cambió

    # --------------------------------------------------------- Historia 4 (atómico)

    def test_stock_insuficiente_de_una_adicion_rechaza_el_cobro_completo(self):
        self._agregar_adicion()
        self.insumo.current_stock = Decimal("0")
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._cobrar(PRECIO_ADICION)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "Stock insuficiente")

        self.assertEqual(
            self.db.query(Sale).filter(Sale.kind == "complementario").count(), 0
        )
        self.assertEqual(service.order_pending_balance(self.db, self.order.id), PRECIO_ADICION)

    def test_stock_insuficiente_sin_modulo_de_inventario_cobra_igual(self):
        self._agregar_adicion(con_inventario=False)
        self.insumo.current_stock = Decimal("0")
        self.db.commit()

        sale = self._cobrar(PRECIO_ADICION, con_inventario=False)
        self.assertEqual(sale.kind, "complementario")


if __name__ == "__main__":
    unittest.main()
