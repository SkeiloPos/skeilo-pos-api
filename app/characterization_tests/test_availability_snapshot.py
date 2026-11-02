"""Spec 148 — consulta de reconciliación de disponibilidad
(contracts/availability-snapshot-api.md): `GET /menu/availability` y
`GET /menu/qr-token/{token}/availability`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_availability_snapshot -v
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Iterator
from unittest import mock
import unittest

from fastapi import HTTPException

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.menu.router import (
    _availability_snapshot, _build_menu, menu_availability_by_signed_qr,
)
from app.core.qr_context import QrContext


class AvailabilitySnapshotEquivalenceTests(unittest.TestCase):
    def _seed_recipe_catalog(self, db):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        sin_stock = fx.make_variant(db, product=product, price=Decimal("10000"))
        item = fx.make_inventory_item(db, current_stock=Decimal("0"))
        fx.make_recipe_item(db, sin_stock, item, quantity=Decimal("1"))
        con_stock = fx.make_variant(db, product=product, price=Decimal("12000"))
        other_product = fx.make_product(db, category=category, available=True)
        fx.make_variant(db, product=other_product, price=Decimal("8000"))
        db.commit()
        return product, sin_stock, con_stock, other_product

    def test_presentacion_sin_stock_de_receta_aparece_no_disponible(self):
        db = fx.new_session()
        product, sin_stock, con_stock, _other = self._seed_recipe_catalog(db)
        tenant = SimpleNamespace(id=7)

        with mock.patch("app.api.v1.menu.router.events.latest_version", return_value=None):
            snap = _availability_snapshot(db, tenant, only_visible_en_qr=False)

        variant_by_id = {v.id: v for v in snap.variants}
        self.assertFalse(variant_by_id[sin_stock.id].available)
        self.assertTrue(variant_by_id[con_stock.id].available)
        product_by_id = {p.id: p for p in snap.products}
        self.assertTrue(product_by_id[product.id].available)  # tiene otra presentación

    def test_equivalencia_con_get_menu(self):
        """Los `available`/`sold_out` del snapshot coinciden con los de
        `GET /menu` para el mismo catálogo (misma fuente: el motor)."""
        db = fx.new_session()
        self._seed_recipe_catalog(db)
        tenant = SimpleNamespace(id=7)

        with mock.patch("app.api.v1.menu.router.events.latest_version", return_value=None):
            snap = _availability_snapshot(db, tenant, only_visible_en_qr=False)
        menu = _build_menu(db, only_visible_en_qr=False)

        menu_products = {p.id: p for c in menu for p in c.products}
        for state in snap.products:
            self.assertEqual(state.available, menu_products[state.id].available)
            self.assertEqual(state.sold_out, menu_products[state.id].sold_out)

        menu_variants = {v.id: v for c in menu for p in c.products for v in p.variants}
        for state in snap.variants:
            self.assertEqual(state.available, menu_variants[state.id].available)

    def test_as_of_es_null_si_redis_falla_y_sigue_200(self):
        db = fx.new_session()
        self._seed_recipe_catalog(db)
        tenant = SimpleNamespace(id=7)

        with mock.patch(
            "app.api.v1.menu.router.events.latest_version", return_value=None,
        ):
            snap = _availability_snapshot(db, tenant, only_visible_en_qr=False)

        self.assertIsNone(snap.as_of)


class AvailabilitySnapshotQrVisibilityTests(unittest.TestCase):
    def test_version_del_comensal_excluye_producto_no_visible_en_qr(self):
        db = fx.new_session()
        category = fx.make_category(db)
        visible = fx.make_product(db, category=category, visible_en_qr=True)
        fx.make_variant(db, product=visible, price=Decimal("10000"))
        hidden = fx.make_product(db, category=category, visible_en_qr=False)
        fx.make_variant(db, product=hidden, price=Decimal("10000"))
        db.commit()
        tenant = SimpleNamespace(id=7)

        with mock.patch("app.api.v1.menu.router.events.latest_version", return_value=None):
            snap = _availability_snapshot(db, tenant, only_visible_en_qr=True)

        product_ids = {p.id for p in snap.products}
        self.assertIn(visible.id, product_ids)
        self.assertNotIn(hidden.id, product_ids)


class QrTokenAvailabilityEndpointTests(unittest.TestCase):
    """`GET /menu/qr-token/{token}/availability` rechaza un token invalido
    igual que `GET /menu/qr-token/{token}` (mismo `open_qr_context`)."""

    def test_token_invalido_se_rechaza_con_401(self):
        @contextmanager
        def _raise(token: str) -> Iterator[QrContext]:
            raise HTTPException(401, "Token de QR inválido")
            yield  # pragma: no cover

        async def _noop_rate_limit(*_args, **_kwargs):
            return None

        async def _run():
            with mock.patch("app.api.v1.menu.router.open_qr_context", _raise), \
                 mock.patch("app.api.v1.menu.router.rate_limit", _noop_rate_limit):
                await menu_availability_by_signed_qr("token-invalido", request=mock.Mock())

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(_run())
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
