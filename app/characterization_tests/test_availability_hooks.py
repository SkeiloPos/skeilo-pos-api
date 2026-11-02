"""Spec 148 (US6) — gancho de sesión sobre `record_movement`/`apply_adjustment`
(`app/core/availability_hooks.py`, research.md D4): detecta transiciones de
pedibilidad causadas por inventario y publica `availability.changed` después
del commit, una sola vez por transacción.

El gancho se instala a mano en cada test (`install_availability_hooks()`,
idempotente) -- nunca al importar, así que ningún otro módulo de
characterization tests lo activa por accidente.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_availability_hooks -v
"""
from __future__ import annotations

import ast
import os
import unittest
from decimal import Decimal
from unittest import mock

from app.characterization_tests import cart_fixtures as fx
from app.core import events
from app.core.availability_hooks import install_availability_hooks
from app.inventory_engine.stock import apply_adjustment, record_movement


class AvailabilityHooksTests(unittest.TestCase):
    def setUp(self):
        install_availability_hooks()
        self.db = fx.new_session()
        self.db.info["tenant_id"] = 7

    def _seed_single_variant_product(self, stock=Decimal("5"), quantity=Decimal("1")):
        category = fx.make_category(self.db)
        product = fx.make_product(self.db, category=category, available=True, active=True)
        variant = fx.make_variant(self.db, product=product, price=Decimal("10000"))
        item = fx.make_inventory_item(self.db, current_stock=stock)
        fx.make_recipe_item(self.db, variant, item, quantity=quantity)
        self.db.commit()
        return product, variant, item

    def test_movimiento_que_cruza_a_cero_publica_un_evento(self):
        product, variant, item = self._seed_single_variant_product(stock=Decimal("1"))

        with mock.patch("app.core.availability_hooks.events.availability_changed") as pub:
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            self.db.commit()

        pub.assert_called_once()
        tenant_id, changes = pub.call_args[0]
        self.assertEqual(tenant_id, 7)
        kinds = {(c["kind"], c["id"]) for c in changes}
        self.assertIn(("variant", variant.id), kinds)
        self.assertIn(("product", product.id), kinds)

    def test_reponer_publica_un_evento_de_regreso(self):
        product, variant, item = self._seed_single_variant_product(stock=Decimal("1"))
        record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
        self.db.commit()

        with mock.patch("app.core.availability_hooks.events.availability_changed") as pub:
            record_movement(self.db, item.id, type="in", quantity=Decimal("1"))
            self.db.commit()

        pub.assert_called_once()
        _tenant_id, changes = pub.call_args[0]
        variant_changes = [c for c in changes if c["kind"] == "variant" and c["id"] == variant.id]
        self.assertEqual(len(variant_changes), 1)
        self.assertTrue(variant_changes[0]["available"])

    def test_stock_10_a_9_no_publica_nada(self):
        _product, _variant, item = self._seed_single_variant_product(stock=Decimal("10"))

        with mock.patch("app.core.availability_hooks.events.availability_changed") as pub:
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            self.db.commit()

        pub.assert_not_called()

    def test_baja_y_repone_el_mismo_insumo_en_la_misma_transaccion_no_publica(self):
        _product, _variant, item = self._seed_single_variant_product(stock=Decimal("1"))

        with mock.patch("app.core.availability_hooks.events.availability_changed") as pub:
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            record_movement(self.db, item.id, type="in", quantity=Decimal("1"))
            self.db.commit()

        pub.assert_not_called()

    def test_insumo_compartido_por_varios_dependientes_produce_un_solo_evento(self):
        category = fx.make_category(self.db)
        item = fx.make_inventory_item(self.db, current_stock=Decimal("1"))

        product_a = fx.make_product(self.db, category=category, available=True, active=True)
        variant_a = fx.make_variant(self.db, product=product_a, price=Decimal("10000"))
        fx.make_recipe_item(self.db, variant_a, item, quantity=Decimal("1"))

        product_b = fx.make_product(self.db, category=category, available=True, active=True)
        variant_b = fx.make_variant(self.db, product=product_b, price=Decimal("12000"))
        fx.make_recipe_item(self.db, variant_b, item, quantity=Decimal("1"))

        group = fx.make_option_group(self.db, min_select=0, selection_mode="cantidad")
        option = fx.make_option(self.db, group=group, inventory_item_id=item.id, item_quantity=Decimal("1"))
        self.db.commit()

        with mock.patch("app.core.availability_hooks.events.availability_changed") as pub:
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            self.db.commit()

        pub.assert_called_once()
        _tenant_id, changes = pub.call_args[0]
        ids = {(c["kind"], c["id"]) for c in changes}
        self.assertIn(("variant", variant_a.id), ids)
        self.assertIn(("variant", variant_b.id), ids)
        self.assertIn(("product", product_a.id), ids)
        self.assertIn(("product", product_b.id), ids)
        self.assertIn(("option", option.id), ids)  # también depende del mismo insumo

    def test_rollback_no_publica(self):
        _product, _variant, item = self._seed_single_variant_product(stock=Decimal("1"))

        with mock.patch("app.core.availability_hooks.events.availability_changed") as pub:
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            self.db.rollback()

        pub.assert_not_called()
        self.assertEqual(self.db.info.get("availability_stock"), None)
        self.assertEqual(self.db.info.get("availability_pending"), None)

    def test_commit_sin_movimientos_no_publica_y_sin_consultas_extra(self):
        self._seed_single_variant_product(stock=Decimal("5"))

        queries = []
        from sqlalchemy import event as sa_event
        conn = self.db.connection()

        def _count(*_a, **_kw):
            queries.append(1)

        sa_event.listen(conn.engine, "before_cursor_execute", _count)
        try:
            with mock.patch("app.core.availability_hooks.events.availability_changed") as pub:
                self.db.commit()
        finally:
            sa_event.remove(conn.engine, "before_cursor_execute", _count)

        pub.assert_not_called()
        self.assertEqual(len(queries), 0)

    def test_mas_del_tope_publica_refresh_true(self):
        category = fx.make_category(self.db)
        item = fx.make_inventory_item(self.db, current_stock=Decimal("1"))
        n = events.AVAILABILITY_EVENT_MAX_CHANGES + 1
        for _ in range(n):
            product = fx.make_product(self.db, category=category, available=True, active=True)
            variant = fx.make_variant(self.db, product=product, price=Decimal("1000"))
            fx.make_recipe_item(self.db, variant, item, quantity=Decimal("1"))
        self.db.commit()

        with mock.patch("app.core.events.publish") as publish_mock:
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            self.db.commit()

        publish_mock.assert_called_once()
        _args, kwargs = publish_mock.call_args
        self.assertEqual(kwargs["payload"], {"changes": [], "refresh": True})

    def test_si_el_calculo_o_la_publicacion_fallan_el_commit_se_completa_igual(self):
        _product, _variant, item = self._seed_single_variant_product(stock=Decimal("1"))

        with mock.patch(
            "app.core.availability_hooks.compute_availability",
            side_effect=RuntimeError("boom"),
        ):
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            self.db.commit()  # no debe lanzar

        self.db.refresh(item)
        self.assertEqual(item.current_stock, Decimal("0"))

    def test_product_available_nunca_cambia_por_este_gancho(self):
        product, _variant, item = self._seed_single_variant_product(stock=Decimal("1"))
        self.assertTrue(product.available)

        record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
        self.db.commit()
        self.db.refresh(product)
        self.assertTrue(product.available)  # sigue "disponible" a mano (FR-011)

    def test_apply_adjustment_tambien_dispara_el_gancho(self):
        _product, variant, item = self._seed_single_variant_product(stock=Decimal("1"))

        with mock.patch("app.core.availability_hooks.events.availability_changed") as pub:
            apply_adjustment(self.db, item.id, signed_delta=Decimal("-1"))
            self.db.commit()

        pub.assert_called_once()
        _tenant_id, changes = pub.call_args[0]
        self.assertTrue(any(c["kind"] == "variant" and c["id"] == variant.id for c in changes))

    def test_publica_despues_del_commit(self):
        _product, _variant, item = self._seed_single_variant_product(stock=Decimal("1"))

        order = []

        def _fake_publish(*_a, **_kw):
            order.append("publish")
            return None

        orig_commit = self.db.commit

        def _wrapped_commit():
            order.append("before_commit_call")
            return orig_commit()

        with mock.patch("app.core.availability_hooks.events.availability_changed", side_effect=_fake_publish):
            record_movement(self.db, item.id, type="out", quantity=Decimal("1"))
            _wrapped_commit()

        self.assertEqual(order, ["before_commit_call", "publish"])


class CurrentStockAssignmentGuardTests(unittest.TestCase):
    """T101 (research.md D4, riesgo): ningún movimiento futuro debe evitar
    `record_movement`/`apply_adjustment` sin que esta prueba falle."""

    _ALLOWED_FILE = os.path.join("app", "inventory_engine", "stock.py")
    _EXCLUDED_DIRS = (
        os.path.join("app", "scripts"),
        os.path.join("app", "characterization_tests"),
    )

    def _app_root(self) -> str:
        import app
        return os.path.dirname(os.path.abspath(app.__file__))

    def _iter_py_files(self):
        root = os.path.dirname(self._app_root())
        app_dir = self._app_root()
        for dirpath, _dirnames, filenames in os.walk(app_dir):
            rel_dir = os.path.relpath(dirpath, root)
            if any(rel_dir.startswith(excl) for excl in self._EXCLUDED_DIRS):
                continue
            for filename in filenames:
                if filename.endswith(".py"):
                    yield os.path.join(dirpath, filename), os.path.relpath(
                        os.path.join(dirpath, filename), root
                    )

    def test_ninguna_asignacion_a_current_stock_fuera_de_stock_py(self):
        offenders = []
        for abs_path, rel_path in self._iter_py_files():
            if rel_path == self._ALLOWED_FILE:
                continue
            with open(abs_path, "r", encoding="utf-8") as fh:
                source = fh.read()
            try:
                tree = ast.parse(source, filename=rel_path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                target_attrs = []
                if isinstance(node, ast.Assign):
                    target_attrs = [t for t in node.targets if isinstance(t, ast.Attribute)]
                elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
                    target_attrs = [node.target]
                for attr in target_attrs:
                    if attr.attr == "current_stock":
                        offenders.append(f"{rel_path}:{getattr(node, 'lineno', '?')}")

        self.assertEqual(
            offenders, [],
            f"Asignación a current_stock fuera de {self._ALLOWED_FILE}: {offenders}. "
            "Todo movimiento de stock debe pasar por record_movement/apply_adjustment "
            "para que el gancho de disponibilidad (spec 148) se entere.",
        )


if __name__ == "__main__":
    unittest.main()
