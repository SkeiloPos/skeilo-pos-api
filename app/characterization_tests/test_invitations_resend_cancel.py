"""POST /invitations/{id}/resend y /cancel (spec 037, US4).

Tests de la nueva funcionalidad. Ejercita `resend_invitation`/
`cancel_invitation` directamente (sin `TestClient`).

    python -m unittest app.characterization_tests.test_invitations_resend_cancel -v
"""
import asyncio
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import HTTPException

from app.characterization_tests import auth_fixtures as af
from app.api.v1.auth import routes as auth_routes
from app.api.v1.auth.schemas import LoginRequest
from app.api.v1.invitations.router import cancel_invitation, resend_invitation


class _FakeRequest:
    def __init__(self, tenant_host):
        self.headers = {"x-tenant-host": tenant_host} if tenant_host else {}


class ResendCancelInvitationTests(unittest.TestCase):
    def setUp(self):
        self.db = af.new_session()
        self.tenant = af.make_tenant(self.db)
        self.role = af.make_role(self.db, name="CASHIER")
        self.admin = af.make_user(self.db, tenant=self.tenant, role=self.role)
        self.invitation = af.make_invitation(
            self.db, self.tenant, role=self.role, email="invitado@acme.com",
            password="Original123!",
        )
        self.db.commit()

    def _login(self, email, password, tenant=None):
        db = self.db
        host = (tenant or self.tenant).host

        @contextmanager
        def fake_with_db(schema):
            yield db

        with patch("app.api.v1.auth.routes.with_db", fake_with_db):
            return asyncio.run(
                auth_routes.login(
                    LoginRequest(email=email, password=password),
                    _FakeRequest(host),
                )
            )

    def _resend(self, mock_ok=True):
        with patch("app.api.v1.invitations.router.send_email") as mock_send:
            if not mock_ok:
                mock_send.side_effect = RuntimeError("boom")
            return resend_invitation(
                self.invitation.id, tenant=self.tenant, admin=self.admin, db=self.db,
            )

    # ---------------------------------------------------------- Acceptance Scenarios 1-2 de US4

    def test_reenvio_exitoso_cambia_password_y_sent_at_no_id_email_status(self):
        original_id, original_email, original_status = (
            self.invitation.id, self.invitation.email, self.invitation.status,
        )
        original_hash, original_sent_at = self.invitation.password_hash, self.invitation.sent_at

        resp = self._resend()

        self.assertEqual(resp.id, original_id)
        self.assertEqual(resp.email, original_email)
        self.db.refresh(self.invitation)
        self.assertEqual(self.invitation.status, original_status)
        self.assertNotEqual(self.invitation.password_hash, original_hash)
        self.assertNotEqual(self.invitation.sent_at, original_sent_at)

    def test_login_con_contrasena_nueva_funciona_con_la_anterior_ya_no(self):
        with patch("app.api.v1.invitations.router.send_email"), patch(
            "app.api.v1.invitations.router.generate_random_password",
            return_value="Nueva456!",
        ):
            resend_invitation(self.invitation.id, tenant=self.tenant, admin=self.admin, db=self.db)

        with self.assertRaises(HTTPException) as ctx:
            self._login("invitado@acme.com", "Original123!")
        self.assertEqual(ctx.exception.status_code, 401)

        resp = self._login("invitado@acme.com", "Nueva456!")
        self.assertEqual(resp.status_code, 200)

    # ---------------------------------------------------------- FR-012 / research.md Decisión 10

    def test_fallo_de_envio_en_reenvio_no_pierde_la_contrasena_anterior(self):
        original_hash = self.invitation.password_hash

        with self.assertRaises(HTTPException) as ctx:
            self._resend(mock_ok=False)
        self.assertEqual(ctx.exception.status_code, 502)

        self.db.refresh(self.invitation)
        self.assertEqual(self.invitation.password_hash, original_hash)

        # La contraseña anterior (previa a este intento fallido) sigue sirviendo.
        resp = self._login("invitado@acme.com", "Original123!")
        self.assertEqual(resp.status_code, 200)

    # ---------------------------------------------------------- spec 091 (A-100): el nombre al reenviar

    def _resend_capturing_body(self):
        with patch("app.api.v1.invitations.router.send_email"), patch(
            "app.api.v1.invitations.router.create_message"
        ) as mock_message:
            resp = resend_invitation(
                self.invitation.id, tenant=self.tenant, admin=self.admin, db=self.db,
            )
        return resp, mock_message.call_args.args[2]

    def test_reenviar_conserva_el_nombre_y_el_correo_saluda_por_el(self):
        # spec 096 (FR-059, D11): el saludo se integra en el párrafo,
        # "Hola, {nombre}." en vez de "Hola, {nombre}:" en línea propia.
        self.invitation.name = "María Pérez"
        self.db.commit()

        resp, body = self._resend_capturing_body()

        self.assertIn("Hola, María Pérez.", body)
        self.assertEqual(resp.name, "María Pérez")
        self.db.refresh(self.invitation)
        self.assertEqual(self.invitation.name, "María Pérez")

    def test_reenviar_una_invitacion_anterior_sin_nombre_saluda_hola(self):
        self.assertIsNone(self.invitation.name)

        resp, body = self._resend_capturing_body()

        self.assertIn("Hola.", body)
        self.assertNotIn("Hola, ", body)
        self.assertIsNone(resp.name)

    # ---------------------------------------------------------- Acceptance Scenario 3 de US4

    def test_cancelar_invalida_la_contrasena_y_deja_de_estar_pendiente(self):
        resp = cancel_invitation(self.invitation.id, admin=self.admin, db=self.db)

        self.assertEqual(resp.status, "cancelled")

        with self.assertRaises(HTTPException) as ctx:
            self._login("invitado@acme.com", "Original123!")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_cancelar_una_invitacion_ya_consumida_es_409(self):
        self._login("invitado@acme.com", "Original123!")  # la consume

        with self.assertRaises(HTTPException) as ctx:
            cancel_invitation(self.invitation.id, admin=self.admin, db=self.db)
        self.assertEqual(ctx.exception.status_code, 409)

    # ---------------------------------------------------------- research.md Decisión 7/9 (carrera)

    def test_cancelar_una_ya_cancelada_es_409(self):
        cancel_invitation(self.invitation.id, admin=self.admin, db=self.db)

        with self.assertRaises(HTTPException) as ctx:
            cancel_invitation(self.invitation.id, admin=self.admin, db=self.db)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_login_gana_la_carrera_y_el_cancel_posterior_ve_409(self):
        """Si el login se compromete primero, el cancel que llega después ya
        no encuentra la invitación 'pending' (research.md Decisión 7: "si el
        login adquiere el lock primero, el cancel espera y luego actúa sobre
        una invitación que ya quedó consumed")."""
        self._login("invitado@acme.com", "Original123!")

        with self.assertRaises(HTTPException) as ctx:
            cancel_invitation(self.invitation.id, admin=self.admin, db=self.db)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_cancel_gana_la_carrera_y_el_login_posterior_ve_401(self):
        """Si la cancelación se compromete primero, el login posterior con
        esa contraseña temporal falla — "la cancelación gana" (Edge Case)."""
        cancel_invitation(self.invitation.id, admin=self.admin, db=self.db)

        with self.assertRaises(HTTPException) as ctx:
            self._login("invitado@acme.com", "Original123!")
        self.assertEqual(ctx.exception.status_code, 401)

    # ---------------------------------------------------------- FR-013 (aislamiento por tenant)

    def test_reenviar_invitacion_de_otro_tenant_es_404(self):
        other_tenant = af.make_tenant(self.db)
        other_admin = af.make_user(self.db, tenant=other_tenant, role=self.role)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            resend_invitation(
                self.invitation.id, tenant=other_tenant, admin=other_admin, db=self.db,
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_cancelar_invitacion_de_otro_tenant_es_404(self):
        other_tenant = af.make_tenant(self.db)
        other_admin = af.make_user(self.db, tenant=other_tenant, role=self.role)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            cancel_invitation(self.invitation.id, admin=other_admin, db=self.db)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
