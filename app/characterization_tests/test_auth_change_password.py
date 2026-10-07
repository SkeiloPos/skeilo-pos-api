"""POST /auth/change-password (spec 031, US2 + US3).

quickstart.md §US2 y §US3 paso 2. `RN-AUTH-01` (verificación de
current_password) y `RN-AUTH-02` (limpieza de must_change_password) no
cambian — solo se agregan FR-019/FR-021/FR-017/FR-022 encima.
"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.characterization_tests import auth_fixtures as af
from app.api.v1.auth import routes as auth_routes
from app.api.v1.auth.schemas import ChangePasswordRequest
from app.core.utils import verify_password


class ChangePasswordTests(unittest.TestCase):
    def setUp(self):
        self.db = af.new_session()
        self.tenant = af.make_tenant(self.db)
        self.user = af.make_user(self.db, tenant=self.tenant, password="claveActual1")
        self.db.commit()

    def test_success_sets_tokens_valid_after(self):
        self.assertIsNone(self.user.tokens_valid_after)

        with patch("app.api.v1.auth.routes.send_email_task"):
            resp = auth_routes.change_password(
                ChangePasswordRequest(current_password="claveActual1", new_password="claveNueva22"),
                user=self.user,
                db=self.db,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(self.user.tokens_valid_after)
        self.assertTrue(verify_password("claveNueva22", self.user.password_hash))

    def test_wrong_current_password_400_hash_unchanged(self):
        """RN-AUTH-01 intacto: password_hash no cambia si current_password falla."""
        original_hash = self.user.password_hash

        with self.assertRaises(HTTPException) as ctx:
            auth_routes.change_password(
                ChangePasswordRequest(current_password="clave-incorrecta", new_password="claveNueva22"),
                user=self.user,
                db=self.db,
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.user.password_hash, original_hash)
        self.assertTrue(verify_password("claveActual1", self.user.password_hash))

    def test_new_password_equal_to_current_is_rejected(self):
        """FR-021, regla nueva de esta spec — spec 001 no la tenía."""
        with self.assertRaises(HTTPException) as ctx:
            auth_routes.change_password(
                ChangePasswordRequest(current_password="claveActual1", new_password="claveActual1"),
                user=self.user,
                db=self.db,
            )

        self.assertEqual(ctx.exception.status_code, 400)

    def test_new_password_out_of_range_rejected_by_schema(self):
        """FR-019: 8-12 caracteres (antes 6-128) — el schema valida antes de
        llegar al handler, así que se prueba directamente sobre el modelo."""
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ChangePasswordRequest(current_password="claveActual1", new_password="x" * 13)

        with self.assertRaises(ValidationError):
            ChangePasswordRequest(current_password="claveActual1", new_password="x" * 7)

    def test_does_not_touch_other_profile_fields(self):
        """SC-007: el endpoint no modifica name/email/phone/role_id."""
        original_name = self.user.name
        original_email = self.user.email
        original_phone = self.user.phone
        original_role_id = self.user.role_id

        with patch("app.api.v1.auth.routes.send_email_task"):
            auth_routes.change_password(
                ChangePasswordRequest(current_password="claveActual1", new_password="claveNueva22"),
                user=self.user,
                db=self.db,
            )

        self.assertEqual(self.user.name, original_name)
        self.assertEqual(self.user.email, original_email)
        self.assertEqual(self.user.phone, original_phone)
        self.assertEqual(self.user.role_id, original_role_id)

    # -- US3, paso 2: un correo de aviso, un fallo no bloquea la respuesta ---

    def test_success_dispatches_one_notice_email(self):
        with patch("app.api.v1.auth.routes.send_email_task") as mock_send:
            resp = auth_routes.change_password(
                ChangePasswordRequest(current_password="claveActual1", new_password="claveNueva22"),
                user=self.user,
                db=self.db,
            )

        self.assertEqual(resp.status_code, 200)
        mock_send.delay.assert_called_once()
        # spec 096 (FR-059, A-113): el asunto pasa al literal nuevo de FR-010.
        self.assertEqual(
            mock_send.delay.call_args.kwargs["subject"], "Tu contraseña de SkeiloPOS fue cambiada"
        )

    def test_email_dispatch_failure_does_not_break_response(self):
        with patch("app.api.v1.auth.routes.send_email_task") as mock_send:
            mock_send.delay.side_effect = RuntimeError("boom")
            resp = auth_routes.change_password(
                ChangePasswordRequest(current_password="claveActual1", new_password="claveNueva22"),
                user=self.user,
                db=self.db,
            )

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
