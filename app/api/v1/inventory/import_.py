"""Importación masiva de inventario (spec 109). Un archivo propio, separado de
`export.py` -- export e import comparten la definición de columnas
(`schemas.INVENTORY_TEMPLATE_HEADERS`) pero tienen validación y dependencias
distintas (plan.md, Structure Decision): el import depende de la tabla de jobs y,
en producción, de la tarea Celery que lo invoca; el export no depende de ninguno
de los dos.

Cada fila se valida y se aplica de forma independiente, con su propio commit --
nunca una transacción de todo el archivo (FR-006/FR-015, data-model.md): así, si
el proceso se interrumpe a mitad de camino, lo ya aplicado queda aplicado tal
cual, y reintentar resubiendo el mismo archivo no duplica nada (el emparejamiento
es por código)."""
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.timezone import utc_now
from app.models.unit_measure import UnitMeasure
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem
from app.api.v1.inventory import service, supplier_items
from app.api.v1.inventory.schemas import (
    INVENTORY_TEMPLATE_HEADERS, SupplierItemCreate, SupplierItemUpdate,
)

_TYPE_LABELS = {"Materia prima": "raw_material", "Empacado": "packaged"}

# Índices de columna dentro de una fila, en el mismo orden que
# INVENTORY_TEMPLATE_HEADERS (data-model.md).
_CODE, _NAME, _TYPE, _UNIT, _STOCK, _MIN, _COST, _SUPPLIER, _PRESENTATION, _FACTOR, _LAST_COST = range(11)


def validar_estructura(headers: list[Any]) -> list[str] | None:
    """FR-005: compara las cabeceras del archivo subido contra la plantilla única,
    nombre y orden exactos. Devuelve la lista de problemas, o `None` si
    corresponde."""
    if list(headers) == INVENTORY_TEMPLATE_HEADERS:
        return None
    problemas = []
    if len(headers) != len(INVENTORY_TEMPLATE_HEADERS):
        problemas.append(
            f"se esperaban {len(INVENTORY_TEMPLATE_HEADERS)} columnas, el archivo trae "
            f"{len(headers)}"
        )
    for i, esperado in enumerate(INVENTORY_TEMPLATE_HEADERS):
        recibido = headers[i] if i < len(headers) else None
        if recibido != esperado:
            problemas.append(
                f"columna {i + 1}: se esperaba '{esperado}', se encontró '{recibido}'"
            )
    return problemas


def _parse_decimal(valor: Any, campo: str) -> Decimal:
    if valor is None or valor == "":
        raise ValueError(f"{campo} es obligatorio")
    try:
        numero = Decimal(str(valor))
    except InvalidOperation as exc:
        raise ValueError(f"{campo} debe ser numérico, se recibió '{valor}'") from exc
    if numero < 0:
        raise ValueError(f"{campo} no puede ser negativo")
    return numero


def _resolve_unit(db: Session, nombre_unidad: Any) -> UnitMeasure:
    """Ajuste post-implementación (pedido explícito del usuario): una unidad que
    no existe ya no hace fallar la fila -- se consolida creándola. La comparación
    y el guardado son siempre en mayúscula, sin distinguir cómo venía escrita en
    el Excel (case-insensitive al buscar, canónico en mayúscula al crear)."""
    if not nombre_unidad or not str(nombre_unidad).strip():
        raise ValueError("Unidad es obligatoria")
    texto = str(nombre_unidad).strip()
    normalizado = texto.upper()

    candidatos = db.execute(
        select(UnitMeasure).where(func.upper(UnitMeasure.abbreviation) == normalizado)
    ).scalars().all()
    if len(candidatos) > 1:
        raise ValueError(
            f"hay más de una unidad que coincide con '{texto}' sin distinguir "
            f"mayúsculas/minúsculas; corrígelo manualmente antes de reimportar"
        )
    if candidatos:
        return candidatos[0]

    conflicto = db.execute(
        select(UnitMeasure).where(func.upper(UnitMeasure.name) == normalizado)
    ).scalar_one_or_none()
    if conflicto is not None:
        raise ValueError(
            f"ya existe una unidad llamada '{conflicto.name}' con una abreviatura distinta "
            f"('{conflicto.abbreviation}'); no se puede crear '{texto}' con ese mismo nombre"
        )

    unidad = UnitMeasure(name=normalizado, abbreviation=normalizado)
    db.add(unidad)
    db.flush()
    return unidad


def _resolve_supplier(db: Session, nombre_proveedor: str) -> Supplier:
    """Mismo ajuste que `_resolve_unit`, para proveedores: se consolida (busca
    sin distinguir mayúsculas, crea en mayúscula si no existe) en vez de fallar
    la fila. Un proveedor inactivo que coincida se reactiva en vez de crear un
    duplicado -- `Supplier.name` no tiene restricción de unicidad en el modelo,
    así que sin este cuidado se acumularían filas repetidas con el mismo
    nombre."""
    texto = nombre_proveedor.strip()
    normalizado = texto.upper()

    candidatos = db.execute(
        select(Supplier).where(func.upper(Supplier.name) == normalizado)
    ).scalars().all()
    if len(candidatos) > 1:
        raise ValueError(
            f"hay más de un proveedor que coincide con '{texto}' sin distinguir "
            f"mayúsculas/minúsculas; corrígelo manualmente antes de reimportar"
        )
    if candidatos:
        supplier = candidatos[0]
        if not supplier.active:
            supplier.active = True
            db.add(supplier)
            db.flush()
        return supplier

    supplier = Supplier(name=normalizado, is_system=False, active=True)
    db.add(supplier)
    db.flush()
    return supplier


def _resolve_type(valor: Any) -> str:
    tipo = _TYPE_LABELS.get(str(valor).strip() if valor else "")
    if tipo is None:
        raise ValueError(
            f"Tipo debe ser 'Materia prima' o 'Empacado', se recibió '{valor}'"
        )
    return tipo


def _apply_preferred_supplier(db: Session, item, fila: list[Any]) -> None:
    """Resuelve/crea la relación proveedor-insumo preferida de la fila (columnas
    Proveedor preferido/Presentación/Factor de conversión/Último costo). Si la
    columna de proveedor viene vacía, no toca nada (Edge case del spec: no borra
    un proveedor preferido existente solo porque la fila llegó sin esos
    datos)."""
    nombre_proveedor = fila[_SUPPLIER]
    if not nombre_proveedor or not str(nombre_proveedor).strip():
        return

    supplier = _resolve_supplier(db, str(nombre_proveedor))

    presentacion = fila[_PRESENTATION]
    if not presentacion:
        raise ValueError("Presentación es obligatoria cuando se indica un proveedor preferido")
    factor = _parse_decimal(fila[_FACTOR], "Factor de conversión")
    if factor <= 0:
        raise ValueError("Factor de conversión debe ser mayor que 0")
    ultimo_costo = None
    if fila[_LAST_COST] not in (None, ""):
        ultimo_costo = _parse_decimal(fila[_LAST_COST], "Último costo")

    presentacion = str(presentacion).strip()
    existing = db.execute(
        select(SupplierItem).where(
            SupplierItem.supplier_id == supplier.id,
            SupplierItem.inventory_item_id == item.id,
            func.lower(func.trim(SupplierItem.presentation)) == presentacion.lower(),
        )
    ).scalar_one_or_none()

    try:
        if existing is not None and existing.active:
            # Re-importar un archivo ya exportado no debe chocar con la relación
            # que el propio export leyó -- se actualiza en vez de intentar crear
            # una duplicada (create_or_reactivate la rechazaría con 409).
            supplier_items.update(db, existing.id, SupplierItemUpdate(
                conversion_factor=factor, last_cost=ultimo_costo, preferred=True,
            ))
        else:
            relation, _status = supplier_items.create_or_reactivate(
                db, supplier_id=supplier.id, inventory_item_id=item.id,
                body=SupplierItemCreate(
                    presentation=presentacion, conversion_factor=factor,
                    last_cost=ultimo_costo,
                ),
            )
            if not relation.preferred:
                supplier_items.set_preferred(db, relation.id)
    except HTTPException as exc:
        detail = exc.detail["detail"] if isinstance(exc.detail, dict) else exc.detail
        raise ValueError(str(detail)) from exc


def validar_y_aplicar_fila(db: Session, fila: list[Any]) -> dict:
    """data-model.md, reglas de validación: valida y aplica una fila de forma
    independiente, con su propio commit. Devuelve
    `{ok, accion, motivo}` -- `accion` es "creado"/"actualizado" cuando `ok` es
    `True`."""
    try:
        tipo = _resolve_type(fila[_TYPE])
        unidad = _resolve_unit(db, fila[_UNIT])
        nombre = fila[_NAME]
        if not nombre:
            raise ValueError("Nombre es obligatorio")
        stock = _parse_decimal(fila[_STOCK], "Stock")
        minimo = _parse_decimal(fila[_MIN], "Mínimo")
        costo = _parse_decimal(fila[_COST], "Costo")
        code = str(fila[_CODE]).strip() if fila[_CODE] else None

        item, creado = service.resolve_or_create_by_code(
            db, code,
            name=str(nombre).strip(), type=tipo, unit_measure_id=unidad.id,
            current_stock=stock, min_stock=minimo, unit_cost=costo,
        )
        _apply_preferred_supplier(db, item, fila)
        db.commit()
        return {"ok": True, "accion": "creado" if creado else "actualizado", "motivo": None}
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "accion": None, "motivo": str(exc)}


def procesar_archivo_importacion(db: Session, job, filas: list[list[Any]]) -> None:
    """Recorre las filas de un archivo ya validado estructuralmente, aplica cada
    una de forma independiente y deja el `job` (`InventoryImportJob`) en
    `finalizado` con los conteos y errores acumulados (FR-006/FR-011/FR-012).

    Pre-escanea los códigos del archivo para detectar duplicados *dentro del
    mismo archivo* (Edge case del spec: ambas filas fallan, ninguna se aplica) --
    una vez committeada la primera, `resolve_or_create_by_code` ya no vería un
    duplicado como tal, sino como una actualización legítima.

    Cancelación cooperativa: cada fila hace su propio commit, lo que expira
    `job` en la sesión (expire_on_commit por defecto) -- así, al releer
    `job.status` al principio de la siguiente vuelta, se ve el valor que haya
    dejado otra sesión (p. ej. el endpoint de cancelar). Si alguien marcó el
    job como ya no `procesando`, el loop corta aquí mismo y nunca llega al
    bloque final que lo pondría en `finalizado`, dejando el status que haya
    puesto quien canceló."""
    codigos_vistos: dict[str, int] = {}
    for numero, fila in enumerate(filas, start=1):
        code = str(fila[_CODE]).strip() if fila[_CODE] else None
        if code:
            codigos_vistos[code] = codigos_vistos.get(code, 0) + 1
    duplicados = {code for code, veces in codigos_vistos.items() if veces > 1}

    for numero, fila in enumerate(filas, start=1):
        if job.status != "procesando":
            return
        code = str(fila[_CODE]).strip() if fila[_CODE] else None
        if code and code in duplicados:
            resultado = {
                "ok": False,
                "motivo": f"el código '{code}' aparece más de una vez en este archivo",
            }
        else:
            resultado = validar_y_aplicar_fila(db, fila)

        if resultado["ok"]:
            if resultado["accion"] == "creado":
                job.created_count += 1
            else:
                job.updated_count += 1
        else:
            job.failed_count += 1
            job.errors = [*job.errors, {
                "fila": numero, "codigo": code, "motivo": resultado["motivo"],
            }]
        db.add(job)
        db.commit()

    job.status = "finalizado"
    job.finished_at = utc_now().replace(tzinfo=None)
    db.commit()
