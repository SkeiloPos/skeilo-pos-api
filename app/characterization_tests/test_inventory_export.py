"""Tests de la funcionalidad de export/import de inventario — spec 086 (origen),
ampliada por spec 109-inventario-excel-masivo (A-123): GET /inventory/items/export
(Historia 1 de la 086: descarga del reporte completo; Historia 2: consistencia de
columnas y datos exportados; Historia 1 de la 109: plantilla única de export/import).

No son characterization tests -- el endpoint no existe antes de la spec 086
(comportamiento enteramente nuevo, ver `research.md`/`contracts/
inventory-export-api.md` de esa spec). Por eso esta spec 109 actualiza sus
aserciones (nuevas columnas) directamente, sin necesitar la autorización especial
del Principio III (ver A-123, plan.md de la 109). Combina las tablas de
`plan_fixtures.py` (tenants/plans/roles/users, para el gating de plan y el
rol ADMIN) con las de `fixtures.py` (insumos/unidades de medida), mismo
patrón que `test_plan_gating_inventory_fields.py`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_inventory_export -v
"""
from __future__ import annotations

import io
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from uuid import UUID

import openpyxl
from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from app.characterization_tests import fixtures as f
from app.characterization_tests import plan_fixtures as pf
from app.api.v1.inventory.router import router as inventory_router
from app.core.db import get_db, get_tenant
from app.core.dependencies import get_current_user
from app.core.models import Base
from app.core.timezone import utc_now
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem
from app.models.inventory_import_job import InventoryImportJob
from app.api.v1.inventory.schemas import INVENTORY_TEMPLATE_HEADERS

_TABLE_NAMES = [
    # plan_fixtures.py — gating de plan y rol ADMIN
    "tenants", "plans", "roles", "users",
    # fixtures.py — insumos y unidades de medida
    "inventory_items", "inventory_movements", "unit_measures",
    # spec 109: el export siempre consulta la relación preferida, aunque esté vacía;
    # el import necesita además la tabla de jobs.
    "suppliers", "supplier_items", "inventory_import_jobs",
]


def _uid():
    import uuid
    return uuid.uuid4()


def _make_supplier(db: Session, **kw) -> Supplier:
    kw.setdefault("id", _uid())
    kw.setdefault("name", f"proveedor-{kw['id']}")
    kw.setdefault("active", True)
    kw.setdefault("is_system", False)
    obj = Supplier(**kw)
    db.add(obj)
    db.flush()
    return obj


def _make_supplier_item(db: Session, supplier: Supplier, item, **kw) -> SupplierItem:
    kw.setdefault("id", _uid())
    kw.setdefault("supplier_id", supplier.id)
    kw.setdefault("inventory_item_id", item.id)
    kw.setdefault("presentation", "Bolsa x 500 g")
    kw.setdefault("conversion_factor", Decimal("500"))
    kw.setdefault("active", True)
    kw.setdefault("preferred", True)
    obj = SupplierItem(**kw)
    db.add(obj)
    db.flush()
    return obj


def _new_session() -> Session:
    """`check_same_thread=False`: a diferencia de las demás fixtures del
    paquete, este módulo ejercita el endpoint vía `TestClient` (spec 086),
    que ejecuta el handler en un thread distinto al del test — mismo ajuste
    que `test_notifications.py` (spec 077) ya necesitó por el mismo motivo."""
    tables = [t for t in Base.metadata.tables.values() if t.name in _TABLE_NAMES]
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    conn = engine.connect().execution_options(
        schema_translate_map={"tenant": None, "shared": None}
    )
    Base.metadata.create_all(bind=conn, tables=tables)
    conn.commit()
    return Session(bind=conn)


def _build_client(db: Session, tenant, user) -> TestClient:
    """App mínima con el router real de inventario -- mismo patrón que
    `test_notifications.py` (spec 077): se sobreescriben `get_db`,
    `get_current_user` y `get_tenant` (esta última la usa
    `require_module_access("inventario")`, aplicado a nivel de router)."""
    app = FastAPI()
    app.include_router(inventory_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_tenant] = lambda: tenant
    return TestClient(app, raise_server_exceptions=False)


def _con_modulo_inventario(db: Session):
    plan = pf.make_plan(db, inventario_access=True)
    tenant = pf.make_tenant(db, plan=plan)
    return tenant


class ExportEndpointTests(unittest.TestCase):
    """Historia 1 (P1): descarga del reporte completo."""

    def setUp(self):
        self.db = _new_session()
        self.tenant = _con_modulo_inventario(self.db)

    def test_admin_con_insumos_descarga_n_mas_1_filas(self):
        for _ in range(3):
            f.make_inventory_item(self.db)
        f.make_inventory_item(self.db, active=False)
        self.db.commit()

        admin = pf.make_user(self.db, self.tenant, role_name="ADMIN")
        self.db.commit()
        client = _build_client(self.db, self.tenant, admin)

        resp = client.get("/api/v1/inventory/items/export")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn('filename="inventario_', resp.headers["content-disposition"])
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.active.max_row, 4 + 1)  # 4 insumos (3 activos + 1 inactivo)

    def test_inventario_vacio_descarga_solo_el_encabezado(self):
        admin = pf.make_user(self.db, self.tenant, role_name="ADMIN")
        self.db.commit()
        client = _build_client(self.db, self.tenant, admin)

        resp = client.get("/api/v1/inventory/items/export")

        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.active.max_row, 1)

    def test_usuario_sin_rol_admin_recibe_403_sin_archivo(self):
        cashier = pf.make_user(self.db, self.tenant, role_name="CASHIER")
        self.db.commit()
        client = _build_client(self.db, self.tenant, cashier)

        resp = client.get("/api/v1/inventory/items/export")

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("content-disposition", resp.headers)

    def test_fallo_a_mitad_de_generacion_responde_500_sin_archivo_parcial(self):
        admin = pf.make_user(self.db, self.tenant, role_name="ADMIN")
        self.db.commit()
        client = _build_client(self.db, self.tenant, admin)

        with mock.patch(
            "app.api.v1.inventory.router.build_inventory_excel",
            side_effect=RuntimeError("fallo simulado a mitad de la generación"),
        ):
            resp = client.get("/api/v1/inventory/items/export")

        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("content-disposition", resp.headers)
        self.assertNotIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            resp.headers.get("content-type", ""),
        )


class ExportColumnsTests(unittest.TestCase):
    """Historia 2 (P2): encabezados exactos y celdas numéricas nativas."""

    def setUp(self):
        self.db = _new_session()
        self.tenant = _con_modulo_inventario(self.db)
        self.admin = pf.make_user(self.db, self.tenant, role_name="ADMIN")

    def _export(self):
        self.db.commit()
        client = _build_client(self.db, self.tenant, self.admin)
        resp = client.get("/api/v1/inventory/items/export")
        self.assertEqual(resp.status_code, 200)
        return openpyxl.load_workbook(io.BytesIO(resp.content))

    def test_fila_1_son_los_encabezados_exactos_en_orden(self):
        wb = self._export()
        headers = [c.value for c in next(wb.active.iter_rows(min_row=1, max_row=1))]
        self.assertEqual(headers, [
            "Código", "Nombre", "Tipo", "Unidad", "Stock", "Mínimo", "Costo",
            "Proveedor preferido", "Presentación", "Factor de conversión", "Último costo",
        ])

    def test_celdas_stock_minimo_costo_son_numericas_no_texto(self):
        f.make_inventory_item(
            self.db, current_stock=Decimal("12.500"), min_stock=Decimal("3.000"),
            unit_cost=Decimal("15.00"),
        )
        wb = self._export()
        row = next(wb.active.iter_rows(min_row=2, max_row=2))
        stock_cell, min_cell, cost_cell = row[4], row[5], row[6]
        self.assertEqual(stock_cell.data_type, "n")
        self.assertEqual(min_cell.data_type, "n")
        self.assertEqual(cost_cell.data_type, "n")

    def test_costo_15_exporta_numero_15_no_texto_con_simbolo_de_moneda(self):
        f.make_inventory_item(self.db, unit_cost=Decimal("15.00"))
        wb = self._export()
        row = next(wb.active.iter_rows(min_row=2, max_row=2))
        cost_cell = row[6]
        self.assertEqual(float(cost_cell.value), 15.0)
        self.assertNotIsInstance(cost_cell.value, str)

    def test_codigo_es_la_primera_columna_y_es_unico_por_fila(self):
        f.make_inventory_item(self.db, name="Leche", code="0001")
        f.make_inventory_item(self.db, name="Fresa", code="0002")
        wb = self._export()
        codigos = [row[0].value for row in wb.active.iter_rows(min_row=2)]
        self.assertEqual(sorted(codigos), ["0001", "0002"])


class ExportLabelsTests(unittest.TestCase):
    """Historia 2 (P2): etiquetas de Tipo/Unidad/Estado idénticas a pantalla."""

    def setUp(self):
        self.db = _new_session()
        self.tenant = _con_modulo_inventario(self.db)
        self.admin = pf.make_user(self.db, self.tenant, role_name="ADMIN")

    def _export(self):
        self.db.commit()
        client = _build_client(self.db, self.tenant, self.admin)
        resp = client.get("/api/v1/inventory/items/export")
        return openpyxl.load_workbook(io.BytesIO(resp.content))

    def test_tipo_raw_material_exporta_materia_prima_y_packaged_exporta_empacado(self):
        f.make_inventory_item(self.db, name="Leche", type="raw_material")
        f.make_inventory_item(self.db, name="Vaso desechable", type="packaged")
        wb = self._export()
        tipos = {row[1].value: row[2].value for row in wb.active.iter_rows(min_row=2)}
        self.assertEqual(tipos["Leche"], "Materia prima")
        self.assertEqual(tipos["Vaso desechable"], "Empacado")

    def test_unidad_exporta_la_abreviatura_no_el_nombre_largo(self):
        unit = f.make_unit(self.db, name="Litro", abbreviation="lt")
        f.make_inventory_item(self.db, name="Leche", unit=unit)
        wb = self._export()
        row = next(wb.active.iter_rows(min_row=2, max_row=2))
        self.assertEqual(row[3].value, "lt")

    def test_insumo_sin_proveedor_preferido_deja_esas_columnas_en_blanco(self):
        """spec 109, Historia 1, escenario 3: sin proveedor preferido, las 4 columnas
        quedan en blanco, sin error."""
        f.make_inventory_item(self.db, name="Sin proveedor")
        wb = self._export()
        row = next(wb.active.iter_rows(min_row=2, max_row=2))
        proveedor, presentacion, factor, ultimo_costo = row[7], row[8], row[9], row[10]
        # openpyxl normaliza la cadena vacía a None al leerla de vuelta.
        self.assertFalse(proveedor.value)
        self.assertFalse(presentacion.value)
        self.assertFalse(factor.value)
        self.assertFalse(ultimo_costo.value)

    def test_insumo_con_proveedor_preferido_exporta_presentacion_factor_y_ultimo_costo(self):
        """spec 109, Historia 1: datos del proveedor preferido (spec 106/107) en la
        plantilla de export/import."""
        item = f.make_inventory_item(self.db, name="Con proveedor")
        supplier = _make_supplier(self.db, name="Distribuidora Norte")
        _make_supplier_item(
            self.db, supplier, item,
            presentation="Bolsa x 500 g", conversion_factor=Decimal("500"),
            last_cost=Decimal("4000"),
        )
        wb = self._export()
        row = next(wb.active.iter_rows(min_row=2, max_row=2))
        self.assertEqual(row[7].value, "Distribuidora Norte")
        self.assertEqual(row[8].value, "Bolsa x 500 g")
        self.assertEqual(float(row[9].value), 500.0)
        self.assertEqual(float(row[10].value), 4000.0)


class ExportEncodingTests(unittest.TestCase):
    """Historia 2 (P2): nombres con tildes/eñes/comillas sin corrupción (FR-007)."""

    def setUp(self):
        self.db = _new_session()
        self.tenant = _con_modulo_inventario(self.db)
        self.admin = pf.make_user(self.db, self.tenant, role_name="ADMIN")

    def test_nombres_con_caracteres_del_espanol_se_preservan_intactos(self):
        f.make_inventory_item(self.db, name="Champiñón")
        f.make_inventory_item(self.db, name="Arequipe (500ml)")
        self.db.commit()
        client = _build_client(self.db, self.tenant, self.admin)
        resp = client.get("/api/v1/inventory/items/export")

        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        nombres = [row[1].value for row in wb.active.iter_rows(min_row=2)]
        self.assertIn("Champiñón", nombres)
        self.assertIn("Arequipe (500ml)", nombres)


def _xlsx_bytes(headers, filas):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for fila in filas:
        ws.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer.getvalue()


class ImportEndpointTests(unittest.TestCase):
    """spec 109, Historia 2/3: POST /inventory/items/import -- estructura
    (FR-005), candado de una importación a la vez (FR-010) y encolado de la
    tarea Celery, sin necesitar un worker real (se mockea `.delay`, mismo
    patrón que `send_email_task` en `test_auth_forgot_password.py`)."""

    def setUp(self):
        self.db = _new_session()
        self.tenant = _con_modulo_inventario(self.db)
        self.admin = pf.make_user(self.db, self.tenant, role_name="ADMIN")

    def _upload(self, content: bytes):
        self.db.commit()
        client = _build_client(self.db, self.tenant, self.admin)
        with mock.patch("app.api.v1.inventory.router.import_inventory_task") as mock_task:
            resp = client.post(
                "/api/v1/inventory/items/import",
                files={"file": ("inventario.xlsx", content,
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        return resp, mock_task

    def test_estructura_valida_crea_job_y_encola_la_tarea(self):
        content = _xlsx_bytes(INVENTORY_TEMPLATE_HEADERS, [
            ["", "Insumo nuevo", "Materia prima", "un", "0", "0", "0", "", "", "", ""],
        ])
        resp, mock_task = self._upload(content)
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["status"], "procesando")
        self.assertEqual(body["total_rows"], 1)
        mock_task.delay.assert_called_once()
        job = self.db.get(InventoryImportJob, UUID(body["id"]))
        self.assertIsNotNone(job)

    def test_estructura_alterada_responde_422_sin_crear_job(self):
        content = _xlsx_bytes(["Nombre", "Código"], [["x", "y"]])
        resp, mock_task = self._upload(content)
        self.assertEqual(resp.status_code, 422)
        mock_task.delay.assert_not_called()
        count = self.db.execute(select(InventoryImportJob)).scalars().all()
        self.assertEqual(count, [])

    def test_archivo_vacio_responde_422(self):
        wb = openpyxl.Workbook()
        buffer = io.BytesIO()
        wb.save(buffer)
        resp, mock_task = self._upload(b"")
        self.assertEqual(resp.status_code, 422)
        mock_task.delay.assert_not_called()

    def test_segunda_importacion_con_una_en_curso_responde_409(self):
        self.db.add(InventoryImportJob(
            status="procesando", file_name="otro.xlsx",
            started_by_user_id=self.admin.id, total_rows=5,
        ))
        content = _xlsx_bytes(INVENTORY_TEMPLATE_HEADERS, [
            ["", "Insumo nuevo", "Materia prima", "un", "0", "0", "0", "", "", "", ""],
        ])
        resp, mock_task = self._upload(content)
        self.assertEqual(resp.status_code, 409)
        mock_task.delay.assert_not_called()

    def test_usuario_sin_rol_admin_no_puede_importar(self):
        cashier = pf.make_user(self.db, self.tenant, role_name="CASHIER")
        self.db.commit()
        client = _build_client(self.db, self.tenant, cashier)
        content = _xlsx_bytes(INVENTORY_TEMPLATE_HEADERS, [])
        with mock.patch("app.api.v1.inventory.router.import_inventory_task") as mock_task:
            resp = client.post(
                "/api/v1/inventory/items/import",
                files={"file": ("inventario.xlsx", content,
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            )
        self.assertEqual(resp.status_code, 403)
        mock_task.delay.assert_not_called()

    def test_job_procesando_vencido_no_bloquea_y_queda_interrumpido(self):
        """spec 109, research.md D5: un job `procesando` de más de 30 minutos se
        considera abandonado -- no bloquea una importación nueva y queda marcado
        `interrumpido`."""
        viejo = InventoryImportJob(
            status="procesando", file_name="viejo.xlsx",
            started_by_user_id=self.admin.id, total_rows=1,
            started_at=utc_now() - timedelta(minutes=45),
        )
        self.db.add(viejo)
        self.db.commit()
        viejo_id = viejo.id

        content = _xlsx_bytes(INVENTORY_TEMPLATE_HEADERS, [
            ["", "Insumo nuevo", "Materia prima", "un", "0", "0", "0", "", "", "", ""],
        ])
        resp, mock_task = self._upload(content)
        self.assertEqual(resp.status_code, 202)
        mock_task.delay.assert_called_once()

        self.db.expire_all()
        self.assertEqual(self.db.get(InventoryImportJob, viejo_id).status, "interrumpido")

    def test_get_current_devuelve_el_job_mas_reciente(self):
        self.db.commit()
        client = _build_client(self.db, self.tenant, self.admin)
        self.assertIsNone(client.get("/api/v1/inventory/items/import/current").json())

        content = _xlsx_bytes(INVENTORY_TEMPLATE_HEADERS, [
            ["", "Insumo nuevo", "Materia prima", "un", "0", "0", "0", "", "", "", ""],
        ])
        resp, _mock_task = self._upload(content)
        job_id = resp.json()["id"]

        current = client.get("/api/v1/inventory/items/import/current").json()
        self.assertEqual(current["id"], job_id)
        self.assertEqual(current["status"], "procesando")
        self.assertFalse(current["has_errors"])

    def test_get_by_id_404_si_el_job_no_existe(self):
        self.db.commit()
        client = _build_client(self.db, self.tenant, self.admin)
        resp = client.get(f"/api/v1/inventory/items/import/{_uid()}")
        self.assertEqual(resp.status_code, 404)

    def test_descarga_de_errores_404_sin_filas_fallidas(self):
        job = InventoryImportJob(
            status="finalizado", file_name="ok.xlsx",
            started_by_user_id=self.admin.id, total_rows=1, failed_count=0,
        )
        self.db.add(job)
        self.db.commit()
        client = _build_client(self.db, self.tenant, self.admin)
        resp = client.get(f"/api/v1/inventory/items/import/{job.id}/errors")
        self.assertEqual(resp.status_code, 404)

    def test_descarga_de_errores_contiene_fila_codigo_y_motivo(self):
        job = InventoryImportJob(
            status="finalizado", file_name="con_errores.xlsx",
            started_by_user_id=self.admin.id, total_rows=1, failed_count=1,
            errors=[{"fila": 3, "codigo": "0099", "motivo": "Costo debe ser numérico"}],
        )
        self.db.add(job)
        self.db.commit()
        client = _build_client(self.db, self.tenant, self.admin)
        resp = client.get(f"/api/v1/inventory/items/import/{job.id}/errors")
        self.assertEqual(resp.status_code, 200)
        wb = openpyxl.load_workbook(io.BytesIO(resp.content))
        headers = [c.value for c in next(wb.active.iter_rows(min_row=1, max_row=1))]
        self.assertEqual(headers, ["Fila", "Código", "Motivo"])
        row = [c.value for c in next(wb.active.iter_rows(min_row=2, max_row=2))]
        self.assertEqual(row, [3, "0099", "Costo debe ser numérico"])


if __name__ == "__main__":
    unittest.main()
