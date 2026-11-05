"""Tests de la nueva funcionalidad — spec 141-modulo-inventario-unificado, SC-001:
el 100 % de las rutas de un módulo rechaza cuando el plan no lo incluye.

No son characterization tests — comportamiento enteramente nuevo. Recorre las
rutas de la aplicación real (`app.routes`) y comprueba que cada ruta de la
tabla de `contracts/inventario-de-rutas.md` lleva la dependencia de su módulo
(`require_module_access(...).module_key`) y que ninguna ruta de fuera de la
tabla la lleva «por accidente». Una ruta nueva bajo un prefijo del módulo sin
la dependencia —o una ruta que se quita o se agrega sin actualizar el
contrato— hace fallar la prueba.

    python -m unittest app.characterization_tests.test_module_route_inventory -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.routing import APIRoute

from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.main

API = "/api/v1"

#: Contrato `inventario-de-rutas.md`: módulo → (prefijos, total esperado de rutas).
TABLA = {
    "inventario": (
        (f"{API}/inventory", f"{API}/unit-measures", f"{API}/unit-measure-groups"),
        # /inventory 29 + /unit-measures 6 + /unit-measure-groups 6 + /reports/inventory 1 + /reports/profitability 1
        43,
    ),
    "promociones": ((f"{API}/promotions",), 7),
    # spec 150: primer punto real de enforcement de `cocina_access` (spec 149
    # ya había agregado el módulo al catálogo, sin ninguna ruta que lo exigiera).
    # spec 151: segundo punto de enforcement -- mover una tarjeta del tablero
    # exige el mismo módulo (`GET /orders/kitchen-board` NO lo exige, ver
    # contracts/kitchen-board.md).
    "cocina": ((f"{API}/orders/{{order_id}}/send-to-kitchen", f"{API}/orders/{{order_id}}/kitchen-status"), 2),
}

#: Rutas de reportes que sí pertenecen a `inventario` (el resto de /reports es ventas y no lleva módulo).
REPORTES_DE_INVENTARIO = (f"{API}/reports/inventory", f"{API}/reports/profitability")

#: Conteo por prefijo dentro de `inventario` (columna «Rutas» del contrato).
CONTEO_INVENTORY = 29
CONTEO_UNIT_MEASURES = 6
CONTEO_UNIT_MEASURE_GROUPS = 6


def _aplicacion_real():
    # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
    # PostgreSQL y migra. Se sustituye por una función vacía ANTES de importar.
    with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
        from app.main import app
    return app


def _modulos_de(route: APIRoute) -> set[str]:
    """Claves de módulo de las dependencias de la ruta (router + ruta)."""
    return {
        d.call.module_key
        for d in route.dependant.dependencies
        if getattr(d.call, "module_key", None) is not None
    }


def _empieza_por(path: str, prefijo: str) -> bool:
    return path == prefijo or path.startswith(prefijo + "/")


class TestInventarioDeRutasPorModulo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rutas = [r for r in _aplicacion_real().routes if isinstance(r, APIRoute)]

    def _rutas_del_modulo(self, modulo: str) -> list[APIRoute]:
        prefijos, _ = TABLA[modulo]
        prefijos = prefijos + (REPORTES_DE_INVENTARIO if modulo == "inventario" else ())
        return [r for r in self.rutas if any(_empieza_por(r.path, p) for p in prefijos)]

    def test_hay_exactamente_las_rutas_del_contrato_en_cada_modulo(self):
        for modulo, (_, esperado) in TABLA.items():
            with self.subTest(modulo=modulo):
                encontradas = sorted((r.path, sorted(r.methods)) for r in self._rutas_del_modulo(modulo))
                self.assertEqual(
                    len(encontradas),
                    esperado,
                    f"El contrato inventario-de-rutas.md dice {esperado} rutas para «{modulo}» y hay "
                    f"{len(encontradas)}: actualizar la tabla y este conteo en el mismo commit.\n"
                    + "\n".join(f"  {p} {m}" for p, m in encontradas),
                )

    def test_el_desglose_de_inventario_coincide_con_el_contrato(self):
        for prefijo, esperado in (
            (f"{API}/inventory", CONTEO_INVENTORY),
            (f"{API}/unit-measures", CONTEO_UNIT_MEASURES),
            (f"{API}/unit-measure-groups", CONTEO_UNIT_MEASURE_GROUPS),
        ):
            with self.subTest(prefijo=prefijo):
                self.assertEqual(len([r for r in self.rutas if _empieza_por(r.path, prefijo)]), esperado)
        for ruta in REPORTES_DE_INVENTARIO:
            with self.subTest(ruta=ruta):
                self.assertEqual(len([r for r in self.rutas if r.path == ruta]), 1)

    def test_cada_ruta_del_modulo_lleva_la_dependencia_de_su_modulo(self):
        for modulo in TABLA:
            for ruta in self._rutas_del_modulo(modulo):
                with self.subTest(modulo=modulo, ruta=ruta.path, metodos=sorted(ruta.methods)):
                    self.assertEqual(
                        _modulos_de(ruta),
                        {modulo},
                        f"{sorted(ruta.methods)} {ruta.path} debe llevar require_module_access('{modulo}') "
                        "y ninguna otra dependencia de módulo (FR-002/FR-004).",
                    )

    def test_compras_y_proveedores_se_gobiernan_solo_por_inventario(self):
        """FR-004, US2.3: las 8 rutas de compras y las de proveedores no llevan gate propio."""
        for r in self.rutas:
            if _empieza_por(r.path, f"{API}/inventory/purchases") or _empieza_por(r.path, f"{API}/inventory/suppliers"):
                with self.subTest(ruta=r.path, metodos=sorted(r.methods)):
                    self.assertEqual(_modulos_de(r), {"inventario"})

    def test_ninguna_ruta_fuera_de_la_tabla_lleva_dependencia_de_modulo(self):
        """Una ruta no puede quedar gobernada por un módulo «por accidente»:
        ventas, catálogo, pedidos, cobro y menú QR nunca se bloquean (FR-011)."""
        en_tabla = {id(r) for modulo in TABLA for r in self._rutas_del_modulo(modulo)}
        for r in self.rutas:
            if id(r) in en_tabla:
                continue
            with self.subTest(ruta=r.path, metodos=sorted(r.methods)):
                self.assertEqual(
                    _modulos_de(r),
                    set(),
                    f"{sorted(r.methods)} {r.path} lleva dependencia de módulo pero no está en "
                    "contracts/inventario-de-rutas.md",
                )

    def test_rutas_publicas_y_de_ventas_no_estan_gobernadas(self):
        """Ejemplos explícitos del contrato: reportes de ventas, anuncios del menú y catálogo."""
        explicitas = (f"{API}/reports/sales", f"{API}/menu/promotions", f"{API}/products")
        for esperada in explicitas:
            with self.subTest(ruta=esperada):
                encontradas = [r for r in self.rutas if r.path == esperada]
                self.assertTrue(encontradas, f"{esperada} ya no existe: ajustar la prueba")
                for r in encontradas:
                    self.assertEqual(_modulos_de(r), set())


if __name__ == "__main__":
    unittest.main()
