"""Cierre de sesiones (spec 031, FR-009/FR-017/SC-005).

Verifica que el chequeo nuevo de `tokens_valid_after` en
`get_current_user`/`get_authenticated_user`/`GET /auth/refresh-token` no
altera el comportamiento ya protegido de RN-AUTH-07/A-23 (cuenta desactivada
→ 401), y que revoca correctamente tokens acuñados antes de un cambio de
contraseña exitoso — por cualquiera de los dos flujos.
"""
import asyncio
import unittest
from datetime import timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException

from app.characterization_tests import auth_fixtures as af
from app.api.v1.auth import routes as auth_routes
from app.api.v1.auth.schemas import ChangePasswordRequest
from app.core.dependencies import get_authenticated_user, get_current_user
from app.core.timezone import utc_now
from app.core.utils import create_access_token, decode_token


class AuthSessionRevocationTests(unittest.TestCase):
    def setUp(self):
        self.db = af.new_session()
        self.tenant = af.make_tenant(self.db)
        self.user = af.make_user(self.db, tenant=self.tenant, password="claveOriginal1")
        self.db.commit()

    def _claims(self, refresh: bool = False) -> dict:
        user_data = {
            "email": self.user.email,
            "uid": str(self.user.id),
            "tenant_id": self.tenant.id,
            "is_super_admin": False,
            "role": self.user.role_name,
            "must_change_password": self.user.must_change_password,
        }
        token = create_access_token(user_data, refresh=refresh)
        return decode_token(token)

    # -- Paso 1: no-regresión A-23 (protegida) --------------------------------

    def test_a23_inactive_account_refresh_still_401_unchanged(self):
        """cuenta active=False, tokens_valid_after=None → 401 "User not found
        or inactive", idéntico al comportamiento ya protegido — debe seguir en
        verde antes de que ninguna historia empiece a fijar tokens_valid_after."""
        self.user.active = False
        self.db.commit()
        claims = self._claims(refresh=True)

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(auth_routes.get_new_access_token(token_details=claims, db=self.db))

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "User not found or inactive")

    # -- Paso 2/3: revocado por cambio de contraseña --------------------------

    def test_access_token_before_cutover_is_revoked(self):
        claims = self._claims()
        self.user.tokens_valid_after = utc_now().replace(tzinfo=None) + timedelta(seconds=2)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            get_current_user(token_data=claims, db=self.db, tenant=self.tenant)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertNotIn(ctx.exception.detail, ("Token has been revoked", "User not found or inactive"))

        with self.assertRaises(HTTPException) as ctx2:
            get_authenticated_user(token_data=claims, db=self.db)
        self.assertEqual(ctx2.exception.status_code, 401)

    def test_refresh_token_before_cutover_is_revoked(self):
        claims = self._claims(refresh=True)
        self.user.tokens_valid_after = utc_now().replace(tzinfo=None) + timedelta(seconds=2)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(auth_routes.get_new_access_token(token_details=claims, db=self.db))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_access_token_after_cutover_is_accepted(self):
        self.user.tokens_valid_after = utc_now().replace(tzinfo=None) - timedelta(seconds=5)
        self.db.commit()
        claims = self._claims()  # emitido después del corte

        user = get_current_user(token_data=claims, db=self.db, tenant=self.tenant)
        self.assertEqual(user.id, self.user.id)

    def test_same_wall_clock_second_as_cutover_is_not_rejected(self):
        """Regresión: PyJWT trunca `iat` a segundo entero. Si el corte y el
        reemisión caen en el mismo segundo de reloj, un `tokens_valid_after`
        con fracción de segundo sin truncar rechazaría el token nuevo por error
        — exactamente la sesión de origen que FR-017 exige preservar en el
        Flujo B."""
        now = utc_now().replace(tzinfo=None)
        self.user.tokens_valid_after = now.replace(microsecond=now.microsecond)
        self.db.commit()
        # Token con el mismo `iat` (segundo) que tokens_valid_after, pero
        # microsegundos antes en el reloj real — simulable fijando `iat` a mano.
        claims = self._claims()
        # `now` es un naive en UTC: `now.timestamp()` lo leería como hora LOCAL de la máquina (con
        # `TZ=Asia/Tokyo` daba un `iat` 9 h en el pasado). Se fija su zona UTC antes de convertir (spec 138, FR-012).
        claims["iat"] = int(now.replace(tzinfo=timezone.utc).timestamp())

        user = get_current_user(token_data=claims, db=self.db, tenant=self.tenant)
        self.assertEqual(user.id, self.user.id)

    # -- Paso 4: Flujo B completo — la sesión de origen sobrevive -------------

    def test_change_password_flow_origin_session_survives_via_relogin(self):
        old_access_claims = self._claims()
        # PyJWT trunca `iat` a segundo entero (confirmado arriba): en un test
        # rápido sobre SQLite en memoria, el login original y el cambio de
        # contraseña pueden caer en el mismo segundo de reloj real, dejando
        # `iat == cutover` (no "antes"). En producción siempre hay al menos un
        # login y una escritura de contraseña real de por medio — se simula
        # ese margen mínimo restando un segundo, en vez de depender de la
        # suerte del reloj de la máquina que corre el test.
        old_access_claims["iat"] -= 1

        with patch("app.api.v1.auth.routes.send_email_task"):
            auth_routes.change_password(
                ChangePasswordRequest(current_password="claveOriginal1", new_password="claveNueva99"),
                user=self.user,
                db=self.db,
            )

        # Mismo patrón que AuthService.changePassword(): re-login inmediato con
        # la contraseña nueva para obtener tokens frescos.
        new_access_claims = self._claims()

        user_again = get_current_user(token_data=new_access_claims, db=self.db, tenant=self.tenant)
        self.assertEqual(user_again.id, self.user.id)

        with self.assertRaises(HTTPException):
            get_current_user(token_data=old_access_claims, db=self.db, tenant=self.tenant)

        # También el refresh del login original queda cerrado (SC-005).
        old_refresh_claims = old_access_claims.copy()
        old_refresh_claims["refresh"] = True
        with self.assertRaises(HTTPException):
            asyncio.run(auth_routes.get_new_access_token(token_details=old_refresh_claims, db=self.db))


if __name__ == "__main__":
    unittest.main()
