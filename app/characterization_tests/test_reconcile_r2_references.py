"""Tests de la nueva funcionalidad — spec 088-integridad-referencias-r2 (A-92), US6:
`app/scripts/reconcile_r2_references.py` (contracts/scripts.md §1, research D14).

R2 se simula con un doble que **falla ante cualquier método que no sea `list_objects_v2` o
`head_object`** (cualquier escritura o borrado), y `with_db` entrega sesiones SQLite en memoria
(una por esquema + la global con `shared.tenants`). Se verifica además que el script nunca hace
`commit` y que la base de datos queda idéntica.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_reconcile_r2_references -v
"""
import csv
import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

from botocore.exceptions import ClientError
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.characterization_tests import fixtures  # noqa: F401  - fija el entorno de Settings
from app.core.models import Base, Tenant
import app.models  # noqa: F401
from app.models.order_payment_attempt import OrderPaymentAttempt
from app.models.payment import PaymentMethod
from app.models.payment_method_catalog import PaymentMethodCatalog
from app.models.product import Product
from app.scripts import reconcile_r2_references as rec

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)
RECENT = NOW - timedelta(hours=2)
CATALOG_ID = uuid.uuid4()


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"


class FakeR2:
    """Solo admite `list_objects_v2` (paginado de a 2) y `head_object`."""

    def __init__(self, objects: dict[str, datetime]):
        self.objects = dict(objects)
        self.calls: list[str] = []

    def list_objects_v2(self, Bucket, Prefix=None, ContinuationToken=None):
        self.calls.append(f"list:{Prefix}")
        keys = sorted(k for k in self.objects if not Prefix or k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 2]
        response = {"Contents": [{"Key": k, "LastModified": self.objects[k]} for k in page]}
        if start + 2 < len(keys):
            response.update(IsTruncated=True, NextContinuationToken=str(start + 2))
        return response

    def head_object(self, Bucket, Key):
        self.calls.append(f"head:{Key}")
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}

    def __getattr__(self, name):
        raise AssertionError(f"el reporte de solo lectura llamó a R2 con '{name}'")


def _new_session(*, tenants=None) -> Session:
    wanted = {"products", "payment_methods", "order_payment_attempts", "tenants", "payment_method_catalog"}
    tables = [t for t in Base.metadata.tables.values() if t.name in wanted]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None})
    conn.execute(text("ATTACH DATABASE ':memory:' AS shared"))
    Base.metadata.create_all(bind=conn, tables=tables)
    conn.commit()
    db = Session(bind=conn, autoflush=False)
    db.add(PaymentMethodCatalog(
        id=CATALOG_ID, name="Nequi", type="transfer", active=True,
        fields=[{"key": "celular", "format": "numeric"}, {"key": "qr", "format": "image"}],
    ))
    for i, (schema, logo) in enumerate(tenants or [], start=1):
        db.add(Tenant(id=i, name=f"n-{schema}", schema=schema, host=f"{schema}.local",
                      plan_id=uuid.uuid4(), logo_url=logo))
    db.commit()
    return db


def _product(db, image):
    db.add(Product(
        id=uuid.uuid4(), category_id=uuid.uuid4(), name=f"p-{uuid.uuid4()}",
        code=f"c-{uuid.uuid4()}", image_url=image,
    ))
    db.commit()


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.shared = _new_session(tenants=[("acme", "acme/logo/logo.png"), ("globex", None)])
        self.acme = _new_session()
        self.globex = _new_session()
        self.sessions = {None: self.shared, "acme": self.acme, "globex": self.globex}

        _product(self.acme, "acme/products/existe.png")
        _product(self.acme, "acme/products/faltante.png")            # referencia sin archivo
        _product(self.acme, "legacy/img/foto.jpg")                   # fuera de convención (también sin archivo)
        _product(self.acme, "https://cdn.otro.com/foto.jpg")         # otro origen
        self.acme.add(PaymentMethod(
            id=uuid.uuid4(), name="Nequi", type="transfer", is_cash=False, active=True, catalog_id=CATALOG_ID,
            payment_info={"celular": "3001234567", "qr": "acme/payment-methods/qr.png"},
        ))
        self.acme.add(OrderPaymentAttempt(
            id=uuid.uuid4(), order_id=uuid.uuid4(), payment_method_id=uuid.uuid4(), status="pendiente",
            receipt_file_url="https://example.invalid/acme/comprobantes/hist.jpg", created_at=datetime.now(),
        ))
        self.acme.commit()
        _product(self.globex, "globex/products/g.png")

        self.r2 = FakeR2({
            "acme/products/existe.png": OLD,
            "acme/logo/logo.png": OLD,
            "acme/payment-methods/qr.png": OLD,
            "acme/comprobantes/hist.jpg": OLD,
            "acme/products/huerfano-viejo.jpg": OLD,                 # huérfano
            "acme/products/huerfano-reciente.jpg": RECENT,           # dentro de la ventana de gracia
            "globex/products/g.png": OLD,
            "globex/logo/suelto.png": OLD,                           # huérfano de globex
            "misterio/x.png": OLD,                                   # negocio desconocido
        })

        @contextmanager
        def fake_with_db(schema):
            yield self.sessions[schema]

        for patcher in (
            mock.patch.object(rec, "with_db", fake_with_db),
            mock.patch.object(rec, "get_r2_client", return_value=self.r2),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, **kwargs):
        out = io.StringIO()
        with redirect_stdout(out):
            code = rec.run(now=NOW, **kwargs)
        return code, out.getvalue()

    def _findings(self, **kwargs):
        return rec.reconcile(now=NOW, grace_hours=kwargs.pop("grace_hours", 24), **kwargs)

    def _kinds(self, findings):
        return {(f.negocio, f.tipo, f.key) for f in findings}

    def test_referencia_sin_archivo(self):
        findings, _, _ = self._findings(tenant=None)
        self.assertIn(("acme", rec.TIPO_SIN_ARCHIVO, "acme/products/faltante.png"), self._kinds(findings))
        self.assertNotIn(("acme", rec.TIPO_SIN_ARCHIVO, "acme/products/existe.png"), self._kinds(findings))

    def test_las_cuatro_fuentes_cuentan_como_referencia(self):
        findings, _, _ = self._findings(tenant=None)
        keys = {f.key for f in findings if f.tipo == rec.TIPO_HUERFANO}
        # logo, QR (payment_info) y comprobante histórico por URL absoluta NO son huérfanos
        for referenced in ("acme/logo/logo.png", "acme/payment-methods/qr.png",
                           "acme/comprobantes/hist.jpg", "acme/products/existe.png", "globex/products/g.png"):
            self.assertNotIn(referenced, keys)

    def test_huerfano_viejo_listado_y_reciente_no(self):
        findings, _, _ = self._findings(tenant=None)
        orphans = {(f.negocio, f.key) for f in findings if f.tipo == rec.TIPO_HUERFANO}
        self.assertIn(("acme", "acme/products/huerfano-viejo.jpg"), orphans)
        self.assertIn(("globex", "globex/logo/suelto.png"), orphans)
        self.assertNotIn(("acme", "acme/products/huerfano-reciente.jpg"), orphans)   # ventana de gracia

    def test_ventana_de_gracia_configurable(self):
        findings, _, _ = self._findings(tenant=None, grace_hours=1)
        orphans = {f.key for f in findings if f.tipo == rec.TIPO_HUERFANO}
        self.assertIn("acme/products/huerfano-reciente.jpg", orphans)

    def test_objeto_sin_negocio_conocido(self):
        findings, _, _ = self._findings(tenant=None)
        self.assertIn((rec.SIN_NEGOCIO, rec.TIPO_HUERFANO, "misterio/x.png"), self._kinds(findings))

    def test_key_historica_fuera_de_convencion_marcada(self):
        findings, _, _ = self._findings(tenant=None)
        self.assertIn(("acme", rec.TIPO_FUERA_DE_CONVENCION, "legacy/img/foto.jpg"), self._kinds(findings))

    def test_url_de_otro_origen_se_cuenta_aparte_y_no_se_verifica(self):
        findings, otro_origen, _ = self._findings(tenant=None)
        self.assertEqual(otro_origen, 1)
        self.assertFalse([f for f in findings if "cdn.otro.com" in f.key])
        self.assertNotIn("head:https://cdn.otro.com/foto.jpg", self.r2.calls)

    def test_tenant_acota_referencias_y_objetos(self):
        findings, _, negocios = self._findings(tenant="acme")
        self.assertEqual(negocios, ["acme"])
        self.assertEqual({f.negocio for f in findings} - {"acme"}, set())
        self.assertNotIn("globex/logo/suelto.png", {f.key for f in findings})
        self.assertIn("list:acme/", self.r2.calls)
        self.assertNotIn("list:None", self.r2.calls)

    def test_tenant_inexistente(self):
        with self.assertRaises(SystemExit):
            self._findings(tenant="no-existe")

    def test_un_solo_listado_paginado(self):
        self._findings(tenant=None)
        listings = [c for c in self.r2.calls if c.startswith("list:")]
        self.assertEqual(len(listings), 5)   # 9 objetos, páginas de 2: es UN recorrido paginado
        self.assertFalse([c for c in self.r2.calls if c.startswith("head:")])   # sin HEAD por referencia

    def test_output_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r2.csv")
            code, out = self._run(output=path, fmt="csv")
            self.assertEqual(code, 0)
            self.assertIn(f"Archivo escrito: {path}", out)
            with open(path, encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(list(rows[0].keys()), ["negocio", "tipo", "campo", "key", "detalle"])
        self.assertEqual(
            {r["tipo"] for r in rows},
            {rec.TIPO_SIN_ARCHIVO, rec.TIPO_HUERFANO, rec.TIPO_FUERA_DE_CONVENCION},
        )

    def test_output_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r2.json")
            code, _ = self._run(output=path, fmt="json")
            self.assertEqual(code, 0)
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertTrue(all(set(item) == {"negocio", "tipo", "campo", "key", "detalle"} for item in data))

    def test_resumen_en_pantalla(self):
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("Negocio: acme", out)
        self.assertIn("Referencias sin archivo", out)
        self.assertIn("Archivos sin referencia", out)
        self.assertIn("Resumen:", out)

    def test_codigo_de_salida_cero_con_o_sin_hallazgos(self):
        code, _ = self._run(tenant="globex", grace_hours=24 * 365)   # ventana enorme: sin huérfanos
        self.assertEqual(code, 0)

    def test_error_operativo_devuelve_distinto_de_cero(self):
        with mock.patch.object(rec, "get_r2_client", side_effect=RuntimeError("sin R2")):
            code, _ = self._run()
        self.assertEqual(code, 1)

    # -------------------------------------------------------------- solo lectura
    def test_solo_lectura_nunca_hace_commit_y_la_base_queda_identica(self):
        def snapshot():
            out = []
            for schema, db in self.sessions.items():
                db.expire_all()
                for table in ("products", "payment_methods", "order_payment_attempts"):
                    out.append((schema, table, db.execute(text(f"SELECT * FROM {table} ORDER BY 1")).all()))
                out.append((schema, "tenants", db.execute(text("SELECT * FROM shared.tenants ORDER BY 1")).all()))
            return out

        before = snapshot()
        with mock.patch.object(Session, "commit", side_effect=AssertionError("el reporte no hace commit")), \
             mock.patch.object(Session, "flush", side_effect=AssertionError("el reporte no escribe")):
            code, _ = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(snapshot(), before)

    def test_el_doble_de_r2_rechaza_cualquier_escritura(self):
        with self.assertRaises(AssertionError):
            self.r2.delete_object(Bucket="x", Key="y")
        with self.assertRaises(AssertionError):
            self.r2.put_object(Bucket="x", Key="y")

    def test_el_modulo_no_importa_delete_object_ni_put_object(self):
        self.assertFalse(hasattr(rec, "delete_object"))
        self.assertFalse(hasattr(rec, "put_object"))


if __name__ == "__main__":
    unittest.main()
