"""Test de export/import masivo de inventario en Excel (spec 109, ajustada por
spec 138: retiro de las columnas de proveedor preferido y vinculación de la
columna Unidad a `UnitMeasure` reales).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_inventory_bulk_excel

Cubre los Acceptance Scenarios de las historias de usuario de la spec 138 que
solo se pueden ejercer contra Postgres real -- en particular el gap de
`group_id` NOT NULL (spec 137/139) en la autocreación de unidades, que SQLite
no expone igual (ver quickstart.md §1, Escenario 4). Trabaja sobre un tenant
real con datos desechables: crea su propio insumo y unidad, y los borra al
terminar (incluso si alguna prueba falla a mitad de camino -- ver `main()`).
"""
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, delete, func, text

from app.core.db import with_db
from app.core.timezone import utc_now
from app.models.inventory_item import InventoryItem
from app.models.unit_measure import UnitMeasure
from app.models.unit_measure_group import UnitMeasureGroup
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem
from app.models.inventory_import_job import InventoryImportJob
from app.api.v1.inventory import service, supplier_items
from app.api.v1.inventory.export import build_inventory_excel
from app.api.v1.inventory.import_ import validar_estructura, procesar_archivo_importacion
from app.api.v1.inventory.schemas import INVENTORY_TEMPLATE_HEADERS

# Códigos extra creados durante las pruebas (p. ej. por un código vacío que se
# autoasigna) y jobs creados con un `started_by_user_id` desechable -- se
# limpian al final por id exacto, nunca por patrón de texto, para no
# arriesgarse a tocar datos reales del tenant.
_codigos_extra: list[str] = []
_job_ids: list = []
_unidades_extra: list = []
_grupos_extra: list = []
_proveedores_extra: list = []


def _schema() -> str:
    with with_db(None) as db:
        rows = db.execute(text("SELECT schema FROM shared.tenants ORDER BY id")).scalars().all()
    for schema in rows:
        with with_db(None) as db:
            exists = db.execute(
                text("SELECT to_regclass(:q)"), {"q": f"{schema}.inventory_items"}
            ).scalar() is not None
        if exists:
            return schema
    raise SystemExit("Ningún tenant tiene su esquema de inventario creado todavía.")


def _fixture(db):
    unit = db.execute(
        select(UnitMeasure).where(func.upper(UnitMeasure.abbreviation) == "G")
    ).scalars().first()
    if unit is None:
        grupo = UnitMeasureGroup(name=f"gramo-test-{uuid4().hex[:6]}")
        db.add(grupo)
        db.flush()
        unit = UnitMeasure(
            name=f"gramo-test-{uuid4().hex[:6]}", abbreviation="g",
            group_id=grupo.id, is_base=True, factor_to_base=1,
        )
        db.add(unit)
        db.flush()

    item_exacto = InventoryItem(
        code=f"T{uuid4().hex[:6]}", name=f"Azucar-test-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=Decimal("10"), min_stock=Decimal("2"),
        unit_cost=Decimal("2000"),
    )
    db.add(item_exacto)
    db.flush()
    return unit, item_exacto


def _cleanup(db, unit, item) -> None:
    """ORM `delete()`, no SQL crudo con nombres de tabla sin calificar -- un
    `text()` plano no pasa por el `schema_translate_map` de `with_db()` y
    apunta al esquema equivocado (o a ninguno)."""
    db.rollback()
    extra_codes = [*_codigos_extra]
    extra_ids = db.execute(
        select(InventoryItem.id).where(InventoryItem.code.in_(extra_codes))
    ).scalars().all() if extra_codes else []
    all_item_ids = [item.id, *extra_ids]

    if _job_ids:
        db.execute(delete(InventoryImportJob).where(InventoryImportJob.id.in_(_job_ids)))
    if _proveedores_extra:
        db.execute(delete(SupplierItem).where(SupplierItem.supplier_id.in_(_proveedores_extra)))
        db.execute(delete(Supplier).where(Supplier.id.in_(_proveedores_extra)))
    db.execute(delete(InventoryItem).where(InventoryItem.id.in_(all_item_ids)))
    if _unidades_extra:
        db.execute(delete(UnitMeasure).where(UnitMeasure.id.in_(_unidades_extra)))
    if _grupos_extra:
        db.execute(delete(UnitMeasureGroup).where(UnitMeasureGroup.id.in_(_grupos_extra)))
    db.commit()


def _row_by_header(headers: list[str], values: list, header: str):
    return values[headers.index(header)]


def test_export_incluye_proveedor_preferido_sin_presentacion_ni_factor(db, item, unit):
    """spec 138, FR-001/FR-002: la plantilla trae código y el nombre del
    proveedor preferido, pero ya no trae presentación/factor de conversión/
    último costo."""
    preferred = supplier_items.preferred_relations_by_item(db, [item.id])
    buffer = build_inventory_excel([item], {unit.id: unit}, preferred)

    import openpyxl
    wb = openpyxl.load_workbook(buffer)
    headers = [c.value for c in next(wb.active.iter_rows(min_row=1, max_row=1))]
    assert headers == INVENTORY_TEMPLATE_HEADERS, headers
    assert "Presentación" not in headers
    assert "Factor de conversión" not in headers
    assert "Último costo" not in headers
    row = [c.value for c in next(wb.active.iter_rows(min_row=2, max_row=2))]
    assert _row_by_header(headers, row, "Código") == item.code
    assert len(row) == 8, row
    print("OK: export_incluye_proveedor_preferido_sin_presentacion_ni_factor")


def _job(db, user_id, total_rows: int) -> InventoryImportJob:
    job = InventoryImportJob(
        status="procesando", file_name="test.xlsx", started_by_user_id=user_id,
        total_rows=total_rows,
    )
    db.add(job)
    db.flush()
    _job_ids.append(job.id)
    return job


def test_reimportar_archivo_exportado_no_falla_y_actualiza(db, item, unit):
    """Historia 1, escenario 2: re-subir el mismo archivo exportado no produce
    ningún error y trata la fila como actualización -- la unidad de la fila
    (igual a la actual) se ignora por completo (FR-007), no solo no cambia."""
    headers = INVENTORY_TEMPLATE_HEADERS
    fila = [
        item.code, item.name, "Materia prima", unit.abbreviation,
        float(item.current_stock), float(item.min_stock), float(item.unit_cost),
    ]
    assert validar_estructura(headers) is None

    job = _job(db, item.id, 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.updated_count == 1, job.errors
    assert job.created_count == 0
    assert job.failed_count == 0
    print("OK: reimportar_archivo_exportado_no_falla_y_actualiza")


def test_editar_costo_actualiza_el_insumo(db, item, unit):
    """Historia 2, escenario 1: editar una fila (costo) y reimportar actualiza
    ese insumo."""
    nuevo_costo = Decimal("2500")
    fila = [item.code, item.name, "Materia prima", unit.abbreviation, "10", "2", str(nuevo_costo)]
    job = _job(db, item.id, 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(item)
    assert item.unit_cost == nuevo_costo, item.unit_cost
    print("OK: editar_costo_actualiza_el_insumo")


def test_fila_nueva_con_codigo_vacio_crea_insumo_con_codigo_automatico(db, unit):
    """Historia 2, escenario 2: código vacío -> se crea un insumo nuevo con un
    código asignado automáticamente."""
    antes = set(db.execute(select(InventoryItem.code)).scalars().all())
    fila = ["", f"Nuevo-{uuid4().hex[:6]}", "Materia prima", unit.abbreviation, "0", "0", "0"]
    job = _job(db, uuid4(), 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.created_count == 1, job.errors
    despues = set(db.execute(select(InventoryItem.code)).scalars().all())
    nuevos = despues - antes
    assert len(nuevos) == 1
    _codigos_extra.append(nuevos.pop())
    print("OK: fila_nueva_con_codigo_vacio_crea_insumo_con_codigo_automatico")


def test_filas_con_errores_distintos_fallan_individualmente(db, unit):
    """Historia 2, escenario 3: costo no numérico y tipo inválido fallan cada
    una con un motivo propio, sin bloquear el resto."""
    filas = [
        [f"T{uuid4().hex[:6]}", "Costo malo", "Materia prima", unit.abbreviation,
         "0", "0", "no-es-numero"],
        [f"T{uuid4().hex[:6]}", "Tipo malo", "Un tipo que no existe", unit.abbreviation,
         "0", "0", "0"],
    ]
    job = _job(db, uuid4(), len(filas))
    db.commit()
    procesar_archivo_importacion(db, job, filas)
    db.refresh(job)
    assert job.failed_count == 2, job.errors
    assert job.created_count == 0
    motivos = {e["motivo"] for e in job.errors}
    assert len(motivos) == 2, motivos
    print("OK: filas_con_errores_distintos_fallan_individualmente")


def test_unidad_inexistente_se_crea_con_grupo_propio_sin_integrity_error(db):
    """spec 138, Historia 2 (research.md §3): el gap de `group_id` NOT NULL
    (spec 137/139) rompía esta ruta contra Postgres real -- una unidad que no
    existe ya se crea con su propio `UnitMeasureGroup`, como base, factor 1, sin
    lanzar `IntegrityError`."""
    etiqueta = f"lb-test-{uuid4().hex[:5]}"
    code = f"T{uuid4().hex[:6]}"
    fila = [code, f"Insumo en {etiqueta}", "Materia prima", etiqueta, "0", "0", "0"]
    job = _job(db, uuid4(), 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.created_count == 1, job.errors
    assert job.failed_count == 0, job.errors

    _codigos_extra.append(code)
    unidad = db.execute(
        select(UnitMeasure).where(UnitMeasure.abbreviation == etiqueta.upper())
    ).scalar_one_or_none()
    assert unidad is not None, "la unidad debía quedar creada en mayúscula"
    assert unidad.name == etiqueta.upper()
    assert unidad.group_id is not None, "group_id debía quedar asignado (research.md §3)"
    assert unidad.is_base is True
    assert unidad.factor_to_base == Decimal("1")
    _unidades_extra.append(unidad.id)
    _grupos_extra.append(unidad.group_id)
    grupo = db.get(UnitMeasureGroup, unidad.group_id)
    assert grupo is not None and grupo.name == etiqueta.upper()
    print("OK: unidad_inexistente_se_crea_con_grupo_propio_sin_integrity_error")


def test_unidad_existente_se_reconoce_sin_distinguir_mayusculas(db, unit):
    """La unidad del fixture (`unit.abbreviation`, normalmente en minúscula)
    debe reconocerse aunque el Excel la escriba en mayúscula/minúscula
    distinta -- no debe crear una unidad duplicada."""
    antes = db.execute(select(UnitMeasure.id)).scalars().all()
    code = f"T{uuid4().hex[:6]}"
    fila = [code, "Insumo con unidad en otro caso", "Materia prima",
            unit.abbreviation.swapcase(), "0", "0", "0"]
    job = _job(db, uuid4(), 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.created_count == 1, job.errors
    _codigos_extra.append(code)

    despues = db.execute(select(UnitMeasure.id)).scalars().all()
    assert set(despues) == set(antes), "no debía crear ninguna unidad nueva"
    print("OK: unidad_existente_se_reconoce_sin_distinguir_mayusculas")


def test_actualizar_insumo_existente_ignora_la_unidad_de_la_fila(db, item, unit):
    """spec 138, Historia 3 (FR-007): actualizar por código con una Unidad
    distinta a la actual no cambia la unidad del insumo ni crea ninguna
    `UnitMeasure` nueva -- solo el resto de campos de la fila se actualiza."""
    antes_unidades = set(db.execute(select(UnitMeasure.id)).scalars().all())
    fila = [item.code, item.name, "Materia prima", "UNIDAD-QUE-NO-EXISTE",
            "7", "1", "999"]
    job = _job(db, item.id, 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.updated_count == 1, job.errors
    db.refresh(item)
    assert item.unit_measure_id == unit.id, "la unidad del insumo no debía cambiar"
    assert item.unit_cost == Decimal("999")
    despues_unidades = set(db.execute(select(UnitMeasure.id)).scalars().all())
    assert despues_unidades == antes_unidades, "no debía crear ninguna unidad nueva"
    print("OK: actualizar_insumo_existente_ignora_la_unidad_de_la_fila")


def test_estructura_alterada_no_crea_job():
    """Historia 2, escenario 4: columnas fuera de orden -> rechazo de estructura."""
    problemas = validar_estructura(["Nombre", "Código", "Tipo"])
    assert problemas, "se esperaba un problema de estructura"
    print("OK: estructura_alterada_no_crea_job")


def test_archivo_legado_de_11_columnas_se_acepta_sin_error_de_estructura():
    """spec 138, FR-003: un archivo exportado antes de este cambio (11
    columnas, con presentación/factor/último costo al final) se sigue
    aceptando."""
    legacy_headers = [
        *INVENTORY_TEMPLATE_HEADERS,
        "Presentación", "Factor de conversión", "Último costo",
    ]
    assert validar_estructura(legacy_headers) is None
    print("OK: archivo_legado_de_11_columnas_se_acepta_sin_error_de_estructura")


def test_proveedor_preferido_marca_relacion_existente_sin_integrity_error(db, item, unit):
    """spec 138 (ajuste pedido por el usuario): importar un Proveedor preferido
    que ya tiene una relación activa con el insumo la marca como preferida --
    sin tocar presentación/factor/último costo de esa relación."""
    proveedor = Supplier(name=f"Proveedor-test-{uuid4().hex[:6]}", is_system=False)
    db.add(proveedor)
    db.flush()
    relacion = SupplierItem(
        supplier_id=proveedor.id, inventory_item_id=item.id,
        presentation="Bolsa x 500 g", conversion_factor=Decimal("500"),
        active=True, preferred=False,
    )
    db.add(relacion)
    db.flush()
    _proveedores_extra.append(proveedor.id)

    fila = [item.code, item.name, "Materia prima", unit.abbreviation,
            str(item.current_stock), str(item.min_stock), str(item.unit_cost),
            proveedor.name.lower()]
    job = _job(db, item.id, 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.updated_count == 1, job.errors

    db.refresh(relacion)
    assert relacion.preferred, "la relación debía quedar marcada como preferida"
    assert relacion.presentation == "Bolsa x 500 g"
    assert relacion.conversion_factor == Decimal("500")
    print("OK: proveedor_preferido_marca_relacion_existente_sin_integrity_error")


def test_proveedor_preferido_sin_relacion_activa_crea_proveedor_y_relacion_con_null(db, item, unit):
    """spec 138 (ajuste pedido por el usuario): si el proveedor no existe
    todavía, se crea (igual que antes de esta spec); y se crea también la
    relación proveedor-insumo preferida, con `presentation`/`conversion_factor`
    en `NULL` porque la plantilla ya no los trae -- cierra el gap de NOT NULL
    de esas dos columnas (ver migración `c5815ab62693`), que el admin completa
    después desde el módulo de proveedores."""
    nombre = f"Proveedor sin relacion {uuid4().hex[:5]}"
    fila = [item.code, item.name, "Materia prima", unit.abbreviation,
            str(item.current_stock), str(item.min_stock), "12345", nombre]
    job = _job(db, item.id, 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.updated_count == 1, job.errors
    assert job.failed_count == 0, job.errors
    db.refresh(item)
    assert item.unit_cost == Decimal("12345")

    proveedor = db.execute(
        select(Supplier).where(Supplier.name == nombre.upper())
    ).scalar_one_or_none()
    assert proveedor is not None, "el proveedor debía quedar creado en mayúscula"
    _proveedores_extra.append(proveedor.id)

    relacion = db.execute(
        select(SupplierItem).where(
            SupplierItem.inventory_item_id == item.id,
            SupplierItem.supplier_id == proveedor.id,
        )
    ).scalar_one_or_none()
    assert relacion is not None, "la relación preferida debía quedar creada"
    assert relacion.preferred is True
    assert relacion.presentation is None
    assert relacion.conversion_factor is None
    print("OK: proveedor_preferido_sin_relacion_activa_crea_proveedor_y_relacion_con_null")


def test_codigos_duplicados_en_el_archivo_fallan_ambas(db, unit):
    """Edge case del spec: dos filas con el mismo código en el mismo archivo
    fallan las dos, ninguna se aplica."""
    code = f"DUP-{uuid4().hex[:6]}"
    filas = [
        [code, "Primera", "Materia prima", unit.abbreviation, "0", "0", "0"],
        [code, "Segunda", "Materia prima", unit.abbreviation, "0", "0", "0"],
    ]
    job = _job(db, uuid4(), len(filas))
    db.commit()
    procesar_archivo_importacion(db, job, filas)
    db.refresh(job)
    assert job.failed_count == 2, job.errors
    assert job.created_count == 0
    existe = db.execute(
        select(InventoryItem).where(InventoryItem.code == code)
    ).scalar_one_or_none()
    assert existe is None
    print("OK: codigos_duplicados_en_el_archivo_fallan_ambas")


def test_nombre_duplicado_contra_insumo_existente_falla_la_fila(db, unit, item):
    """Regresión: un `code` nuevo con un `name` que ya existe en otro insumo
    (unique constraint `ix_tenant_inventory_items_name`) no debe tumbar el
    job entero -- debe fallar solo esa fila, dejando el resto del archivo
    aplicado y el job en `finalizado`."""
    code_nuevo = f"T{uuid4().hex[:6]}"
    code_ok = f"T{uuid4().hex[:6]}"
    filas = [
        [code_nuevo, item.name, "Materia prima", unit.abbreviation, "0", "0", "0"],
        [code_ok, f"Insumo-normal-{uuid4().hex[:6]}", "Materia prima", unit.abbreviation,
         "0", "0", "0"],
    ]
    job = _job(db, uuid4(), len(filas))
    db.commit()
    procesar_archivo_importacion(db, job, filas)
    db.refresh(job)
    assert job.status == "finalizado", job.status
    assert job.failed_count == 1, job.errors
    assert job.created_count == 1, job.errors
    assert "ya existe" in job.errors[0]["motivo"].lower(), job.errors

    _codigos_extra.append(code_ok)
    sin_crear = db.execute(
        select(InventoryItem).where(InventoryItem.code == code_nuevo)
    ).scalar_one_or_none()
    assert sin_crear is None, "la fila con nombre duplicado no debía crear nada"
    creado = db.execute(
        select(InventoryItem).where(InventoryItem.code == code_ok)
    ).scalar_one_or_none()
    assert creado is not None, "la fila siguiente debía aplicarse con normalidad"
    print("OK: nombre_duplicado_contra_insumo_existente_falla_la_fila")


def test_job_cancelado_a_mitad_de_archivo_detiene_las_filas_restantes(db, unit, schema):
    """Cancelación cooperativa (botón "Cancelar" del front, sin tocar la
    base de datos a mano): si otra sesión marca el job como ya no
    `procesando` mientras el worker sigue en el loop de
    `procesar_archivo_importacion`, debe cortar antes de la siguiente fila
    y no sobrescribir el status que dejó quien canceló."""
    code_1 = f"T{uuid4().hex[:6]}"
    code_2 = f"T{uuid4().hex[:6]}"
    filas = [
        [code_1, f"Antes-de-cancelar-{uuid4().hex[:6]}", "Materia prima", unit.abbreviation,
         "0", "0", "0"],
        [code_2, f"Despues-de-cancelar-{uuid4().hex[:6]}", "Materia prima", unit.abbreviation,
         "0", "0", "0"],
    ]
    job = _job(db, uuid4(), len(filas))
    db.commit()

    original_resolve = service.resolve_or_create_by_code

    def _resolver_y_cancelar(*args, **kwargs):
        resultado = original_resolve(*args, **kwargs)
        with with_db(schema) as otra_sesion:
            otro_job = otra_sesion.get(InventoryImportJob, job.id)
            otro_job.status = "interrumpido"
            otro_job.finished_at = utc_now().replace(tzinfo=None)
            otra_sesion.commit()
        return resultado

    service.resolve_or_create_by_code = _resolver_y_cancelar
    try:
        procesar_archivo_importacion(db, job, filas)
    finally:
        service.resolve_or_create_by_code = original_resolve

    db.refresh(job)
    assert job.status == "interrumpido", job.status
    assert job.created_count == 1, job.errors
    _codigos_extra.append(code_1)

    existe_segunda = db.execute(
        select(InventoryItem).where(InventoryItem.code == code_2)
    ).scalar_one_or_none()
    assert existe_segunda is None, "la fila posterior a la cancelación no debía aplicarse"
    print("OK: job_cancelado_a_mitad_de_archivo_detiene_las_filas_restantes")


def test_interrupcion_a_mitad_de_archivo_conserva_lo_ya_aplicado(db, unit):
    """research.md D5/FR-015 (quickstart.md §1 punto 9): si el proceso se
    detiene a mitad de un archivo (simulado invocando directamente las
    validaciones de fila por fuera de `procesar_archivo_importacion`, sin
    llegar a marcar el job como finalizado), la fila ya aplicada permanece
    aplicada -- no hay una transacción de todo el archivo que revertir."""
    from app.api.v1.inventory.import_ import validar_y_aplicar_fila

    code = f"T{uuid4().hex[:6]}"
    fila_aplicada = [code, f"Sobrevive-{uuid4().hex[:6]}", "Materia prima",
                      unit.abbreviation, "0", "0", "0"]
    job = _job(db, uuid4(), 2)
    db.commit()

    resultado = validar_y_aplicar_fila(db, fila_aplicada)
    assert resultado["ok"], resultado
    # El proceso "se interrumpe" aquí -- nunca se llama procesar_archivo_importacion
    # ni se marca el job como finalizado.

    _codigos_extra.append(code)
    existe = db.execute(
        select(InventoryItem).where(InventoryItem.code == code)
    ).scalar_one_or_none()
    assert existe is not None, "la fila aplicada antes del corte debía seguir aplicada"
    db.refresh(job)
    assert job.status == "procesando", "el job no se marca finalizado si nadie lo termina"
    print("OK: interrupcion_a_mitad_de_archivo_conserva_lo_ya_aplicado")


def main() -> None:
    schema = _schema()
    print(f"Usando tenant con esquema: {schema}")
    with with_db(schema) as db:
        unit, item = _fixture(db)
        db.commit()
        tests = [
            lambda: test_export_incluye_proveedor_preferido_sin_presentacion_ni_factor(db, item, unit),
            lambda: test_reimportar_archivo_exportado_no_falla_y_actualiza(db, item, unit),
            lambda: test_editar_costo_actualiza_el_insumo(db, item, unit),
            lambda: test_fila_nueva_con_codigo_vacio_crea_insumo_con_codigo_automatico(db, unit),
            lambda: test_filas_con_errores_distintos_fallan_individualmente(db, unit),
            test_estructura_alterada_no_crea_job,
            test_archivo_legado_de_11_columnas_se_acepta_sin_error_de_estructura,
            lambda: test_codigos_duplicados_en_el_archivo_fallan_ambas(db, unit),
            lambda: test_nombre_duplicado_contra_insumo_existente_falla_la_fila(db, unit, item),
            lambda: test_job_cancelado_a_mitad_de_archivo_detiene_las_filas_restantes(db, unit, schema),
            lambda: test_interrupcion_a_mitad_de_archivo_conserva_lo_ya_aplicado(db, unit),
            lambda: test_unidad_inexistente_se_crea_con_grupo_propio_sin_integrity_error(db),
            lambda: test_unidad_existente_se_reconoce_sin_distinguir_mayusculas(db, unit),
            lambda: test_actualizar_insumo_existente_ignora_la_unidad_de_la_fila(db, item, unit),
            lambda: test_proveedor_preferido_marca_relacion_existente_sin_integrity_error(db, item, unit),
            lambda: test_proveedor_preferido_sin_relacion_activa_crea_proveedor_y_relacion_con_null(db, item, unit),
        ]
        fallos = 0
        try:
            for test in tests:
                try:
                    test()
                except AssertionError as exc:
                    fallos += 1
                    print(f"FALLÓ: {test}: {exc}")
        finally:
            _cleanup(db, unit, item)
        if fallos:
            raise SystemExit(f"{fallos}/{len(tests)} pruebas fallaron")
        print(f"Todas las {len(tests)} pruebas pasaron.")


if __name__ == "__main__":
    main()
