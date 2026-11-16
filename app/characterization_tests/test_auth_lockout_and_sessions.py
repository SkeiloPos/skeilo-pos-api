"""Tests de la nueva funcionalidad — spec 159, User Story 6: supervisar
accesos, intentos fallidos y sesiones (research.md Decisiones 4/5).

No son characterization tests: el bloqueo automático por intentos fallidos y
el registro/revocación de sesiones son comportamiento nuevo de esta spec.
`test_auth_session_revocation.py`/`test_auth_session_name_claim.py` siguen
protegidos y sin modificar -- ver ese módulo para la no-regresión de
`tokens_valid_after`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_auth_lockout_and_sessions -v
"""
import asyncio
import unittest
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.characterization_tests import plan_fixtures as fx
from app.characterization_tests import super_admin_http_fixtures as sa_fx
from app.api.v1.auth import routes as auth_routes
from app.api.v1.auth.schemas import LoginRequest
from app.api.v1.super_admin import users_router
from app.core.timezone import utc_now_naive
from app.core.utils import create_access_token, decode_token, generate_passwd_hash
from app.models.admin_audit_log import AdminAuditLog
from app.models.user_session import UserSession


class _FakeRequest:
    def __init__(self, headers: dict | None = None, ip: str | None = "9.9.9.9"):
        self.headers = headers or {}
        self.client = SimpleNamespace(host=ip) if ip is not None else None


class _LockoutTestCase(unittest.TestCase):
    """`with_db` apunta a la sesión SQLite del test (mismo patrón que
    `test_auth_session_name_claim`). `enforce_sliding_window` se mockea con
    la secuencia de booleans que produciría la ventana real (mismo criterio
    que `test_auth_forgot_password.py::test_rate_limit_blocks_...`): un
    proceso que hace varios `asyncio.run()` seguidos no puede reutilizar la
    misma conexión Redis async entre llamadas (queda atada al loop ya
    cerrado), así que ejercitar la ventana deslizante *real* aquí no es
    viable -- `rate_limit.py` ya tiene su propia cobertura sin este router
    de por medio."""

    def setUp(self):
        self.db = fx.new_session()
        self.tenant = fx.make_tenant(self.db)
        self.password = "ClaveBuena123!"
        self.user = fx.make_user(
            self.db,
            self.tenant,
            email=f"user-{uuid.uuid4().hex[:10]}@acme.com",
            password_hash=generate_passwd_hash(self.password),
        )
        self.db.commit()

        @contextmanager
        def fake_with_db(schema):
            yield self.db

        patcher = patch("app.api.v1.auth.routes.with_db", fake_with_db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _login(self, password: str, *, blocked: bool = False):
        req = _FakeRequest(headers={"x-tenant-host": self.tenant.host})
        with patch(
            "app.api.v1.auth.routes.enforce_sliding_window",
            new=AsyncMock(return_value=blocked),
        ):
            return asyncio.run(
                auth_routes.login(LoginRequest(email=self.user.email, password=password), req)
            )


class LoginLockoutTests(_LockoutTestCase):
    def test_cruzar_el_umbral_bloquea_la_cuenta_y_registra_auditoria_de_sistema(self):
        """Mientras la ventana no está al límite, el intento fallido se
        cuenta pero no bloquea; en el intento donde `enforce_sliding_window`
        ya reporta el límite alcanzado, la cuenta pasa a `blocked_security`
        -- el 401 de ESE intento individual no cambia (research.md Decisión
        4: el bloqueo es un efecto interno, no una respuesta nueva)."""
        with self.assertRaises(HTTPException) as ctx:
            self._login("clave-incorrecta", blocked=False)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Invalid credentials")

        self.db.refresh(self.user)
        self.assertEqual(self.user.status, "active")

        with self.assertRaises(HTTPException) as ctx2:
            self._login("clave-incorrecta", blocked=True)
        self.assertEqual(ctx2.exception.status_code, 401)
        self.assertEqual(ctx2.exception.detail, "Invalid credentials")

        self.db.refresh(self.user)
        self.assertEqual(self.user.status, "blocked_security")
        self.assertFalse(self.user.active)
        self.assertIsNotNone(self.user.security_blocked_at)
        self.assertEqual(self.user.failed_login_attempts, 2)

        log = (
            self.db.query(AdminAuditLog)
            .filter_by(affected_user_id=self.user.id, action="blocked_security")
            .one()
        )
        self.assertEqual(log.actor_type, "system")
        self.assertIsNone(log.actor_user_id)

    def test_cuenta_ya_bloqueada_responde_403_inactiva_sin_rama_nueva(self):
        """Una vez bloqueada, un intento posterior (aun con la contraseña
        correcta) cae en el 403 ya protegido de cuenta inactiva -- no hay
        ningún código/mensaje nuevo para este caso (contrato sin cambios)."""
        with self.assertRaises(HTTPException):
            self._login("clave-incorrecta", blocked=True)

        with self.assertRaises(HTTPException) as ctx:
            self._login(self.password, blocked=False)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail, "User account is inactive")

    def test_login_exitoso_reinicia_el_contador_y_crea_una_sesion(self):
        resp = self._login(self.password, blocked=False)
        import json

        body = json.loads(resp.body)
        self.db.refresh(self.user)
        self.assertEqual(self.user.failed_login_attempts, 0)

        access_claims = decode_token(body["access_token"])
        refresh_claims = decode_token(body["refresh_token"])
        self.assertIsNotNone(access_claims.get("sid"))
        self.assertEqual(access_claims["sid"], refresh_claims["sid"])

        session_row = self.db.get(UserSession, uuid.UUID(access_claims["sid"]))
        self.assertIsNotNone(session_row)
        self.assertEqual(session_row.user_id, self.user.id)
        self.assertIsNone(session_row.revoked_at)

    def test_un_intento_fallido_no_bloquea_por_debajo_del_umbral(self):
        with self.assertRaises(HTTPException):
            self._login("clave-incorrecta", blocked=False)

        self.db.refresh(self.user)
        self.assertEqual(self.user.status, "active")
        self.assertTrue(self.user.active)
        self.assertEqual(self.user.failed_login_attempts, 1)

    def test_desbloqueo_manual_limpia_tambien_la_ventana_deslizante_de_redis(self):
        """Gap encontrado en la validación end-to-end (T100): el desbloqueo
        manual (`POST /activate`) debe limpiar la clave de Redis además del
        snapshot persistido -- si no, un intento fallido inmediatamente
        posterior dentro de la misma ventana vuelve a cruzar el umbral y
        re-bloquea a un usuario que el Super Admin acaba de desbloquear."""
        self.user.status = "blocked_security"
        self.user.active = False
        self.user.security_blocked_at = utc_now_naive()
        self.user.failed_login_attempts = 5
        self.db.commit()

        with patch("app.core.redis.token_blocklist.delete", new=AsyncMock()) as mock_delete:
            actor = sa_fx.make_super_admin(self.db)
            result = asyncio.run(
                users_router.activate_user(
                    self.user.id, _FakeRequest(), db=self.db, actor=actor
                )
            )

        self.assertEqual(result.status, "active")
        mock_delete.assert_awaited_once()


class RefreshAndLogoutSessionTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.tenant = fx.make_tenant(self.db)
        self.user = fx.make_user(self.db, self.tenant)
        self.db.commit()

    def _access_claims_with_sid(self, sid: str, refresh: bool = False) -> dict:
        user_data = {
            "email": self.user.email,
            "name": self.user.name,
            "uid": str(self.user.id),
            "tenant_id": self.tenant.id,
            "is_super_admin": False,
            "role": self.user.role_name,
            "must_change_password": self.user.must_change_password,
        }
        token = create_access_token(user_data, refresh=refresh, sid=sid)
        return decode_token(token)

    def test_refresh_con_sid_revocado_responde_401_token_revocado(self):
        session_row = fx.make_user_session(self.db, self.user, revoked_at=utc_now_naive())
        self.db.commit()

        claims = self._access_claims_with_sid(str(session_row.id), refresh=True)
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(auth_routes.get_new_access_token(token_details=claims, db=self.db))
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "Token has been revoked")

    def test_refresh_con_sid_activo_actualiza_last_seen_y_reemite_el_sid(self):
        session_row = fx.make_user_session(self.db, self.user)
        self.db.commit()
        old_last_seen = session_row.last_seen_at

        claims = self._access_claims_with_sid(str(session_row.id), refresh=True)
        resp = asyncio.run(auth_routes.get_new_access_token(token_details=claims, db=self.db))

        import json

        body = json.loads(resp.body)
        new_claims = decode_token(body["access_token"])
        self.assertEqual(new_claims["sid"], str(session_row.id))

        self.db.refresh(session_row)
        self.assertGreaterEqual(session_row.last_seen_at, old_last_seen)

    def test_refresh_sin_sid_sigue_aceptado_compatibilidad_retroactiva(self):
        """Tokens emitidos antes de esta spec no llevan `sid` -- deben
        seguir aceptándose igual que hoy (data-model.md § UserSession)."""
        user_data = {
            "email": self.user.email,
            "uid": str(self.user.id),
            "tenant_id": self.tenant.id,
            "is_super_admin": False,
            "role": self.user.role_name,
            "must_change_password": self.user.must_change_password,
        }
        token = create_access_token(user_data, refresh=True)
        claims = decode_token(token)
        self.assertNotIn("sid", claims)

        resp = asyncio.run(auth_routes.get_new_access_token(token_details=claims, db=self.db))
        self.assertIsNotNone(resp)

    def test_logout_marca_la_sesion_revocada(self):
        session_row = fx.make_user_session(self.db, self.user)
        self.db.commit()

        claims = self._access_claims_with_sid(str(session_row.id), refresh=False)
        claims["jti"] = str(uuid.uuid4())

        with patch(
            "app.api.v1.auth.routes.add_jti_to_blocklist", new=AsyncMock(return_value=None)
        ):
            asyncio.run(auth_routes.revoke_token(token_details=claims, db=self.db))

        self.db.refresh(session_row)
        self.assertIsNotNone(session_row.revoked_at)


class RevokeSessionsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.db = fx.new_session()
        self.tenant = fx.make_tenant(self.db)
        self.actor = sa_fx.make_super_admin(self.db)
        self.user = fx.make_user(self.db, self.tenant)
        self.db.commit()

    def test_revoke_all_marca_todas_las_sesiones_y_registra_auditoria(self):
        s1 = fx.make_user_session(self.db, self.user)
        s2 = fx.make_user_session(self.db, self.user)
        self.db.commit()

        result = users_router.revoke_all_sessions(
            self.user.id, _FakeRequest(), db=self.db, actor=self.actor
        )
        self.assertEqual(result.active_sessions, [])

        self.db.refresh(s1)
        self.db.refresh(s2)
        self.assertIsNotNone(s1.revoked_at)
        self.assertIsNotNone(s2.revoked_at)

        log = (
            self.db.query(AdminAuditLog)
            .filter_by(affected_user_id=self.user.id, action="sessions_revoked")
            .one()
        )
        self.assertEqual(log.detail["alcance"], "todas")

    def test_revoke_single_revoca_solo_esa_sesion(self):
        s1 = fx.make_user_session(self.db, self.user)
        s2 = fx.make_user_session(self.db, self.user)
        self.db.commit()

        result = users_router.revoke_single_session(
            self.user.id, s1.id, _FakeRequest(), db=self.db, actor=self.actor
        )
        self.assertEqual(len(result.active_sessions), 1)
        self.assertEqual(result.active_sessions[0].id, s2.id)

        self.db.refresh(s1)
        self.db.refresh(s2)
        self.assertIsNotNone(s1.revoked_at)
        self.assertIsNone(s2.revoked_at)

    def test_revoke_single_404_si_no_pertenece_o_ya_revocada(self):
        otro_usuario = fx.make_user(self.db, self.tenant)
        ajena = fx.make_user_session(self.db, otro_usuario)
        ya_revocada = fx.make_user_session(self.db, self.user, revoked_at=utc_now_naive())
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            users_router.revoke_single_session(
                self.user.id, ajena.id, _FakeRequest(), db=self.db, actor=self.actor
            )
        self.assertEqual(ctx.exception.status_code, 404)

        with self.assertRaises(HTTPException) as ctx2:
            users_router.revoke_single_session(
                self.user.id, ya_revocada.id, _FakeRequest(), db=self.db, actor=self.actor
            )
        self.assertEqual(ctx2.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
