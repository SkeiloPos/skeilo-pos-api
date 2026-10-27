"""Spec 138 (FR-010, FR-016) — validación común de la zona horaria IANA.

Parte 1: la función pura `validate_iana_timezone` (regla de forma + base IANA del
sistema), contra la tabla de `data-model.md §1`. Parte 2 (T075): el modelo, el esquema de alta,
`POST /super-admin/tenants`, `tenant_create` y el script de soporte rechazan o aceptan por la misma regla.

Regla: un valor es válido si Python lo reconoce como zona real **y** tiene forma de región
(`Área/Lugar`, puede llevar más de una barra) o es exactamente `UTC`. Eso rechaza los
archivos de la máquina (`localtime`, `Factory`, `posixrules`) que `ZoneInfo` sí acepta.

No toca Postgres.

    python -m unittest app.characterization_tests.test_tenant_timezone_validation -v
"""
import contextlib
import io
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException
from psycopg import errors as pg_errors
from pydantic import ValidationError
from sqlalchemy.exc import DataError
from starlette.testclient import TestClient

import app.models  # noqa: F401  - registra `Plan` para que el mapper de `Tenant` se pueda configurar al ejecutar este módulo solo
from app.characterization_tests import super_admin_http_fixtures as hf
from app.api.v1.super_admin.schemas import TenantCreateWithUser
from app.core import db as core_db
from app.core.models import Tenant
from app.core.timezone import validate_iana_timezone
from app.scripts import set_tenant_timezone as script_zona

ZONAS_VALIDAS = [
    "America/Bogota",
    "America/Mexico_City",
    "America/Lima",
    "America/Santiago",
    "America/Argentina/Buenos_Aires",  # dos barras: también es forma de región
    "Europe/Madrid",
    "Asia/Tokyo",
    "UTC",
]

ZONAS_RECHAZADAS = [
    "Nowhere/Land",       # forma de región pero no existe
    "Bogota",             # sin región
    "america/bogota",     # distingue mayúsculas
    "UTC+5",              # desfase, no zona
    "PST",                # abreviatura
    "",
    " ",
    "../etc/passwd",      # intento de salir del directorio de zonas
    None,
    "localtime",          # archivos de la máquina que ZoneInfo acepta
    "Factory",
    "posixrules",
]


class TestValidateIanaTimezone(unittest.TestCase):
    def test_zonas_validas_se_devuelven_tal_cual(self):
        for valor in ZONAS_VALIDAS:
            with self.subTest(valor=valor):
                self.assertEqual(validate_iana_timezone(valor), valor)

    def test_zonas_invalidas_lanzan_value_error(self):
        for valor in ZONAS_RECHAZADAS:
            with self.subTest(valor=valor):
                with self.assertRaises(ValueError):
                    validate_iana_timezone(valor)

    def test_un_valor_que_no_es_texto_lanza_value_error(self):
        for valor in (5, 5.5, b"America/Bogota", ["America/Bogota"]):
            with self.subTest(valor=valor):
                with self.assertRaises(ValueError):
                    validate_iana_timezone(valor)


# ================================================================== parte 2 (T075)

def _tenant() -> Tenant:
    return Tenant(id=1, name="t", schema="t", plan_id=uuid.uuid4(), host="t.example.com")


class TestModeloTenant(unittest.TestCase):
    """(a) `Tenant.timezone` usa la regla común: antes el ORM aceptaba `localtime` y `Factory`."""

    def test_los_archivos_de_la_maquina_se_rechazan(self):
        for valor in ("localtime", "Factory", "posixrules"):
            with self.subTest(valor=valor):
                tenant = _tenant()
                with self.assertRaises(ValueError):
                    tenant.timezone = valor

    def test_una_zona_valida_se_asigna(self):
        tenant = _tenant()
        tenant.timezone = "America/Mexico_City"
        self.assertEqual(tenant.timezone, "America/Mexico_City")

    def test_un_rechazo_deja_intacto_el_valor_previo(self):
        tenant = _tenant()
        tenant.timezone = "Europe/Madrid"
        with self.assertRaises(ValueError):
            tenant.timezone = "Nowhere/Land"
        self.assertEqual(tenant.timezone, "Europe/Madrid")


def _cuerpo_de_alta(**extra) -> dict:
    return {
        "tenant_name": "Heladería Central", "schema_name": "heladeria_central", "host": "heladeria-central",
        "name": "María", "email": "maria@example.com", "plan_id": str(uuid.uuid4()),
        "ciclo_facturacion": None, **extra,
    }


class TestEsquemaDeAlta(unittest.TestCase):
    """(b) `TenantCreateWithUser.timezone` es opcional y, si viene, válido."""

    def test_sin_zona_o_con_none_es_valido_y_queda_en_none(self):
        self.assertIsNone(TenantCreateWithUser(**_cuerpo_de_alta()).timezone)
        self.assertIsNone(TenantCreateWithUser(**_cuerpo_de_alta(timezone=None)).timezone)

    def test_con_una_zona_valida_la_conserva(self):
        self.assertEqual(TenantCreateWithUser(**_cuerpo_de_alta(timezone="Asia/Tokyo")).timezone, "Asia/Tokyo")

    def test_con_una_zona_invalida_falla_la_validacion(self):
        for valor in ("Nowhere/Land", "localtime", "", "UTC+5"):
            with self.subTest(valor=valor):
                with self.assertRaises(ValidationError):
                    TenantCreateWithUser(**_cuerpo_de_alta(timezone=valor))

    def test_un_alias_heredado_pasa_el_esquema_y_lo_atrapa_la_base_de_datos(self):
        # `Asia/Calcutta` existe para Python pero no en `pg_timezone_names` de PostgreSQL 16 (Debian 13).
        self.assertEqual(
            TenantCreateWithUser(**_cuerpo_de_alta(timezone="Asia/Calcutta")).timezone, "Asia/Calcutta"
        )


class TestAltaHttp(unittest.TestCase):
    """(c) `POST /super-admin/tenants`: zona inválida => 422 con el envelope del módulo y NO crea nada."""

    def setUp(self):
        self.db = hf.new_session()
        self.admin = hf.make_super_admin(self.db)
        self.db.commit()
        self.client = TestClient(hf.build_app(self.db, token_data=hf.super_admin_token_data(self.admin)))

    def _post(self, **extra):
        with mock.patch("app.api.v1.super_admin.router.tenant_create") as crear, \
                mock.patch("app.api.v1.super_admin.router.send_email_task"):
            resp = self.client.post("/api/v1/super-admin/tenants", json=_cuerpo_de_alta(**extra))
        return resp, crear

    def test_zona_invalida_responde_422_y_no_crea_nada(self):
        resp, crear = self._post(timezone="Nowhere/Land")
        self.assertEqual(resp.status_code, 422)
        body = resp.json()
        self.assertFalse(body["success"])
        self.assertIn("Zona horaria", str(body))
        crear.assert_not_called()

    def test_zona_valida_se_entrega_a_tenant_create(self):
        resp, crear = self._post(timezone="America/Mexico_City")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(crear.call_args.kwargs["timezone"], "America/Mexico_City")

    def test_sin_zona_tenant_create_recibe_none_y_rige_el_valor_por_defecto(self):
        resp, crear = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(crear.call_args.kwargs["timezone"])


class TestTraduccionDelRechazoDeLaBase(unittest.TestCase):
    """(c, parte 2) La base de datos rechaza con SQLSTATE `22023` (disparador de la migración); `tenant_create`
    lo traduce a 422. Es la función pura, probable sin PostgreSQL."""

    def test_sqlstate_22023_se_traduce_a_422(self):
        exc = DataError("UPDATE shared.tenants ...", {}, pg_errors.InvalidParameterValue("Zona horaria inválida: 'Asia/Calcutta'"))
        error = core_db._http_error_for_timezone_rejection(exc)
        self.assertIsInstance(error, HTTPException)
        self.assertEqual(error.status_code, 422)
        self.assertIn("Zona horaria", str(error.detail))

    def test_otro_sqlstate_no_se_traduce(self):
        exc = DataError("INSERT ...", {}, pg_errors.NumericValueOutOfRange("fuera de rango"))
        self.assertIsNone(core_db._http_error_for_timezone_rejection(exc))

    def test_una_excepcion_sin_orig_no_se_traduce(self):
        self.assertIsNone(core_db._http_error_for_timezone_rejection(ValueError("x")))


class TestTenantCreate(unittest.TestCase):
    """(d) `tenant_create(timezone=...)`: con valor se asigna al `Tenant` antes del `flush`; con `None` no se
    asigna (rige el `server_default`). `tenant_create` exige PostgreSQL real; aquí se sustituyen sus bordes."""

    def _ejecutar(self, *, flush_error=None, **kwargs):
        db = mock.MagicMock()
        db.get.return_value = SimpleNamespace(id=uuid.uuid4())
        db.execute.return_value.fetchone.return_value = ("rol-admin",)
        if flush_error is not None:
            db.flush.side_effect = flush_error

        @contextlib.contextmanager
        def con_db(_schema):
            yield db

        contexto = mock.MagicMock()
        contexto.get_current_revision.return_value = "head"
        directorio = mock.MagicMock()
        directorio.get_current_head.return_value = "head"
        with mock.patch.object(core_db, "with_db", con_db), \
                mock.patch.object(core_db.MigrationContext, "configure", return_value=contexto), \
                mock.patch.object(core_db.script.ScriptDirectory, "from_config", return_value=directorio), \
                mock.patch("app.core.plan_limits.validate_billing_cycle_price"), \
                mock.patch("app.core.plan_limits.calculate_plan_vencimiento", return_value=None), \
                mock.patch.object(core_db, "get_tenant_specific_metadata"), \
                mock.patch.object(core_db, "seed_default_denominations"):
            core_db.tenant_create(
                name="Negocio", schema="negocio", host="negocio", admin_name="Ana", admin_email="ana@x.co",
                admin_password="Secreta1!", plan_id=uuid.uuid4(), ciclo_facturacion=None, **kwargs,
            )
        return db

    @staticmethod
    def _tenant_agregado(db) -> Tenant:
        return next(c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], Tenant))

    def test_con_zona_se_asigna_al_tenant(self):
        db = self._ejecutar(timezone="America/Mexico_City")
        self.assertEqual(self._tenant_agregado(db).timezone, "America/Mexico_City")

    def test_sin_zona_no_se_asigna_y_rige_el_valor_por_defecto_de_la_base(self):
        db = self._ejecutar()
        self.assertIsNone(self._tenant_agregado(db).timezone)  # sin asignar: el `server_default` la fija al insertar

    def test_con_none_tampoco_se_asigna(self):
        db = self._ejecutar(timezone=None)
        self.assertIsNone(self._tenant_agregado(db).timezone)

    def test_el_rechazo_de_la_base_por_zona_responde_422_y_hace_rollback(self):
        error = DataError("INSERT ...", {}, pg_errors.InvalidParameterValue("Zona horaria inválida: 'Asia/Calcutta'"))
        with self.assertRaises(HTTPException) as capturado:
            self._ejecutar(flush_error=error, timezone="Asia/Calcutta")
        self.assertEqual(capturado.exception.status_code, 422)

    def test_cualquier_otro_error_de_datos_sigue_siendo_500(self):
        error = DataError("INSERT ...", {}, pg_errors.NumericValueOutOfRange("fuera de rango"))
        with self.assertRaises(HTTPException) as capturado:
            self._ejecutar(flush_error=error)
        self.assertEqual(capturado.exception.status_code, 500)


class TestScriptDeSoporte(unittest.TestCase):
    """(e) `set_tenant_timezone`: un valor inválido termina en error y no persiste nada; imprime la advertencia
    de FR-016 ANTES de escribir."""

    def _con_tenant(self, db_error=None):
        tenant = _tenant()
        tenant.timezone = "America/Bogota"
        db = mock.MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = tenant
        if db_error is not None:
            db.commit.side_effect = db_error

        @contextlib.contextmanager
        def con_db(_schema):
            yield db

        return tenant, db, mock.patch.object(script_zona, "with_db", con_db)

    def test_un_valor_invalido_falla_y_no_persiste_nada(self):
        tenant, db, parche = self._con_tenant()
        with parche, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            script_zona.set_tenant_timezone("negocio", "Nowhere/Land")
        db.commit.assert_not_called()
        self.assertEqual(tenant.timezone, "America/Bogota")

    def test_los_archivos_de_la_maquina_tambien_se_rechazan(self):
        for valor in ("localtime", "Factory"):
            with self.subTest(valor=valor):
                _, db, parche = self._con_tenant()
                with parche, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
                    script_zona.set_tenant_timezone("negocio", valor)
                db.commit.assert_not_called()

    def test_imprime_la_advertencia_antes_de_escribir(self):
        tenant, db, parche = self._con_tenant()
        salida = io.StringIO()
        vista_al_confirmar = []
        db.commit.side_effect = lambda: vista_al_confirmar.append(salida.getvalue())
        with parche, contextlib.redirect_stdout(salida):
            script_zona.set_tenant_timezone("negocio", "America/Mexico_City")
        self.assertEqual(tenant.timezone, "America/Mexico_City")
        self.assertEqual(len(vista_al_confirmar), 1)
        antes_de_escribir = vista_al_confirmar[0]
        self.assertIn("America/Bogota", antes_de_escribir)       # zona anterior
        self.assertIn("America/Mexico_City", antes_de_escribir)  # zona nueva
        self.assertIn("los instantes guardados no cambian", antes_de_escribir.lower())
        self.assertIn("historial", antes_de_escribir.lower())

    def test_si_la_base_rechaza_el_valor_informa_el_mensaje_del_motor(self):
        error = DataError("UPDATE shared.tenants ...", {}, pg_errors.InvalidParameterValue("Zona horaria inválida: 'Asia/Calcutta'"))
        _, db, parche = self._con_tenant(db_error=error)
        with parche, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as capturado:
            script_zona.set_tenant_timezone("negocio", "Asia/Calcutta")
        self.assertIn("Zona horaria inválida", str(capturado.exception))
        db.rollback.assert_called()


if __name__ == "__main__":
    unittest.main()

