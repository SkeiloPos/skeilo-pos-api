"""Paridad del desglose del borrador con el del cobro — spec 087, FR-016 (A-90).

El "TOTAL ORDEN" de la página de pedido manual llama a `POST /orders/draft-preview`
(`compute_draft_preview`). Para que el total mostrado tras guardar coincida con el que
calcula el backend al cobrar (`compute_checkout_preview`), el frontend debe mandar el
**conjunto vigente completo** (ítems guardados no anulados + borradores) y no solo el
borrador. No hay cambio de backend: estos tests son la línea base del fix de frontend
(`research.md` D13).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_draft_preview_parity -v
"""
from decimal import Decimal
from uuid import uuid4
import unittest

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.catalog.schemas import OptionSelectionIn
from app.api.v1.orders import checkout, service
from app.api.v1.orders.schemas import DraftPreviewIn, OrderChannel, OrderCreate, OrderItemIn

PRECIO = Decimal("8000")


class TestDraftPreviewParity(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed(self):
        """Pedido abierto con una variante a $8.000 y promoción del 50 % llevando 2."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)
        category = fx.make_category(db)
        variant = fx.make_variant(
            db, product=fx.make_product(db, category=category), price=PRECIO
        )
        order = fx.make_customer_order(db, ts, status="abierta", channel="POS")
        promo = fx.make_promotion(db, status="active")
        fx.add_rule_to_promotion(
            db, promo, type="percent", value=Decimal("50"), min_qty=2, variants=[variant],
        )
        db.commit()
        return dict(db=db, order=order, variant=variant, category=category)

    def _draft(self, db, *cantidades_por_variante):
        return checkout.compute_draft_preview(
            db,
            DraftPreviewIn(
                items=[
                    OrderItemIn(product_variant_id=v.id, quantity=q)
                    for v, q in cantidades_por_variante
                ]
            ),
        )

    # ------------------------------------------------------------ Escenarios

    def test_conjunto_completo_da_el_mismo_total_que_el_cobro(self):
        """Un ítem guardado + uno nuevo: el preview del conjunto `[guardado + nuevo]`
        coincide con `compute_checkout_preview` del pedido ya guardado con ambos."""
        s = self._seed()
        db, order, variant = s["db"], s["order"], s["variant"]
        fx.make_order_item(db, order, variant, quantity=1)
        db.commit()

        # Borrador con 1 unidad más → 2 en total → aplica la promoción llevando 2.
        preview_conjunto = self._draft(db, (variant, 1), (variant, 1))

        fx.make_order_item(db, order, variant, quantity=1)
        db.commit()
        cobro = checkout.compute_checkout_preview(db, order.id)

        self.assertEqual(preview_conjunto.total, cobro.total)
        self.assertEqual(preview_conjunto.discount, cobro.discount)
        self.assertEqual(preview_conjunto.total, Decimal("8000"))

    def test_solo_el_item_nuevo_difiere_si_la_promocion_depende_de_la_cantidad_conjunta(self):
        """Evidencia de por qué el frontend no puede mandar solo el borrador: con
        únicamente el ítem nuevo (1 unidad) la promoción `min_qty=2` no aplica."""
        s = self._seed()
        db, order, variant = s["db"], s["order"], s["variant"]
        fx.make_order_item(db, order, variant, quantity=1)
        db.commit()

        solo_nuevo = self._draft(db, (variant, 1))
        conjunto = self._draft(db, (variant, 1), (variant, 1))

        self.assertEqual(solo_nuevo.discount, Decimal("0"))
        self.assertEqual(solo_nuevo.total, PRECIO)
        # El conjunto sí reevalúa la promoción (50 % de 2 × $8.000 = $8.000 de descuento).
        self.assertEqual(conjunto.discount, Decimal("8000"))
        self.assertNotEqual(solo_nuevo.discount, conjunto.discount)

    def test_combo_sumado_a_su_precio_coincide_con_el_cobro(self):
        """Los combos quedan fuera de `draft-preview`: el frontend suma su precio de
        combo al `total` del preview de los productos (FR-016, sin descuento
        promocional adicional). Debe coincidir con el cobro del pedido guardado."""
        s = self._seed()
        db, order, variant, category = s["db"], s["order"], s["variant"], s["category"]
        precio_combo = Decimal("9000")
        combo_promo = fx.make_promotion(db, status="active")
        combo_variant = fx.make_variant(
            db, product=fx.make_product(db, category=category), price=precio_combo
        )
        fx.make_order_item(db, order, variant, quantity=2)
        fx.make_order_item(
            db, order, combo_variant, quantity=1, unit_price=precio_combo,
            combo_id=combo_promo.id,
        )
        db.commit()

        total_mostrado = self._draft(db, (variant, 2)).total + precio_combo
        cobro = checkout.compute_checkout_preview(db, order.id)

        self.assertEqual(total_mostrado, cobro.total)
        self.assertEqual(cobro.total, Decimal("8000") + precio_combo)

    def test_grupo_cantidad_fuera_de_1a1_coincide_con_el_cobro_real(self):
        """Hallazgo 1 de `tasks.md`: `compute_draft_preview` es un 7º call-site de
        escritura no listado en `plan.md`/`research.md` -- construye su propia
        `SaleLine` con `compute_line_price` "folded" (sin separar `addons_total`), así
        que hoy diverge de `create_order` + `compute_checkout_preview` para una línea
        "cantidad" fuera de 1:1 (2 unidades de producto, 1 de la adición)."""
        db = fx.new_session()
        category = fx.make_category(db)
        variant = fx.make_variant(db, product=fx.make_product(db, category=category), price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("100"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        group = fx.make_option_group(
            db, selection_mode="cantidad", pricing_type="incluido", min_select=0, max_select=1,
        )
        fx.link_variant_group(db, variant, group, min_select=0, max_select=1)
        gomitas = fx.make_option(db, group=group, extra_price=Decimal("1000"))
        db.commit()

        item_in = OrderItemIn(
            product_variant_id=variant.id, quantity=2,
            options=[OptionSelectionIn(option_id=gomitas.id, quantity=1)],
        )
        preview = checkout.compute_draft_preview(db, DraftPreviewIn(items=[item_in]))

        order = service.create_order(
            db,
            OrderCreate(
                channel=OrderChannel.POS, customer_name="Cliente de prueba", items=[item_in],
            ),
            uuid4(),
        )
        cobro = checkout.compute_checkout_preview(db, order.id)

        self.assertEqual(preview.total, cobro.total)
        self.assertEqual(preview.total, PRECIO * 2 + Decimal("1000"))


if __name__ == "__main__":
    unittest.main()
