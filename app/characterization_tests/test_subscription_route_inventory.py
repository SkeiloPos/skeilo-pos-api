"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, FR-006/FR-008 y SC-003;
contrato `blocked-access.md § 2`: CADA ruta de la aplicación está clasificada como exenta del bloqueo
por suscripción, bloqueada, o sin tenant (nunca se bloquea). Mismo patrón que
`test_module_route_inventory.py` (spec 141): recorre las rutas reales de la aplicación (`app.routes`).

El guard global protege por defecto toda ruta que resuelve tenant, así que olvidar clasificar una ruta
nueva sería inocuo en el momento... y un riesgo después: nadie decidió si debía poder usarse con el tenant
bloqueado. Esta prueba obliga a decidirlo: una ruta con un primer segmento desconocido hace fallar la suite.

    python -m unittest app.characterization_tests.test_subscription_route_inventory -v
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi.routing import APIRoute

from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.main
from app.core.db import get_tenant
from app.core.subscription_guard import EXEMPT_PREFIXES, is_exempt_path

API = "/api/v1"

#: Siguen disponibles con el tenant bloqueado (contrato § 2), por prefijo de ruta. Debe coincidir con `EXEMPT_PREFIXES`.
EXENTAS = {"/auth", "/bootstrap", "/plan", "/notifications", "/realtime", "/legal/platform-acceptance"}

#: Se rechazan con `403 subscription_blocked` cuando el tenant está bloqueado. `cart` y parte de `menu` resuelven el
#: tenant por el token del comensal (`resolve_tenant_by_id`), no por `get_tenant`; el guard las cubre igual. `legal`
#: (spec 162) es el único segmento MIXTO: `/legal/platform-acceptance` es exento y el resto se bloquea.
BLOQUEADAS = {
    "audit-logs", "cart", "cash", "categories", "inventory", "invitations", "invoices", "legal", "menu",
    "option-groups", "options", "orders", "presentations", "products", "promotions", "reports", "sales",
    "table-sessions", "tenant", "unit-measure-groups", "unit-measures", "uploads", "users", "variants",
}

#: No resuelven tenant: el guard nunca se ejecuta y nunca se bloquean (`/webhooks` lo crea la Historia 4).
SIN_TENANT = {"health", "super-admin", "webhooks"}

#: Segmentos con rutas de las dos clases (exentas y bloqueadas a la vez).
MIXTOS = {"legal"}


def _aplicacion_real():
    # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
    # PostgreSQL y migra. Se sustituye por una función vacía ANTES de importar.
    with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
        from app.main import app
    return app


def _usa(dependant, objetivo) -> bool:
    """¿Alguna dependencia (a cualquier profundidad) de la ruta es `objetivo`? `get_db` y `get_current_user`
    dependen de `get_tenant`, así que cuentan."""
    return any(d.call is objetivo or _usa(d, objetivo) for d in dependant.dependencies)


def _segmento(path: str) -> str:
    return path[len(API) + 1:].split("/")[0]


def _clase(path: str):
    """Clasificación de una ruta (`path` completo con `/api/v1`): `exenta`, `bloqueada`, `sin_tenant` o `None`."""
    sin_prefijo = path[len(API):]
    if is_exempt_path(sin_prefijo):
        return "exenta"
    segmento = _segmento(path)
    if segmento in BLOQUEADAS:
        return "bloqueada"
    if segmento in SIN_TENANT:
        return "sin_tenant"
    return None


class TestInventarioDeRutasDelBloqueo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rutas = [r for r in _aplicacion_real().routes if isinstance(r, APIRoute) and r.path.startswith(API + "/")]

    def test_las_listas_no_se_solapan_salvo_los_segmentos_mixtos_previstos(self):
        self.assertEqual(BLOQUEADAS & SIN_TENANT, set())
        primeros_segmentos_exentos = {p.lstrip("/").split("/")[0] for p in EXENTAS}
        self.assertEqual(primeros_segmentos_exentos & SIN_TENANT, set())
        self.assertEqual(primeros_segmentos_exentos & BLOQUEADAS, MIXTOS)

    def test_las_exentas_son_exactamente_los_prefijos_del_guard(self):
        self.assertEqual(set(EXEMPT_PREFIXES), EXENTAS)

    def test_cada_prefijo_exento_corresponde_a_alguna_ruta_real(self):
        """Un prefijo mal escrito (o de una ruta que ya no existe) dejaría una exención fantasma. Si el SEGMENTO del
        prefijo aún no existe en la aplicación (p. ej. `/legal` antes de integrar la spec 162), no hay nada que
        comprobar; en cuanto existe, el prefijo completo debe coincidir con alguna ruta."""
        segmentos = {_segmento(r.path) for r in self.rutas}
        for prefijo in EXENTAS:
            if prefijo.lstrip("/").split("/")[0] not in segmentos:
                continue
            with self.subTest(prefijo):
                self.assertTrue(
                    any(r.path[len(API):] == prefijo or r.path[len(API):].startswith(prefijo + "/") for r in self.rutas),
                    f"ninguna ruta real cae bajo el prefijo exento {prefijo}",
                )

    def test_toda_ruta_esta_clasificada(self):
        for r in self.rutas:
            with self.subTest(ruta=r.path, metodos=sorted(r.methods)):
                self.assertIsNotNone(
                    _clase(r.path),
                    f"{sorted(r.methods)} {r.path}: el segmento «{_segmento(r.path)}» no está clasificado. Decide si "
                    "debe seguir disponible con el tenant bloqueado (EXEMPT_PREFIXES en subscription_guard.py y "
                    "EXENTAS aquí) o si se bloquea (BLOQUEADAS), y actualiza contracts/blocked-access.md § 2.",
                )

    def test_las_rutas_sin_tenant_no_dependen_de_get_tenant(self):
        for r in self.rutas:
            if _clase(r.path) == "sin_tenant":
                with self.subTest(ruta=r.path, metodos=sorted(r.methods)):
                    self.assertFalse(
                        _usa(r.dependant, get_tenant),
                        f"{r.path} resuelve tenant: ya no puede estar en SIN_TENANT, quedaría sujeta al bloqueo.",
                    )

    def test_toda_ruta_que_resuelve_tenant_esta_en_exentas_o_bloqueadas(self):
        for r in self.rutas:
            if _usa(r.dependant, get_tenant):
                with self.subTest(ruta=r.path, metodos=sorted(r.methods)):
                    self.assertIn(_clase(r.path), ("exenta", "bloqueada"))

    def test_un_segmento_solo_es_mixto_si_esta_declarado_como_tal(self):
        """Un segmento con rutas exentas y bloqueadas a la vez debe estar en MIXTOS: es una decisión consciente."""
        for seg in {_segmento(r.path) for r in self.rutas}:
            clases = {_clase(r.path) for r in self.rutas if _segmento(r.path) == seg}
            if {"exenta", "bloqueada"} <= clases:
                with self.subTest(seg):
                    self.assertIn(seg, MIXTOS)

    def test_en_legal_solo_la_aceptacion_de_plataforma_es_exenta(self):
        for r in self.rutas:
            if _segmento(r.path) == "legal":
                with self.subTest(ruta=r.path, metodos=sorted(r.methods)):
                    esperado = "exenta" if r.path.startswith(f"{API}/legal/platform-acceptance") else "bloqueada"
                    self.assertEqual(_clase(r.path), esperado)

    def test_el_login_no_resuelve_tenant_por_get_tenant(self):
        """FR-010: el inicio de sesión nunca se bloquea, ni siquiera por un cambio futuro de exenciones."""
        login = [r for r in self.rutas if r.path == f"{API}/auth/login"]
        self.assertEqual(len(login), 1)
        self.assertFalse(_usa(login[0].dependant, get_tenant))

    def test_el_stream_del_staff_no_resuelve_tenant_y_el_ticket_si_es_exento(self):
        stream = [r for r in self.rutas if r.path == f"{API}/realtime/stream"]
        ticket = [r for r in self.rutas if r.path == f"{API}/realtime/ticket"]
        self.assertEqual((len(stream), len(ticket)), (1, 1))
        self.assertFalse(_usa(stream[0].dependant, get_tenant))  # staff por ticket; el comensal pasa por resolve_tenant_by_id
        self.assertTrue(_usa(ticket[0].dependant, get_tenant))
        self.assertTrue(is_exempt_path("/realtime/ticket"))


if __name__ == "__main__":
    unittest.main()
