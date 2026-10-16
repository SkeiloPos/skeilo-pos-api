"""`name` real en la sesión (spec 122, FR-010, contracts/session-name-claim.md).

`POST /auth/login` y `GET /auth/refresh-token` firman `user_data` dentro del
JWT y (solo login) lo devuelven también en el body -- ambos deben llevar el
`User.name` real, no derivarlo del correo (eso es responsabilidad exclusiva
del frontend cuando el claim falta, research.md §1).

    python -m unittest app.characterization_tests.test_auth_session_name_claim -v
"""
import asyncio
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app.api.v1.auth import routes as auth_routes
from app.api.v1.auth.schemas import LoginRequest
from app.characterization_tests import auth_fixtures as af
from app.core.utils import decode_token

EMAIL = "maria.perez@acme.com"
PASSWORD = "Negocio123!"
NOMBRE_REAL = "María Pérez"


class _FakeRequest:
    def __init__(self, tenant_host: str | None = "acme"):
        self.headers = {"x-tenant-host": tenant_host} if tenant_host is not None else {}


class SessionNameClaimTests(unittest.TestCase):
    def setUp(self):
        self.db = af.new_session()
        self.tenant = af.make_tenant(self.db, host="acme", name="Acme")
        self.role = af.make_role(self.db, name="ADMIN")
        self.user = af.make_user(
            self.db, self.tenant, self.role,
            password=PASSWORD, email=EMAIL, name=NOMBRE_REAL,
        )
        self.db.commit()

        @contextmanager
        def fake_with_db(schema):
            yield self.db

        patcher = patch("app.api.v1.auth.routes.with_db", fake_with_db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _login(self):
        return asyncio.run(
            auth_routes.login(LoginRequest(email=EMAIL, password=PASSWORD), _FakeRequest())
        )

    def test_login_devuelve_name_real_en_el_body(self):
        resp = self._login()
        body = json.loads(resp.body)
        self.assertEqual(body["user"]["name"], NOMBRE_REAL)

    def test_login_firma_name_real_en_los_claims_del_access_token(self):
        resp = self._login()
        body = json.loads(resp.body)
        claims = decode_token(body["access_token"])
        self.assertEqual(claims["user"]["name"], NOMBRE_REAL)

    def test_login_firma_name_real_en_los_claims_del_refresh_token(self):
        resp = self._login()
        body = json.loads(resp.body)
        claims = decode_token(body["refresh_token"])
        self.assertEqual(claims["user"]["name"], NOMBRE_REAL)

    def test_refresh_token_firma_name_real_en_el_nuevo_access_token(self):
        token_details = {"user": {"uid": str(self.user.id)}}
        resp = asyncio.run(
            auth_routes.get_new_access_token(token_details=token_details, db=self.db)
        )
        body = json.loads(resp.body)
        claims = decode_token(body["access_token"])
        self.assertEqual(claims["user"]["name"], NOMBRE_REAL)


if __name__ == "__main__":
    unittest.main()
