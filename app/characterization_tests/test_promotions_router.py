"""CONGELA comportamiento corregido: app/api/v1/promotions/router.py
(list_promotions) expone X-Server-Time — A-09 (registro-de-anomalias.md,
reapertura 2026-08-18, spec 023-correccion-zona-horaria-pos-staff).

Reescrito para la spec 063 (modelo por conjunto explícito de variantes;
decisión de negocio A-58…A-65 en registro-de-anomalias.md): la respuesta pierde
`overlaps` (A-59) y `priority` (A-58), el enum de entrada se reduce a
`{percent, package_price}` (A-62) y `PromotionResponse` gana `variants` /
`condition_text` / `closed_by_refactor_at` (FR-005, FR-025). El header
`X-Server-Time` **no cambia**.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_promotions_router -v
"""
from datetime import datetime, timezone
import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException, Response

from app.characterization_tests import cart_fixtures as fx
from app.characterization_tests import product_availability_fixtures as role_fx
from app.api.v1.promotions import router as promotions_router
from app.api.v1.promotions import service
from app.api.v1.promotions.schemas import PromotionUpdate
from app.core.dependencies import require_tenant_admin


def _list(db, user, response, **kw):
    kw.setdefault("page", 1)
    kw.setdefault("size", 20)
    kw.setdefault("status_filter", None)
    kw.setdefault("search", None)
    kw.setdefault("closed_by_refactor", None)
    return promotions_router.list_promotions(response=response, db=db, _=user, **kw)


class TestListPromotionsA09(unittest.TestCase):
    def test_expone_x_server_time_en_utc(self):
        db = fx.new_session()
        user = SimpleNamespace(id="u1")  # doble mínimo de get_current_user
        response = Response()

        instant = datetime(2026, 8, 18, 22, 30, 5, tzinfo=timezone.utc)
        with mock.patch("app.api.v1.promotions.router.datetime") as mocked:
            mocked.now.return_value = instant
            _list(db, user, response)

        self.assertEqual(response.headers["X-Server-Time"], instant.isoformat())

    def test_el_header_no_cambia_la_forma_de_la_respuesta(self):
        """El cuerpo (Page[PromotionResponse]) sigue igual: el header es
        aditivo. spec 063: cada item trae `variants` / `condition_text` y ya no
        trae `overlaps` / `priority`."""
        db = fx.new_session()
        cat = fx.make_category(db)
        prod = fx.make_product(db, category=cat)
        # spec 066: sin `name` explícito el fixture genera `variante-{uid}`, que no
        # es determinista y no permite afirmar el texto por nombres (research.md D-8).
        variant = fx.make_variant(db, product=prod, price=8000, name="Pequeño 8oz")
        promo = fx.make_promotion(db, name="10% Granizados")
        fx.add_rule_to_promotion(db, promo, type="percent", value=10, min_qty=1, variants=[variant])
        db.commit()
        user = SimpleNamespace(id="u1")
        response = Response()

        page = _list(db, user, response)

        self.assertEqual(page["total"], 1)
        self.assertIn("X-Server-Time", response.headers)
        item = page["items"][0]
        self.assertNotIn("overlaps", item)
        self.assertNotIn("priority", item)
        self.assertNotIn("targets", item)
        self.assertNotIn("variants", item)  # spec 063 (2026-09-01): vive en cada regla
        regla = item["rules"][0]
        self.assertEqual(len(regla["variants"]), 1)
        self.assertEqual(regla["variants"][0]["unit_price"], 8000)
        # spec 066 (A-66): el conjunto de una sola variante se nombra — el defecto
        # reportado era justamente `"10% en estas 1 variantes"`.
        self.assertEqual(regla["condition_text"], "10% en Pequeño 8oz")

    def test_filtro_closed_by_refactor_lista_las_finalizadas_por_la_migracion(self):
        """FR-025: `?closed_by_refactor=true` filtra `closed_by_refactor_at IS NOT NULL`."""
        db = fx.new_session()
        viva = fx.make_promotion(db, name="viva")
        cerrada = fx.make_promotion(db, name="cerrada por refactor", status="finished")
        cerrada.closed_by_refactor_at = datetime(2026, 8, 31, 12, 0)
        db.commit()
        user = SimpleNamespace(id="u1")

        solo_cerradas = _list(db, user, Response(), closed_by_refactor=True)
        self.assertEqual({i["name"] for i in solo_cerradas["items"]}, {"cerrada por refactor"})

        solo_vivas = _list(db, user, Response(), closed_by_refactor=False)
        self.assertEqual({i["name"] for i in solo_vivas["items"]}, {"viva"})

        self.assertEqual(viva.id is not None, True)  # silencia linters sobre `viva`


class TestUpdatePromotionVisibleEnQrPermisoSpec144(unittest.TestCase):
    """spec 144 (T012): `PATCH /promotions/{id}` -- incluido el campo nuevo
    `visible_en_qr` -- sigue dependiendo de `require_tenant_admin`
    (`update_promotion`, `app/api/v1/promotions/router.py`), sin excepción
    por campo. Mismo patrón que `test_product_visibility_permission.py`
    para `visible_en_qr` de `Product`: la dependencia de FastAPI rechaza
    con 403 antes de llegar a la lógica de negocio."""

    def test_rol_sin_acceso_de_administrador_no_alcanza_require_tenant_admin(self):
        db = role_fx.new_session()
        tenant = role_fx.make_tenant(db)
        cajero = role_fx.make_user(db, tenant=tenant, role=role_fx.make_role(db, name="CASHIER"))

        with self.assertRaises(HTTPException) as ctx:
            require_tenant_admin(cajero)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_si_puede_cambiar_visible_en_qr(self):
        db = role_fx.new_session()
        tenant = role_fx.make_tenant(db)
        admin = role_fx.make_user(db, tenant=tenant, role=role_fx.make_role(db, name="ADMIN"))
        self.assertIs(require_tenant_admin(admin), admin)

        promo_db = fx.new_session()
        promo = fx.make_promotion(promo_db, status="active", visible_en_qr=True)
        promo_db.commit()

        service.update(promo_db, promo, PromotionUpdate(visible_en_qr=False))
        promo_db.commit()

        self.assertFalse(promo.visible_en_qr)


if __name__ == "__main__":
    unittest.main()
