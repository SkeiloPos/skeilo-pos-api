"""Tests de la nueva funcionalidad — spec 080-imagenes-key-relativa-r2, US2:
`app/scripts/migrate_image_keys_relative.py` (contracts/data-migration.md,
data-model.md §5).

`with_db` se parchea para entregar una sesión SQLite en memoria con las tres
tablas del recorrido (`shared.tenants`, `tenant.products`,
`tenant.payment_methods`), mismo patrón que
`test_migrate_payment_methods_catalog.py`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_migrate_image_keys_relative -v
"""
import unittest
import uuid
from contextlib import contextmanager
from decimal import Decimal
from unittest import mock

from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.characterization_tests import fixtures  # noqa: F401  - fija el entorno de Settings
from app.core.models import Base
import app.models  # noqa: F401
from app.core.models import Tenant
from app.models.category import Category
from app.models.product import Product
from app.models.payment import PaymentMethod
from app.scripts import migrate_image_keys_relative as mig

SCHEMA = "heladeria3"
ASSETS = "https://assets.example.invalid"
LEGACY = "https://example.invalid"


def _key(folder, name="46f1a4d1c4aa4c68ba7b32642334d084.png"):
    return f"{SCHEMA}/{folder}/{name}"


V0 = None
V1_IMG = _key("products")
V2_IMG = f"{LEGACY}/{_key('products', 'vieja.png')}"
V3_IMG = f"{ASSETS}/{_key('products', 'nueva.png')}"
V4 = "https://xyz.supabase.co/storage/v1/object/public/img/foto.jpg"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # pragma: no cover
    return "JSON"


_TENANT_TABLES = ["products", "payment_methods", "categories"]
_SHARED_TABLES = ["tenants"]


def _new_session() -> Session:
    tenant_tables = [t for t in Base.metadata.tables.values() if t.name in _TENANT_TABLES]
    shared_tables = [t for t in Base.metadata.tables.values() if t.name in _SHARED_TABLES]
    engine = create_engine("sqlite:///:memory:")
    conn = engine.connect().execution_options(schema_translate_map={"tenant": None})
    conn.execute(text("ATTACH DATABASE ':memory:' AS shared"))
    Base.metadata.create_all(bind=conn, tables=tenant_tables + shared_tables)
    conn.commit()
    return Session(bind=conn, autoflush=False)


def _seed(db):
    tenant = Tenant(
        id=1, name="Heladería 3", schema=SCHEMA, host="h3.local",
        plan_id=uuid.uuid4(), logo_url=f"{LEGACY}/{_key('logo')}",
    )
    db.add(tenant)
    cat = Category(id=uuid.uuid4(), name="Conos", active=True, display_order=0)
    db.add(cat)
    db.flush()
    products = {
        "v0": Product(id=uuid.uuid4(), category_id=cat.id, name="p-v0", code="v0", image_url=V0),
        "v1": Product(id=uuid.uuid4(), category_id=cat.id, name="p-v1", code="v1", image_url=V1_IMG),
        "v2": Product(id=uuid.uuid4(), category_id=cat.id, name="p-v2", code="v2", image_url=V2_IMG),
        "v3": Product(id=uuid.uuid4(), category_id=cat.id, name="p-v3", code="v3", image_url=V3_IMG),
        "v4": Product(id=uuid.uuid4(), category_id=cat.id, name="p-v4", code="v4", image_url=V4),
    }
    for p in products.values():
        db.add(p)
    method = PaymentMethod(
        id=uuid.uuid4(), name="Nequi", type="transfer", is_cash=False, active=True,
        is_complete=True,
        payment_info={"celular": "3001234567", "qr": f"{LEGACY}/{_key('payment-methods')}"},
    )
    db.add(method)
    db.commit()
    return tenant, products, method


@contextmanager
def _fake_with_db(_schema):
    yield _fake_with_db.session


class TestMigrateImageKeysRelative(unittest.TestCase):
    def setUp(self):
        self.db = _new_session()
        _fake_with_db.session = self.db
        p = mock.patch.object(mig, "with_db", _fake_with_db)
        p.start()
        self.addCleanup(p.stop)
        self.tenant, self.products, self.method = _seed(self.db)

    def _refresh_all(self):
        self.db.expire_all()
        return (
            self.db.get(Tenant, 1),
            {k: self.db.get(Product, v.id) for k, v in self.products.items()},
            self.db.get(PaymentMethod, self.method.id),
        )

    # -------------------------------------------------------------- G1 / SC-007
    def test_migrar_reescribe_v2_v3_a_key_en_los_tres_campos(self):
        mig.run(write=True, revert=False)
        tenant, products, method = self._refresh_all()

        self.assertEqual(tenant.logo_url, _key("logo"))
        self.assertEqual(products["v2"].image_url, _key("products", "vieja.png"))
        self.assertEqual(products["v3"].image_url, _key("products", "nueva.png"))
        self.assertEqual(method.payment_info["qr"], _key("payment-methods"))

        # ninguna fila queda con http:// en los tres campos gestionados
        rows = self.db.execute(
            text("SELECT image_url FROM products WHERE image_url LIKE 'https://%'")
        ).fetchall()
        self.assertEqual([r[0] for r in rows], [V4])  # solo el de otro origen

    # -------------------------------------------------------------- G6 / FR-010
    def test_otro_origen_intacto_en_ambos_modos(self):
        mig.run(write=True, revert=False)
        _, products, _ = self._refresh_all()
        self.assertEqual(products["v4"].image_url, V4)
        mig.run(write=True, revert=True)
        _, products, _ = self._refresh_all()
        self.assertEqual(products["v4"].image_url, V4)

    # -------------------------------------------------------------- filas ya-key / vacías
    def test_v0_y_v1_sin_cambios(self):
        mig.run(write=True, revert=False)
        _, products, _ = self._refresh_all()
        self.assertIsNone(products["v0"].image_url)
        self.assertEqual(products["v1"].image_url, V1_IMG)

    def test_claves_no_imagen_de_payment_info_intactas(self):
        mig.run(write=True, revert=False)
        _, _, method = self._refresh_all()
        self.assertEqual(method.payment_info["celular"], "3001234567")

    # -------------------------------------------------------------- G3 / SC-009
    def test_idempotente_segunda_corrida_no_cambia_nada(self):
        mig.run(write=True, revert=False)
        _, products_1, method_1 = self._refresh_all()
        snapshot = ({k: v.image_url for k, v in products_1.items()}, dict(method_1.payment_info))

        code = mig.run(write=True, revert=False)
        self.assertEqual(code, 0)
        _, products_2, method_2 = self._refresh_all()
        self.assertEqual(
            ({k: v.image_url for k, v in products_2.items()}, dict(method_2.payment_info)),
            snapshot,
        )

    def test_report_only_no_escribe(self):
        mig.run(write=False, revert=False)
        tenant, products, method = self._refresh_all()
        self.assertEqual(products["v2"].image_url, V2_IMG)  # sin cambios
        self.assertEqual(tenant.logo_url, f"{LEGACY}/{_key('logo')}")
        self.assertTrue(method.payment_info["qr"].startswith("https://"))

    # -------------------------------------------------------------- G4 / SC-008
    def test_migrar_y_revert_deja_los_campos_pre_migracion_como_antes(self):
        # Estado pre-migración real: V2 (URL vieja), V0 (vacío) y V4 (otro
        # origen) — nunca V1/V3, que solo nacen tras el despliegue. Para esas
        # filas, migrar + --revert es un round-trip exacto (SC-008).
        self.products["v1"].image_url = None
        self.products["v3"].image_url = None
        self.db.commit()

        before = self._snapshot()
        mig.run(write=True, revert=False)
        mig.run(write=True, revert=True)
        self.assertEqual(self._snapshot(), before)

    def test_revert_de_una_subida_nueva_la_deja_como_url_vieja_del_mismo_objeto(self):
        # Nota operativa de contracts/data-migration.md: --revert reconstruye
        # {R2_PUBLIC_BASE_URL}/{key} para TODA key, sin distinguir si la puso la
        # migración o una subida nueva posterior. La imagen sigue viéndose (el
        # objeto físico es el mismo); solo cambia el dominio de la referencia.
        mig.run(write=True, revert=True)
        _, products, _ = self._refresh_all()
        self.assertEqual(products["v1"].image_url, f"{LEGACY}/{V1_IMG}")

    def test_revert_es_idempotente_y_no_toca_celular(self):
        mig.run(write=True, revert=False)
        mig.run(write=True, revert=True)
        _, _, method = self._refresh_all()
        self.assertEqual(method.payment_info["celular"], "3001234567")
        self.assertEqual(method.payment_info["qr"], f"{LEGACY}/{_key('payment-methods')}")

    # -------------------------------------------------------------- escenario 6
    def test_relanzar_tras_migrar_parcial_converge(self):
        # simula una interrupción: solo se migró el logo (shared); relanzar
        # completa products/payment_info sin re-tocar el logo.
        mig._process_shared(write=True, revert=False)
        code = mig.run(write=True, revert=False)
        self.assertEqual(code, 0)
        tenant, products, method = self._refresh_all()
        self.assertEqual(tenant.logo_url, _key("logo"))
        self.assertEqual(products["v2"].image_url, _key("products", "vieja.png"))
        self.assertEqual(method.payment_info["qr"], _key("payment-methods"))

    def _snapshot(self):
        self.db.expire_all()
        tenant = self.db.get(Tenant, 1)
        products = self.db.execute(select(Product).order_by(Product.name)).scalars().all()
        method = self.db.get(PaymentMethod, self.method.id)
        return (
            tenant.logo_url,
            tuple((p.name, p.image_url) for p in products),
            tuple(sorted(method.payment_info.items())),
        )


if __name__ == "__main__":
    unittest.main()
