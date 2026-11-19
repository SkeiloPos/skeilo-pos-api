"""Tests de la nueva funcionalidad — spec 161-suscripcion-pago-wompi, Historia de Usuario 6 (FR-033 a FR-035,
SC-010; contrato `subscription-api.md §§ 3 y 4`): el historial de pagos de suscripción.

- El ADMIN ve los de SU tenant: del más reciente al más antiguo, paginado, con aislamiento estricto —el
  tenant sale siempre de `x-tenant-host`, no existe parámetro de tenant—. Los demás roles reciben `403`.
- El Super Admin ve los de cualquier tenant por `/super-admin/tenants/{id}/subscription-payments`, con la
  autenticación y el sobre de error reales del panel.
- Ninguno de los dos expone la URL del link, el identificador de la transacción ni nada de la firma.

No son characterization tests — comportamiento enteramente nuevo.

    python -m unittest app.characterization_tests.test_subscription_history -v
"""
from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from starlette.testclient import TestClient

from app.api.v1.plan import subscription_service as service
from app.characterization_tests import fixtures  # noqa: F401  - variables de entorno inertes antes de importar app.main
from app.characterization_tests import plan_fixtures as fx
from app.characterization_tests import super_admin_http_fixtures as sa
from app.core import wompi
from app.core.db import get_db, get_tenant
from app.core.dependencies import get_current_user
from app.models.subscription_payment import SubscriptionPayment

BASE = datetime(2026, 12, 1, 10, 0, 0)

CAMPOS_DEL_CONTRATO = {
    "id", "status", "amount", "currency", "ciclo_facturacion", "gateway", "gateway_reference",
    "requested_by_user_id", "period_start", "period_end", "paid_at", "payment_link_expires_at",
    "created_at", "updated_at",
}


def _pago(db, tenant, plan, *, minutos=0, status="approved", user=None, tx="TX", **kw) -> SubscriptionPayment:
    """Un pago creado `minutos` después de `BASE`, para controlar el orden sin depender del reloj."""
    pid = kw.pop("id", uuid.uuid4())
    pago = SubscriptionPayment(
        id=pid, tenant_id=tenant.id, plan_id=plan.id, amount=Decimal("89900.00"), status=status,
        gateway_reference=wompi.build_reference(pid), gateway_transaction_id=f"{tx}-{pid.hex[:6]}",
        payment_link_url="https://checkout.wompi.co/p/?public-key=pub&signature%3Aintegrity=" + "f" * 64,
        payment_link_expires_at=BASE + timedelta(minutes=minutos, hours=24),
        requested_by_user_id=user.id if user else None,
        created_at=BASE + timedelta(minutes=minutos), **kw,
    )
    db.add(pago)
    db.flush()
    return pago


def _escenario(db):
    plan = fx.make_plan(db, precio_mensual=Decimal("89900.00"))
    tenant = fx.make_tenant(db, plan=plan)
    admin = fx.make_user(db, tenant, role_name="ADMIN")
    return plan, tenant, admin


# ======================================================================
# El servicio
# ======================================================================


class ListarPagosTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.plan, self.tenant, self.admin = _escenario(self.db)

    def test_orden_del_mas_reciente_al_mas_antiguo(self):
        p1 = _pago(self.db, self.tenant, self.plan, minutos=1)
        p3 = _pago(self.db, self.tenant, self.plan, minutos=3)
        p2 = _pago(self.db, self.tenant, self.plan, minutos=2)

        items, total = service.list_payments(self.db, self.tenant.id, page=1, size=20)

        self.assertEqual([p.id for p in items], [p3.id, p2.id, p1.id])
        self.assertEqual(total, 3)

    def test_un_empate_de_fecha_se_desempata_por_id_de_forma_estable(self):
        for _ in range(3):
            _pago(self.db, self.tenant, self.plan, minutos=5)

        primera, _ = service.list_payments(self.db, self.tenant.id, page=1, size=20)
        segunda, _ = service.list_payments(self.db, self.tenant.id, page=1, size=20)

        self.assertEqual([p.id for p in primera], [p.id for p in segunda])
        self.assertEqual([p.id for p in primera], sorted((p.id for p in primera), reverse=True))

    def test_paginacion(self):
        ids = [_pago(self.db, self.tenant, self.plan, minutos=m).id for m in range(1, 6)]  # 1..5

        pagina1, total = service.list_payments(self.db, self.tenant.id, page=1, size=2)
        pagina2, _ = service.list_payments(self.db, self.tenant.id, page=2, size=2)
        pagina3, _ = service.list_payments(self.db, self.tenant.id, page=3, size=2)
        pagina4, _ = service.list_payments(self.db, self.tenant.id, page=4, size=2)

        self.assertEqual(total, 5)
        self.assertEqual([p.id for p in pagina1], [ids[4], ids[3]])
        self.assertEqual([p.id for p in pagina2], [ids[2], ids[1]])
        self.assertEqual([p.id for p in pagina3], [ids[0]])
        self.assertEqual(pagina4, [])

    def test_solo_devuelve_los_del_tenant_pedido(self):
        _, otro, _ = _escenario(self.db)
        propio = _pago(self.db, self.tenant, self.plan, minutos=1)
        ajeno = _pago(self.db, otro, self.plan, minutos=2)

        items, total = service.list_payments(self.db, self.tenant.id, page=1, size=20)
        items_otro, total_otro = service.list_payments(self.db, otro.id, page=1, size=20)

        self.assertEqual(([p.id for p in items], total), ([propio.id], 1))
        self.assertEqual(([p.id for p in items_otro], total_otro), ([ajeno.id], 1))

    def test_sin_pagos_devuelve_una_lista_vacia(self):
        self.assertEqual(service.list_payments(self.db, self.tenant.id, page=1, size=20), ([], 0))

    def test_incluye_todos_los_estados(self):
        for minutos, estado in enumerate(("pending", "approved", "rejected", "expired", "voided"), start=1):
            _pago(self.db, self.tenant, self.plan, minutos=minutos, status=estado)

        items, total = service.list_payments(self.db, self.tenant.id, page=1, size=20)

        self.assertEqual(total, 5)
        self.assertEqual({p.status for p in items}, {"pending", "approved", "rejected", "expired", "voided"})


# ======================================================================
# GET /api/v1/plan/subscription/payments
# ======================================================================


def _aplicacion_real():
    # `app.main` ejecuta `create_app()` al importarse y esta llama a `initialize_database()`, que abre
    # PostgreSQL; se sustituye por una función vacía ANTES de importar (mismo motivo que
    # `test_module_not_in_plan_error.py`).
    with mock.patch("app.core.db.initialize_database", lambda *a, **k: None):
        from app.main import app
    return app


class HistorialDelAdminHttpTests(unittest.TestCase):
    URL = "/api/v1/plan/subscription/payments"

    def setUp(self):
        self.app = _aplicacion_real()
        self.db = fx.new_session(multithreaded=True)  # TestClient atiende el endpoint en otro hilo
        self.plan, self.tenant, self.admin = _escenario(self.db)
        self.addCleanup(self.app.dependency_overrides.clear)

    def _get(self, user=None, **params):
        """`params` van tal cual a la query: ninguno de sus nombres puede chocar con los de este helper
        (por eso no recibe `tenant`: el tenant lo fija la cabecera, y un `?tenant=` debe ignorarse)."""
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_tenant] = lambda: self.tenant
        self.app.dependency_overrides[get_current_user] = lambda: user or self.admin
        return TestClient(self.app, raise_server_exceptions=False).get(self.URL, params=params)

    def test_el_admin_ve_sus_pagos_con_los_campos_del_contrato(self):
        _pago(self.db, self.tenant, self.plan, minutos=1, user=self.admin,
              period_start=BASE, period_end=BASE + timedelta(days=31), paid_at=BASE + timedelta(minutes=2))
        self.db.commit()

        resp = self._get()

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual((body["total"], body["page"], body["size"]), (1, 1, 20))
        item = body["items"][0]
        self.assertEqual(set(item), CAMPOS_DEL_CONTRATO)
        self.assertEqual(item["status"], "approved")
        self.assertEqual(item["amount"], "89900.00")
        self.assertEqual(item["currency"], "COP")
        self.assertEqual(item["ciclo_facturacion"], "mensual")
        self.assertEqual(item["gateway"], "wompi")
        self.assertEqual(item["requested_by_user_id"], str(self.admin.id))
        self.assertTrue(item["period_start"].endswith("+00:00"))
        self.assertTrue(item["gateway_reference"].startswith("SKP-"))

    def test_nunca_expone_la_url_del_link_ni_la_transaccion_ni_la_firma(self):
        _pago(self.db, self.tenant, self.plan, minutos=1)
        self.db.commit()

        texto = self._get().text

        for prohibido in ("payment_link_url", "gateway_transaction_id", "checkout.wompi.co", "signature", "TX-"):
            self.assertNotIn(prohibido, texto)

    def test_orden_descendente_y_paginacion_por_http(self):
        ids = [_pago(self.db, self.tenant, self.plan, minutos=m).id for m in range(1, 5)]
        self.db.commit()

        resp = self._get(page=2, size=2)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual([i["id"] for i in resp.json()["items"]], [str(ids[1]), str(ids[0])])
        self.assertEqual((resp.json()["total"], resp.json()["page"], resp.json()["size"]), (4, 2, 2))

    def test_aislamiento_entre_tenants_no_aparece_ningun_pago_ajeno(self):
        _, otro, _ = _escenario(self.db)
        propio = _pago(self.db, self.tenant, self.plan, minutos=1)
        for m in range(2, 7):
            _pago(self.db, otro, self.plan, minutos=m)
        self.db.commit()

        body = self._get().json()

        self.assertEqual([i["id"] for i in body["items"]], [str(propio.id)])
        self.assertEqual(body["total"], 1)

    def test_no_existe_ningun_parametro_para_elegir_el_tenant(self):
        _, otro, _ = _escenario(self.db)
        _pago(self.db, otro, self.plan, minutos=1)
        self.db.commit()

        for parametro in ({"tenant_id": otro.id}, {"tenant": otro.id}, {"host": otro.host}):
            with self.subTest(parametro):
                self.assertEqual(self._get(**parametro).json()["total"], 0)

    def test_los_demas_roles_reciben_403(self):
        for rol in ("CASHIER", "MESERO", "KITCHEN"):
            with self.subTest(rol):
                user = fx.make_user(self.db, self.tenant, role_name=rol)
                self.db.commit()

                resp = self._get(user=user)

                self.assertEqual(resp.status_code, 403)
                self.assertEqual(resp.json(), {"detail": "Tenant admin access required"})

    def test_validacion_de_page_y_size(self):
        for params in ({"page": 0}, {"size": 0}, {"size": 101}, {"page": "x"}):
            with self.subTest(params):
                self.assertEqual(self._get(**params).status_code, 422)

    def test_el_endpoint_no_acepta_post(self):
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.app.dependency_overrides[get_tenant] = lambda: self.tenant
        self.app.dependency_overrides[get_current_user] = lambda: self.admin

        self.assertEqual(TestClient(self.app, raise_server_exceptions=False).post(self.URL).status_code, 405)


# ======================================================================
# GET /api/v1/super-admin/tenants/{tenant_id}/subscription-payments
# ======================================================================


class HistorialDelSuperAdminTests(unittest.TestCase):
    def setUp(self):
        self.db = sa.new_session()
        self.super_admin = sa.make_super_admin(self.db)
        self.plan, self.tenant, self.admin = _escenario(self.db)
        self.db.commit()
        self.cliente = TestClient(
            sa.build_app(self.db, token_data=sa.super_admin_token_data(self.super_admin)),
            raise_server_exceptions=False,
        )

    def _url(self, tenant_id):
        return f"/api/v1/super-admin/tenants/{tenant_id}/subscription-payments"

    def test_ve_los_pagos_de_cualquier_tenant(self):
        _, otro, _ = _escenario(self.db)
        propio = _pago(self.db, self.tenant, self.plan, minutos=1, user=self.admin)
        ajeno = _pago(self.db, otro, self.plan, minutos=2)
        self.db.commit()

        a = self.cliente.get(self._url(self.tenant.id)).json()
        b = self.cliente.get(self._url(otro.id)).json()

        self.assertEqual(([i["id"] for i in a["items"]], a["total"]), ([str(propio.id)], 1))
        self.assertEqual(([i["id"] for i in b["items"]], b["total"]), ([str(ajeno.id)], 1))
        self.assertEqual(set(a["items"][0]), CAMPOS_DEL_CONTRATO)

    def test_mismo_orden_y_paginacion_que_el_del_admin(self):
        ids = [_pago(self.db, self.tenant, self.plan, minutos=m).id for m in range(1, 5)]
        self.db.commit()

        body = self.cliente.get(self._url(self.tenant.id), params={"page": 1, "size": 3}).json()

        self.assertEqual([i["id"] for i in body["items"]], [str(ids[3]), str(ids[2]), str(ids[1])])
        self.assertEqual((body["total"], body["page"], body["size"]), (4, 1, 3))

    def test_un_tenant_sin_pagos_devuelve_la_lista_vacia(self):
        resp = self.cliente.get(self._url(self.tenant.id))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"], [])
        self.assertEqual(resp.json()["total"], 0)

    def test_un_tenant_inexistente_es_404_con_el_sobre_estandar_del_panel(self):
        resp = self.cliente.get(self._url(999_999))

        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertIs(body["success"], False)
        self.assertEqual(body["detail"], "No existe ese tenant")

    def test_tampoco_expone_la_url_del_link_ni_la_transaccion(self):
        _pago(self.db, self.tenant, self.plan, minutos=1)
        self.db.commit()

        texto = self.cliente.get(self._url(self.tenant.id)).text

        for prohibido in ("payment_link_url", "gateway_transaction_id", "checkout.wompi.co", "signature"):
            self.assertNotIn(prohibido, texto)

    def test_sin_credenciales_de_super_admin_no_responde_200(self):
        sin_token = TestClient(sa.build_app(self.db), raise_server_exceptions=False)

        self.assertIn(sin_token.get(self._url(self.tenant.id)).status_code, (401, 403))

    def test_un_usuario_de_tenant_no_es_super_admin(self):
        cliente = TestClient(
            sa.build_app(self.db, token_data={"user": {"email": self.admin.email, "is_super_admin": False, "uid": str(self.admin.id)}}),
            raise_server_exceptions=False,
        )

        self.assertIn(cliente.get(self._url(self.tenant.id)).status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
