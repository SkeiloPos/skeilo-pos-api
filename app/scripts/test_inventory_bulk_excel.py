"""Test de export/import masivo de inventario en Excel (spec 109).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_inventory_bulk_excel

Cubre los Acceptance Scenarios de las 4 historias de usuario de la spec — ver
quickstart.md §1 para el mapeo exacto. Trabaja sobre un tenant real con datos
desechables: crea su propio insumo, unidad y proveedor, y los borra al terminar
(incluso si alguna prueba falla a mitad de camino -- ver `main()`).
"""
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, delete, func, text

from app.core.db import with_db
from app.core.timezone import utc_now
from app.models.inventory_item import InventoryItem
from app.models.unit_measure import UnitMeasure
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
    unit = db.execute(select(UnitMeasure).where(UnitMeasure.abbreviation == "g")).scalar_one_or_none()
    if unit is None:
        unit = UnitMeasure(name=f"gramo-test-{uuid4().hex[:6]}", abbreviation="g")
        db.add(unit)
        db.flush()

    item_exacto = InventoryItem(
        code=f"T{uuid4().hex[:6]}", name=f"Azucar-test-{uuid4().hex[:8]}", type="raw_material",
        unit_measure_id=unit.id, current_stock=Decimal("10"), min_stock=Decimal("2"),
        unit_cost=Decimal("2000"),
    )
    supplier = Supplier(name=f"Distribuidora-test-{uuid4().hex[:6]}", is_system=False)
    db.add_all([item_exacto, supplier])
    db.flush()

    relation = SupplierItem(
        supplier_id=supplier.id, inventory_item_id=item_exacto.id,
        presentation="Bolsa x 500 g", conversion_factor=Decimal("500"),
        last_cost=Decimal("4000"), active=True, preferred=True,
    )
    db.add(relation)
    db.flush()
    return unit, item_exacto, supplier


def _cleanup(db, unit, item, supplier) -> None:
    """ORM `delete()`, no SQL crudo con nombres de tabla sin calificar -- un
    `text()` plano no pasa por el `schema_translate_map` de `with_db()` y
    apunta al esquema equivocado (o a ninguno)."""
    db.rollback()
    extra_codes = [*_codigos_extra]
    extra_ids = db.execute(
        select(InventoryItem.id).where(InventoryItem.code.in_(extra_codes))
    ).scalars().all() if extra_codes else []
    all_item_ids = [item.id, *extra_ids]

    all_supplier_ids = [supplier.id, *_proveedores_extra]
    db.execute(delete(SupplierItem).where(
        SupplierItem.inventory_item_id.in_(all_item_ids)
        | SupplierItem.supplier_id.in_(all_supplier_ids)
    ))
    if _job_ids:
        db.execute(delete(InventoryImportJob).where(InventoryImportJob.id.in_(_job_ids)))
    db.execute(delete(InventoryItem).where(InventoryItem.id.in_(all_item_ids)))
    db.execute(delete(Supplier).where(Supplier.id.in_(all_supplier_ids)))
    if _unidades_extra:
        db.execute(delete(UnitMeasure).where(UnitMeasure.id.in_(_unidades_extra)))
    db.commit()


def _row_by_header(headers: list[str], values: list, header: str):
    return values[headers.index(header)]


def test_export_incluye_codigo_y_proveedor_preferido(db, item, unit):
    """Historia 1: la plantilla trae código y, para un insumo con proveedor
    preferido, presentación/factor/último costo (escenario 1-2 de US1)."""
    items = [item]
    preferred = supplier_items.preferred_relations_by_item(db, [item.id])
    buffer = build_inventory_excel(items, {unit.id: unit}, preferred)

    import openpyxl
    wb = openpyxl.load_workbook(buffer)
    headers = [c.value for c in next(wb.active.iter_rows(min_row=1, max_row=1))]
    assert headers == INVENTORY_TEMPLATE_HEADERS, headers
    row = [c.value for c in next(wb.active.iter_rows(min_row=2, max_row=2))]
    assert _row_by_header(headers, row, "Código") == item.code
    assert _row_by_header(headers, row, "Proveedor preferido") == preferred[item.id]["supplier_name"]
    assert _row_by_header(headers, row, "Presentación") == preferred[item.id]["presentation"]
    print("OK: export_incluye_codigo_y_proveedor_preferido")


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
    ningún error y trata cada fila como actualización."""
    preferred = supplier_items.preferred_relations_by_item(db, [item.id])
    headers = INVENTORY_TEMPLATE_HEADERS
    fila = [
        item.code, item.name, "Materia prima", unit.abbreviation,
        float(item.current_stock), float(item.min_stock), float(item.unit_cost),
        preferred[item.id]["supplier_name"], preferred[item.id]["presentation"],
        float(preferred[item.id]["conversion_factor"]), float(preferred[item.id]["last_cost"]),
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
    fila = [item.code, item.name, "Materia prima", unit.abbreviation, "10", "2", str(nuevo_costo),
            "", "", "", ""]
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
    fila = ["", f"Nuevo-{uuid4().hex[:6]}", "Materia prima", unit.abbreviation, "0", "0", "0",
            "", "", "", ""]
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
    """Historia 2, escenario 3: costo no numérico, tipo inválido y factor de
    conversión inválido fallan cada una con un motivo propio, sin bloquear el
    resto. (Unidad/proveedor inexistentes ya NO son motivo de fallo -- se
    consolidan automáticamente, ver los tests de consolidación más abajo.)"""
    filas = [
        [f"T{uuid4().hex[:6]}", "Costo malo", "Materia prima", unit.abbreviation,
         "0", "0", "no-es-numero", "", "", "", ""],
        [f"T{uuid4().hex[:6]}", "Tipo malo", "Un tipo que no existe", unit.abbreviation,
         "0", "0", "0", "", "", "", ""],
        [f"T{uuid4().hex[:6]}", "Factor malo", "Materia prima", unit.abbreviation,
         "0", "0", "0", "Proveedor Nuevo SA", "Caja x 10", "0", ""],
    ]
    job = _job(db, uuid4(), len(filas))
    db.commit()
    procesar_archivo_importacion(db, job, filas)
    db.refresh(job)
    assert job.failed_count == 3, job.errors
    assert job.created_count == 0
    motivos = {e["motivo"] for e in job.errors}
    assert len(motivos) == 3, motivos
    print("OK: filas_con_errores_distintos_fallan_individualmente")


def test_unidad_inexistente_se_consolida_creandola_en_mayuscula(db):
    """Ajuste pedido por el usuario: una unidad que no existe ya no hace
    fallar la fila -- se crea automáticamente, siempre en mayúscula sin
    importar cómo venía escrita en el Excel."""
    etiqueta = f"lb-test-{uuid4().hex[:5]}"
    code = f"T{uuid4().hex[:6]}"
    fila = [code, f"Insumo en {etiqueta}", "Materia prima", etiqueta,
            "0", "0", "0", "", "", "", ""]
    job = _job(db, uuid4(), 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.created_count == 1, job.errors

    _codigos_extra.append(code)
    unidad = db.execute(
        select(UnitMeasure).where(UnitMeasure.abbreviation == etiqueta.upper())
    ).scalar_one_or_none()
    assert unidad is not None, "la unidad debía quedar creada en mayúscula"
    assert unidad.name == etiqueta.upper()
    _unidades_extra.append(unidad.id)
    print("OK: unidad_inexistente_se_consolida_creandola_en_mayuscula")


def test_unidad_existente_se_reconoce_sin_distinguir_mayusculas(db, unit):
    """La unidad del fixture (`unit.abbreviation`, normalmente en minúscula)
    debe reconocerse aunque el Excel la escriba en mayúscula/minúscula
    distinta -- no debe crear una unidad duplicada."""
    antes = db.execute(select(UnitMeasure.id)).scalars().all()
    code = f"T{uuid4().hex[:6]}"
    fila = [code, "Insumo con unidad en otro caso", "Materia prima",
            unit.abbreviation.swapcase(), "0", "0", "0", "", "", "", ""]
    job = _job(db, uuid4(), 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.created_count == 1, job.errors
    _codigos_extra.append(code)

    despues = db.execute(select(UnitMeasure.id)).scalars().all()
    assert set(despues) == set(antes), "no debía crear ninguna unidad nueva"
    print("OK: unidad_existente_se_reconoce_sin_distinguir_mayusculas")


def test_proveedor_inexistente_se_consolida_creandolo_en_mayuscula(db, unit):
    """Mismo ajuste para proveedores: uno que no existe se crea
    automáticamente en mayúscula, en vez de fallar la fila."""
    nombre = f"proveedor nuevo {uuid4().hex[:5]}"
    code = f"T{uuid4().hex[:6]}"
    fila = [code, "Insumo con proveedor nuevo", "Materia prima", unit.abbreviation,
            "0", "0", "0", nombre, "Caja x 10", "10", ""]
    job = _job(db, uuid4(), 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.created_count == 1, job.errors
    _codigos_extra.append(code)

    proveedor = db.execute(
        select(Supplier).where(Supplier.name == nombre.upper())
    ).scalar_one_or_none()
    assert proveedor is not None, "el proveedor debía quedar creado en mayúscula"
    assert proveedor.active and not proveedor.is_system
    _proveedores_extra.append(proveedor.id)
    print("OK: proveedor_inexistente_se_consolida_creandolo_en_mayuscula")


def test_proveedor_inactivo_se_reactiva_en_vez_de_duplicarse(db, unit):
    """Un proveedor ya existente pero inactivo, con el mismo nombre (sin
    distinguir mayúsculas), se reactiva en vez de crear un duplicado --
    `Supplier.name` no tiene restricción de unicidad, así que sin este
    cuidado se acumularían filas repetidas."""
    nombre = f"Proveedor Inactivo {uuid4().hex[:5]}"
    inactivo = Supplier(name=nombre, active=False, is_system=False)
    db.add(inactivo)
    db.flush()
    _proveedores_extra.append(inactivo.id)

    code = f"T{uuid4().hex[:6]}"
    fila = [code, "Insumo con proveedor reactivado", "Materia prima", unit.abbreviation,
            "0", "0", "0", nombre.upper(), "Caja x 10", "10", ""]
    job = _job(db, uuid4(), 1)
    db.commit()
    procesar_archivo_importacion(db, job, [fila])
    db.refresh(job)
    assert job.created_count == 1, job.errors
    _codigos_extra.append(code)

    db.refresh(inactivo)
    assert inactivo.active, "el proveedor existente debía reactivarse, no duplicarse"
    total_con_ese_nombre = db.execute(
        select(Supplier).where(func.upper(Supplier.name) == nombre.upper())
    ).scalars().all()
    assert len(total_con_ese_nombre) == 1, "no debía crear un proveedor duplicado"
    print("OK: proveedor_inactivo_se_reactiva_en_vez_de_duplicarse")


def test_estructura_alterada_no_crea_job():
    """Historia 2, escenario 4: columnas fuera de orden -> rechazo de estructura."""
    problemas = validar_estructura(["Nombre", "Código", "Tipo"])
    assert problemas, "se esperaba un problema de estructura"
    print("OK: estructura_alterada_no_crea_job")


def test_codigos_duplicados_en_el_archivo_fallan_ambas(db, unit):
    """Edge case del spec: dos filas con el mismo código en el mismo archivo
    fallan las dos, ninguna se aplica."""
    code = f"DUP-{uuid4().hex[:6]}"
    filas = [
        [code, "Primera", "Materia prima", unit.abbreviation, "0", "0", "0", "", "", "", ""],
        [code, "Segunda", "Materia prima", unit.abbreviation, "0", "0", "0", "", "", "", ""],
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
        [code_nuevo, item.name, "Materia prima", unit.abbreviation,
         "0", "0", "0", "", "", "", ""],
        [code_ok, f"Insumo-normal-{uuid4().hex[:6]}", "Materia prima", unit.abbreviation,
         "0", "0", "0", "", "", "", ""],
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
         "0", "0", "0", "", "", "", ""],
        [code_2, f"Despues-de-cancelar-{uuid4().hex[:6]}", "Materia prima", unit.abbreviation,
         "0", "0", "0", "", "", "", ""],
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
                      unit.abbreviation, "0", "0", "0", "", "", "", ""]
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
        unit, item, supplier = _fixture(db)
        db.commit()
        tests = [
            lambda: test_export_incluye_codigo_y_proveedor_preferido(db, item, unit),
            lambda: test_reimportar_archivo_exportado_no_falla_y_actualiza(db, item, unit),
            lambda: test_editar_costo_actualiza_el_insumo(db, item, unit),
            lambda: test_fila_nueva_con_codigo_vacio_crea_insumo_con_codigo_automatico(db, unit),
            lambda: test_filas_con_errores_distintos_fallan_individualmente(db, unit),
            test_estructura_alterada_no_crea_job,
            lambda: test_codigos_duplicados_en_el_archivo_fallan_ambas(db, unit),
            lambda: test_nombre_duplicado_contra_insumo_existente_falla_la_fila(db, unit, item),
            lambda: test_job_cancelado_a_mitad_de_archivo_detiene_las_filas_restantes(db, unit, schema),
            lambda: test_interrupcion_a_mitad_de_archivo_conserva_lo_ya_aplicado(db, unit),
            lambda: test_unidad_inexistente_se_consolida_creandola_en_mayuscula(db),
            lambda: test_unidad_existente_se_reconoce_sin_distinguir_mayusculas(db, unit),
            lambda: test_proveedor_inexistente_se_consolida_creandolo_en_mayuscula(db, unit),
            lambda: test_proveedor_inactivo_se_reactiva_en_vez_de_duplicarse(db, unit),
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
            _cleanup(db, unit, item, supplier)
        if fallos:
            raise SystemExit(f"{fallos}/{len(tests)} pruebas fallaron")
        print(f"Todas las {len(tests)} pruebas pasaron.")


if __name__ == "__main__":
    main()
