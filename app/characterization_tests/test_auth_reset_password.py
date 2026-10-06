"""GET /auth/reset-password/validate + POST /auth/reset-password (spec 031, US1).

quickstart.md §US1 pasos 5-7 y §US3 paso 1. `send_email_task` se mockea
(research.md Decisión 10).
"""
import re
import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi import HTTPException

from app.characterization_tests import auth_fixtures as af
from app.api.v1.auth import routes as auth_routes
from app.api.v1.auth.schemas import ResetPasswordRequest
from app.core.utils import verify_password


class ResetPasswordTests(unittest.TestCase):
    def setUp(self):
        self.db = af.new_session()
        self.tenant = af.make_tenant(self.db)
        self.user = af.make_user(self.db, tenant=self.tenant, password="claveOriginal1")
        self.db.commit()

    def _issue_token(self, raw_token="token-crudo-de-prueba", **kw):
        return af.make_password_reset_token(self.db, self.user, raw_token, **kw)

    # -- Paso 5: vigente/caducado -------------------------------------------

    def test_validate_within_expiry_is_valid(self):
        self._issue_token(expiry_minutes=30)
        self.db.commit()

        resp = auth_routes.validate_reset_token("token-crudo-de-prueba", db=self.db)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('"valid":true', resp.body.decode())

    def test_validate_after_expiry_reports_expired(self):
        # Emitido hace 31 minutos con vigencia de 30 → ya caducado.
        from app.core.timezone import utc_now

        issued_31_min_ago = utc_now().replace(tzinfo=None) - timedelta(minutes=31)
        self._issue_token(issued_at=issued_31_min_ago, expiry_minutes=30)
        self.db.commit()

        resp = auth_routes.validate_reset_token("token-crudo-de-prueba", db=self.db)
        self.assertEqual(resp.status_code, 400)
        body = resp.body.decode()
        self.assertIn('"valid":false', body)
        self.assertIn('"reason":"expired"', body)

    def test_validate_unknown_token_reports_invalid_404(self):
        resp = auth_routes.validate_reset_token("token-que-no-existe", db=self.db)
        self.assertEqual(resp.status_code, 404)
        self.assertIn('"reason":"invalid"', resp.body.decode())

    # -- Paso 6/7: doble consumo, superseding, no-recorte -------------------

    def test_successful_reset_updates_state_and_login(self):
        self._issue_token()
        self.db.commit()

        with patch("app.api.v1.auth.routes.send_email_task"):
            resp = auth_routes.reset_password(
                ResetPasswordRequest(token="token-crudo-de-prueba", new_password="claveNueva22"),
                db=self.db,
            )
        self.assertEqual(resp.status_code, 200)

        self.db.refresh(self.user)
        self.assertIsNotNone(self.user.tokens_valid_after)
        self.assertFalse(self.user.must_change_password)
        self.assertFalse(verify_password("claveOriginal1", self.user.password_hash))
        self.assertTrue(verify_password("claveNueva22", self.user.password_hash))

    def test_double_confirmation_second_attempt_is_rejected_no_second_change(self):
        self._issue_token()
        self.db.commit()

        with patch("app.api.v1.auth.routes.send_email_task"):
            auth_routes.reset_password(
                ResetPasswordRequest(token="token-crudo-de-prueba", new_password="claveNueva22"),
                db=self.db,
            )

        with patch("app.api.v1.auth.routes.send_email_task"):
            with self.assertRaises(HTTPException) as ctx:
                auth_routes.reset_password(
                    ResetPasswordRequest(token="token-crudo-de-prueba", new_password="otraClaveXX"),
                    db=self.db,
                )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail["reason"], "used")

        self.db.refresh(self.user)
        self.assertTrue(verify_password("claveNueva22", self.user.password_hash))

    def test_superseded_link_reports_invalid(self):
        """Pedir un enlace nuevo invalida el anterior (FR-005)."""
        self._issue_token(raw_token="token-viejo")
        self.db.commit()

        with patch("app.api.v1.auth.routes.enforce_sliding_window", return_value=False), \
             patch("app.api.v1.auth.routes.send_email_task"):
            import asyncio
            from app.api.v1.auth.schemas import ForgotPasswordRequest

            asyncio.run(
                auth_routes.forgot_password(
                    ForgotPasswordRequest(email=self.user.email), tenant=self.tenant, db=self.db
                )
            )

        resp = auth_routes.validate_reset_token("token-viejo", db=self.db)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('"reason":"invalid"', resp.body.decode())

    def test_new_password_not_trimmed_or_altered(self):
        """FR-025/FR-026: espacios y tildes/eñes se preservan tal cual — el login
        solo funciona con la contraseña exacta."""
        self._issue_token()
        self.db.commit()
        raw_password = " ñoño1  "

        with patch("app.api.v1.auth.routes.send_email_task"):
            auth_routes.reset_password(
                ResetPasswordRequest(token="token-crudo-de-prueba", new_password=raw_password),
                db=self.db,
            )

        self.db.refresh(self.user)
        self.assertTrue(verify_password(raw_password, self.user.password_hash))
        self.assertFalse(verify_password(raw_password.strip(), self.user.password_hash))

    def test_email_change_after_issuing_invalidates_link(self):
        """FR-012: mutar user.email después de emitir un token vigente →
        .../validate devuelve reason="invalid"."""
        self._issue_token()
        self.user.email = "otro-correo@example.com"
        self.db.commit()

        resp = auth_routes.validate_reset_token("token-crudo-de-prueba", db=self.db)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('"reason":"invalid"', resp.body.decode())

    # -- US3, paso 1: correo del enlace + correo de aviso, asuntos distintos --

    def test_full_flow_dispatches_two_emails_with_distinct_subjects(self):
        """Acceptance Scenario 1 de US3: un ciclo completo pedir→guardar
        despacha dos llamadas a send_email_task — el correo del enlace (al
        pedirlo) y el correo de aviso (al guardar), con asuntos distintos."""
        import asyncio

        from app.api.v1.auth.schemas import ForgotPasswordRequest

        with patch("app.api.v1.auth.routes.enforce_sliding_window", return_value=False), \
             patch("app.api.v1.auth.routes.send_email_task") as mock_send:
            asyncio.run(
                auth_routes.forgot_password(
                    ForgotPasswordRequest(email=self.user.email), tenant=self.tenant, db=self.db
                )
            )
            raw_token = re.search(
                r"token=([^\"&]+)", mock_send.delay.call_args.kwargs["body"]
            ).group(1)

            resp = auth_routes.reset_password(
                ResetPasswordRequest(token=raw_token, new_password="claveNueva22"), db=self.db
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_send.delay.call_count, 2)
        subjects = [call.kwargs["subject"] for call in mock_send.delay.call_args_list]
        # spec 096 (FR-059, A-113): los dos asuntos pasan a los literales
        # nuevos de FR-010.
        self.assertEqual(
            subjects,
            ["Restablece tu contraseña de SkeiloPOS", "Tu contraseña de SkeiloPOS fue cambiada"],
        )

    def test_email_dispatch_failure_does_not_break_response(self):
        """FR-028: un fallo del envío no debe romper la respuesta 200."""
        self._issue_token()
        self.db.commit()

        with patch("app.api.v1.auth.routes.send_email_task") as mock_send:
            mock_send.delay.side_effect = RuntimeError("boom")
            resp = auth_routes.reset_password(
                ResetPasswordRequest(token="token-crudo-de-prueba", new_password="claveNueva22"),
                db=self.db,
            )

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
