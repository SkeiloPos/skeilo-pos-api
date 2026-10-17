"""Las funciones públicas de `app/api/v1/orders/additions.py` (spec 123,
Historias 1, 2 y 4) — agregar una adición a una línea de pedido ya creada.

Dominio enteramente nuevo (greenfield, confirmado en `research.md`): antes de
esta spec no existía ningún camino para modificar un `OrderItem` ya
persistido — `add_item_to_order`/`create_order`/`void_item` (reemplazo)
siempre insertan una fila nueva.

El gating por módulo de inventario (`has_module_access`) se controla con un
mock en vez de sembrar `shared.plans`/`shared.tenants` reales: esos fixtures
viven en un motor SQLite separado (`plan_fixtures.py`, dominio distinto),
y la mecánica de "¿el tenant tiene el módulo?" ya está exhaustivamente
probada en `test_plan_module_access.py` — aquí solo importa que `additions`
se comporte distinto según la respuesta, no cómo se resuelve esa respuesta.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_additions -v
"""
from decimal import Decimal
import unittest
from unittest import mock

from fastapi import HTTPException

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.catalog.schemas import OptionSelectionIn, UpdateAdditionQuantityIn
from app.api.v1.orders import additions, service
from app.models.order_item import OrderItemOption
from app.models.sale import Sale

PRECIO = Decimal("15000")
PRECIO_ADICION = Decimal("2000")


class TestAddAdditionToOrderItem(unittest.TestCase):
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

    def _item(self, *, quantity=1, estado_cocina="pendiente", order_status=None):
        if order_status is not None:
            self.order.status = order_status
            self.db.flush()
        return fx.make_order_item(
            self.db, self.order, self.variant, quantity=quantity,
            unit_price=PRECIO, estado_cocina=estado_cocina,
        )

    def _agregar(self, item, *, quantity=1, con_inventario=True):
        body = OptionSelectionIn(option_id=self.option.id, quantity=quantity)
        with mock.patch.object(additions, "has_module_access", return_value=con_inventario):
            return additions.add_addition_to_order_item(self.db, item.id, body, self.user, self.tenant)

    # ------------------------------------------------- Historia 1 (sin pagar)

    def test_agregar_adicion_a_pedido_sin_pagar_recalcula_normal(self):
        item = self._item()
        order = self._agregar(item)

        self.db.refresh(item)
        [opcion] = item.options
        self.assertEqual(item.addons_total, PRECIO_ADICION)
        self.assertEqual(item.line_total, PRECIO + PRECIO_ADICION)
        self.assertFalse(opcion.added_after_sale)
        self.assertIsNone(opcion.complementary_sale_id)
        self.assertEqual(service.order_pending_balance(self.db, order.id), Decimal("0"))
        self.assertEqual(
            self.db.query(Sale).filter(Sale.customer_order_id == order.id).count(), 0
        )

    def test_agregar_adicion_sin_pagar_descuenta_inventario_de_inmediato_sin_tocar_la_receta(self):
        """`/speckit-analyze` G1/research.md D9: el insumo de la adición se
        descuenta ya mismo (no se difiere al cobro) y la receta base de la
        variante (sin receta configurada aquí) no se toca."""
        item = self._item()
        self._agregar(item)

        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("9"))

    def test_agregar_adicion_sin_pagar_sin_modulo_de_inventario_no_valida_ni_descuenta(self):
        item = self._item()
        self._agregar(item, con_inventario=False)

        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("10"))

    def test_adicion_por_cantidad_no_se_multiplica_por_la_cantidad_de_producto(self):
        """FR-004 (regresión de spec 117 sobre este call-site nuevo, `/speckit-analyze` G2):
        una adición de un grupo 'Por cantidad' cobra exactamente lo elegido, sin
        importar la cantidad de producto de la línea."""
        item = self._item(quantity=2)
        self._agregar(item, quantity=1)

        self.db.refresh(item)
        self.assertEqual(item.addons_total, PRECIO_ADICION)
        self.assertEqual(item.line_total, PRECIO * 2 + PRECIO_ADICION)

    def test_adicion_conteo_se_multiplica_por_la_cantidad_de_producto(self):
        grupo_conteo = fx.make_option_group(self.db, selection_mode="conteo")
        fx.link_variant_group(self.db, self.variant, grupo_conteo)
        opcion_conteo = fx.make_option(self.db, group=grupo_conteo, extra_price=Decimal("1000"))
        item = self._item(quantity=2)

        body = OptionSelectionIn(option_id=opcion_conteo.id, quantity=1)
        with mock.patch.object(additions, "has_module_access", return_value=False):
            additions.add_addition_to_order_item(self.db, item.id, body, self.user, self.tenant)

        self.db.refresh(item)
        # per_line=False: el extra entra a unit_price, multiplicado por la
        # cantidad de producto vía la propiedad `line_total`.
        self.assertEqual(item.unit_price, PRECIO + Decimal("1000"))
        self.assertEqual(item.line_total, (PRECIO + Decimal("1000")) * 2)

    # ------------------------------------------------ Historia 2 (ya pagado)

    def test_agregar_adicion_a_pedido_ya_pagado_genera_saldo_pendiente_sin_tocar_la_sale(self):
        sale = fx.make_sale(self.db, order=self.order, total=Decimal("22000"))
        item = self._item()

        snapshot_antes = (sale.total, sale.subtotal, sale.status)

        order = self._agregar(item)

        self.db.refresh(item)
        [opcion] = item.options
        self.assertTrue(opcion.added_after_sale)
        self.assertIsNone(opcion.complementary_sale_id)
        self.assertEqual(
            service.order_pending_balance(self.db, order.id), PRECIO_ADICION
        )
        self.db.refresh(sale)
        self.assertEqual((sale.total, sale.subtotal, sale.status), snapshot_antes)

    def test_agregar_adicion_a_pedido_ya_pagado_no_descuenta_inventario_todavia(self):
        fx.make_sale(self.db, order=self.order)
        item = self._item()
        self._agregar(item)

        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("10"))

    def test_varias_adiciones_sobre_distintas_lineas_del_mismo_pedido_pagado_suman(self):
        fx.make_sale(self.db, order=self.order)
        item1 = self._item()
        item2 = self._item()
        self._agregar(item1)
        self._agregar(item2)

        self.assertEqual(
            service.order_pending_balance(self.db, self.order.id), PRECIO_ADICION * 2
        )

    def test_anular_una_linea_con_adicion_pendiente_retira_su_porcion_del_saldo(self):
        fx.make_sale(self.db, order=self.order)
        item = self._item()
        self._agregar(item)
        self.assertEqual(service.order_pending_balance(self.db, self.order.id), PRECIO_ADICION)

        item.estado_cocina = "anulado"
        self.db.commit()

        self.assertEqual(service.order_pending_balance(self.db, self.order.id), Decimal("0"))

    # --------------------------------------------- Historia 4 (restricciones)

    def test_linea_en_estado_listo_rechaza_con_409_y_texto_de_ayuda(self):
        item = self._item(estado_cocina="listo")
        body = OptionSelectionIn(option_id=self.option.id, quantity=1)
        with self.assertRaises(HTTPException) as ctx:
            additions.add_addition_to_order_item(self.db, item.id, body, self.user, self.tenant)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "Producto ya preparado: agrega un producto nuevo")

        self.db.refresh(item)
        self.assertEqual(item.addons_total, Decimal("0"))

    def test_linea_anulada_rechaza_con_409(self):
        item = self._item(estado_cocina="anulado")
        body = OptionSelectionIn(option_id=self.option.id, quantity=1)
        with self.assertRaises(HTTPException) as ctx:
            additions.add_addition_to_order_item(self.db, item.id, body, self.user, self.tenant)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_pedido_cancelado_rechaza_con_409_aunque_el_item_siga_pendiente(self):
        """Hallazgo 2 de `tasks.md`: un pedido cancelado no anula el
        `estado_cocina` de sus ítems (bugfix spec 050) -- el chequeo de
        estado de línea por sí solo no basta."""
        item = self._item(order_status="cancelada")
        self.assertEqual(item.estado_cocina, "pendiente")
        body = OptionSelectionIn(option_id=self.option.id, quantity=1)
        with self.assertRaises(HTTPException) as ctx:
            additions.add_addition_to_order_item(self.db, item.id, body, self.user, self.tenant)
        self.assertEqual(ctx.exception.status_code, 409)

        self.db.refresh(item)
        self.assertEqual(item.addons_total, Decimal("0"))

    def test_stock_insuficiente_rechaza_sin_cambiar_la_linea_ni_el_total(self):
        self.insumo.current_stock = Decimal("0")
        self.db.commit()
        item = self._item()
        body = OptionSelectionIn(option_id=self.option.id, quantity=1)
        with mock.patch.object(additions, "has_module_access", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                additions.add_addition_to_order_item(self.db, item.id, body, self.user, self.tenant)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["error"], "Stock insuficiente")
        self.assertEqual(ctx.exception.detail["insumo"], self.insumo.name)

        self.db.refresh(item)
        self.assertEqual(item.addons_total, Decimal("0"))
        self.assertEqual(len(item.options), 0)

    def test_stock_insuficiente_sin_modulo_de_inventario_no_rechaza(self):
        self.insumo.current_stock = Decimal("0")
        self.db.commit()
        item = self._item()
        self._agregar(item, con_inventario=False)

        self.db.refresh(item)
        self.assertEqual(item.addons_total, PRECIO_ADICION)

    def test_opcion_repetida_en_la_misma_linea_rechaza_422(self):
        item = self._item()
        self._agregar(item)

        body = OptionSelectionIn(option_id=self.option.id, quantity=1)
        with mock.patch.object(additions, "has_module_access", return_value=True):
            with self.assertRaises(HTTPException) as ctx:
                additions.add_addition_to_order_item(self.db, item.id, body, self.user, self.tenant)
        self.assertEqual(ctx.exception.status_code, 422)


class TestUpdateAdditionQuantity(TestAddAdditionToOrderItem):
    """`PATCH /orders/items/{item_id}/additions/{option_id}` (spec 123): subir o bajar
    la cantidad de una adición que ya está en la línea, en vez de obligar a anular toda
    la línea por un ajuste de un topping. Hereda el `setUp` de
    `TestAddAdditionToOrderItem` (misma variante/grupo/opción/insumo) -- este dominio
    solo tiene sentido después de que la opción ya está en la línea."""

    def _subir(self, item, *, quantity, con_inventario=True):
        body = UpdateAdditionQuantityIn(quantity=quantity)
        with mock.patch.object(additions, "has_module_access", return_value=con_inventario):
            return additions.update_addition_quantity(
                self.db, item.id, self.option.id, body, self.user, self.tenant
            )

    def test_sube_la_cantidad_y_recalcula_el_precio_por_el_delta(self):
        item = self._item()
        self._agregar(item, quantity=2)
        order = self._subir(item, quantity=5)

        self.db.refresh(item)
        [opcion] = item.options
        self.assertEqual(opcion.quantity, 5)
        self.assertEqual(item.addons_total, PRECIO_ADICION * 5)
        self.assertEqual(item.line_total, PRECIO + PRECIO_ADICION * 5)
        self.assertEqual(service.order_pending_balance(self.db, order.id), Decimal("0"))

    def test_sube_la_cantidad_descuenta_solo_el_delta_de_inventario(self):
        item = self._item()
        self._agregar(item, quantity=2)
        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("8"))

        self._subir(item, quantity=5)

        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("5"))

    def test_opcion_que_no_esta_en_la_linea_rechaza_con_404(self):
        item = self._item()
        with self.assertRaises(HTTPException) as ctx:
            self._subir(item, quantity=2)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_baja_la_cantidad_y_recalcula_el_precio_por_el_delta(self):
        item = self._item()
        self._agregar(item, quantity=5)
        order = self._subir(item, quantity=2)

        self.db.refresh(item)
        [opcion] = item.options
        self.assertEqual(opcion.quantity, 2)
        self.assertEqual(item.addons_total, PRECIO_ADICION * 2)
        self.assertEqual(item.line_total, PRECIO + PRECIO_ADICION * 2)
        self.assertEqual(service.order_pending_balance(self.db, order.id), Decimal("0"))

    def test_baja_la_cantidad_devuelve_al_stock_el_delta(self):
        item = self._item()
        self._agregar(item, quantity=5)
        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("5"))

        self._subir(item, quantity=2)

        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("8"))

    def test_baja_la_cantidad_nunca_falla_por_stock_insuficiente(self):
        """`reverse_addition` siempre devuelve, nunca descuenta -- bajar nunca puede
        chocar con el chequeo de disponibilidad que sí aplica al subir."""
        item = self._item()
        self._agregar(item, quantity=5)
        self.insumo.current_stock = Decimal("0")
        self.db.commit()

        self._subir(item, quantity=1)

        self.db.refresh(item)
        [opcion] = item.options
        self.assertEqual(opcion.quantity, 1)

    def test_bajar_sin_modulo_de_inventario_no_devuelve_nada(self):
        item = self._item()
        self._agregar(item, quantity=5, con_inventario=False)
        self._subir(item, quantity=2, con_inventario=False)

        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("10"))

    def test_misma_cantidad_rechaza_con_422(self):
        item = self._item()
        self._agregar(item, quantity=3)
        with self.assertRaises(HTTPException) as ctx:
            self._subir(item, quantity=3)
        self.assertEqual(ctx.exception.status_code, 422)

    def test_adicion_ya_liquidada_por_pago_complementario_rechaza_con_409(self):
        fx.make_sale(self.db, order=self.order)
        item = self._item()
        self._agregar(item)
        [opcion] = item.options
        liquidacion = fx.make_sale(self.db, order=self.order)
        opcion.complementary_sale_id = liquidacion.id
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._subir(item, quantity=5)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_linea_en_estado_listo_rechaza_con_409(self):
        item = self._item()
        self._agregar(item, quantity=2)
        item.estado_cocina = "listo"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._subir(item, quantity=5)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_pedido_cancelado_rechaza_con_409(self):
        item = self._item()
        self._agregar(item, quantity=2)
        self.order.status = "cancelada"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._subir(item, quantity=5)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_sin_modulo_de_inventario_no_valida_ni_descuenta_el_delta(self):
        item = self._item()
        self._agregar(item, quantity=2, con_inventario=False)
        self._subir(item, quantity=5, con_inventario=False)

        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("10"))

    def test_stock_insuficiente_para_el_delta_rechaza_sin_cambiar_la_cantidad(self):
        item = self._item()
        self._agregar(item, quantity=2)
        self.insumo.current_stock = Decimal("1")
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._subir(item, quantity=5)
        self.assertEqual(ctx.exception.status_code, 409)

        self.db.refresh(item)
        [opcion] = item.options
        self.assertEqual(opcion.quantity, 2)


class TestRemoveAddition(TestAddAdditionToOrderItem):
    """`DELETE /orders/items/{item_id}/additions/{option_id}` (spec 123): quitar del
    todo una adición -- hasta ahora la única forma de deshacerla era anular la línea
    entera y rehacerla. Hereda el `setUp` de `TestAddAdditionToOrderItem`."""

    def _quitar(self, item, option_id=None, *, con_inventario=True):
        with mock.patch.object(additions, "has_module_access", return_value=con_inventario):
            return additions.remove_addition(
                self.db, item.id, option_id or self.option.id, self.user, self.tenant
            )

    def test_quita_la_opcion_y_recalcula_el_precio(self):
        item = self._item()
        self._agregar(item, quantity=3)
        order = self._quitar(item)

        self.db.refresh(item)
        self.assertEqual(len(item.options), 0)
        self.assertEqual(item.addons_total, Decimal("0"))
        self.assertEqual(item.line_total, PRECIO)
        self.assertEqual(service.order_pending_balance(self.db, order.id), Decimal("0"))

    def test_quita_la_opcion_y_devuelve_todo_su_consumo_al_stock(self):
        item = self._item()
        self._agregar(item, quantity=3)
        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("7"))

        self._quitar(item)

        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("10"))

    def test_opcion_que_no_esta_en_la_linea_rechaza_con_404(self):
        item = self._item()
        with self.assertRaises(HTTPException) as ctx:
            self._quitar(item)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_adicion_ya_liquidada_por_pago_complementario_rechaza_con_409(self):
        fx.make_sale(self.db, order=self.order)
        item = self._item()
        self._agregar(item)
        [opcion] = item.options
        liquidacion = fx.make_sale(self.db, order=self.order)
        opcion.complementary_sale_id = liquidacion.id
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._quitar(item)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_linea_en_estado_listo_rechaza_con_409(self):
        item = self._item()
        self._agregar(item, quantity=2)
        item.estado_cocina = "listo"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._quitar(item)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_pedido_cancelado_rechaza_con_409(self):
        item = self._item()
        self._agregar(item, quantity=2)
        self.order.status = "cancelada"
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self._quitar(item)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_sin_modulo_de_inventario_no_devuelve_nada(self):
        item = self._item()
        self._agregar(item, quantity=3, con_inventario=False)
        self._quitar(item, con_inventario=False)

        self.db.refresh(self.insumo)
        self.assertEqual(self.insumo.current_stock, Decimal("10"))

    def test_adicion_pendiente_de_pedido_pagado_se_quita_sin_tocar_la_sale(self):
        sale = fx.make_sale(self.db, order=self.order, total=Decimal("22000"))
        item = self._item()
        self._agregar(item)
        self.assertEqual(service.order_pending_balance(self.db, self.order.id), PRECIO_ADICION)

        snapshot_antes = (sale.total, sale.subtotal, sale.status)
        self._quitar(item)

        self.assertEqual(service.order_pending_balance(self.db, self.order.id), Decimal("0"))
        self.db.refresh(sale)
        self.assertEqual((sale.total, sale.subtotal, sale.status), snapshot_antes)

    def test_quitar_una_opcion_obligatoria_de_un_grupo_conteo_rechaza_con_422(self):
        """Un sabor obligatorio que además descuenta inventario (conteo, min_select=1,
        `item_quantity>0`) no se puede quitar y dejar la línea sin ninguno --
        `validate_option_selection` es la misma guardia que ya usa
        `add_addition_to_order_item` al agregar. Tiene que descontar inventario: un
        grupo obligatorio que NO descuenta solo se tolera con un warning mientras
        `STRICT_OPTION_SELECTION=False` (default), igual que ya pasa al agregar --
        "los grupos que descuentan se validan siempre" (`pricing.py`)."""
        grupo_sabor = fx.make_option_group(
            self.db, selection_mode="conteo", min_select=1, max_select=1,
        )
        fx.link_variant_group(self.db, self.variant, grupo_sabor, min_select=1, max_select=1)
        sabor = fx.make_option(
            self.db, group=grupo_sabor, extra_price=Decimal("0"),
            inventory_item_id=self.insumo.id, item_quantity=Decimal("1"),
        )
        item = self._item()

        body = OptionSelectionIn(option_id=sabor.id, quantity=1)
        with mock.patch.object(additions, "has_module_access", return_value=False):
            additions.add_addition_to_order_item(self.db, item.id, body, self.user, self.tenant)

        with self.assertRaises(HTTPException) as ctx:
            self._quitar(item, option_id=sabor.id, con_inventario=False)
        self.assertEqual(ctx.exception.status_code, 422)

        self.db.refresh(item)
        self.assertEqual(len(item.options), 1)

    def test_quitar_un_topping_opcional_no_choca_con_ningun_minimo(self):
        """Contraste con el test anterior: un grupo "cantidad" nunca exige mínimo
        (research.md Decisión 3), así que quitar su única opción siempre es válido."""
        item = self._item()
        self._agregar(item, quantity=3, con_inventario=False)

        self._quitar(item, con_inventario=False)

        self.db.refresh(item)
        self.assertEqual(len(item.options), 0)


if __name__ == "__main__":
    unittest.main()
