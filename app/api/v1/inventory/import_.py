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

from app.core.timezone import utc_now_naive
from app.models.inventory_item import InventoryItem
from app.models.unit_measure import UnitMeasure
from app.models.unit_measure_group import UnitMeasureGroup
from app.models.supplier import Supplier
from app.models.supplier_item import SupplierItem
from app.api.v1.inventory import service, supplier_items
from app.api.v1.inventory.schemas import INVENTORY_TEMPLATE_HEADERS

_TYPE_LABELS = {"Materia prima": "raw_material", "Empacado": "packaged"}

# Índices de columna dentro de una fila, en el mismo orden que
# INVENTORY_TEMPLATE_HEADERS (data-model.md): las últimas 3 columnas de la
# plantilla previa a la spec 138 (presentación/factor/último costo) ya no
# existen -- solo el nombre del proveedor preferido se conserva.
_CODE, _NAME, _TYPE, _UNIT, _STOCK, _MIN, _COST, _SUPPLIER = range(8)

# spec 109: plantilla previa a la spec 138, con 3 columnas más al final
# (presentación/factor de conversión/último costo). Un archivo exportado antes
# de la spec 138 sigue aceptándose con esta forma (FR-003); el contenido de
# esas 3 columnas se ignora por completo (FR-004) -- por eso ningún índice las
# referencia.
_LEGACY_TEMPLATE_HEADERS = [
    *INVENTORY_TEMPLATE_HEADERS,
    "Presentación", "Factor de conversión", "Último costo",
]


def validar_estructura(headers: list[Any]) -> list[str] | None:
    """FR-005/FR-003: compara las cabeceras del archivo subido contra la plantilla
    vigente (8 columnas) o la plantilla legada previa a la spec 138 (11 columnas),
    nombre y orden exactos. Devuelve la lista de problemas, o `None` si
    corresponde."""
    headers = list(headers)
    if headers == INVENTORY_TEMPLATE_HEADERS or headers == _LEGACY_TEMPLATE_HEADERS:
        return None
    esperado = (
        _LEGACY_TEMPLATE_HEADERS if len(headers) == len(_LEGACY_TEMPLATE_HEADERS)
        else INVENTORY_TEMPLATE_HEADERS
    )
    problemas = []
    if len(headers) != len(esperado):
        problemas.append(
            f"se esperaban {len(INVENTORY_TEMPLATE_HEADERS)} columnas, el archivo trae "
            f"{len(headers)}"
        )
    for i, nombre_esperado in enumerate(esperado):
        recibido = headers[i] if i < len(headers) else None
        if recibido != nombre_esperado:
            problemas.append(
                f"columna {i + 1}: se esperaba '{nombre_esperado}', se encontró '{recibido}'"
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
    """FR-006/FR-008/FR-009: vincula la columna Unidad de una fila que crea un
    insumo nuevo a una `UnitMeasure` existente del negocio (case-insensitive por
    `abbreviation`) o crea una completa -- grupo propio nuevo, unidad base de ese
    grupo, factor 1 -- si ninguna coincide. Solo se llama al crear: una fila que
    actualiza un insumo existente nunca pasa por aquí (FR-007, ver
    `validar_y_aplicar_fila`), así que no hay riesgo de recrear la unidad de un
    insumo ya registrado. La comparación y el guardado son siempre en mayúscula,
    sin distinguir cómo venía escrita en el Excel."""
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

    # Spec 137/139: `group_id` es NOT NULL -- toda unidad nueva necesita su propio
    # grupo antes de poder crearse (mismo patrón que
    # unit_measure_groups.router.create_unit_measure_group).
    grupo = UnitMeasureGroup(name=normalizado)
    db.add(grupo)
    db.flush()

    unidad = UnitMeasure(
        name=normalizado, abbreviation=normalizado,
        group_id=grupo.id, is_base=True, factor_to_base=1,
    )
    db.add(unidad)
    db.flush()
    return unidad


def _resolve_type(valor: Any) -> str:
    tipo = _TYPE_LABELS.get(str(valor).strip() if valor else "")
    if tipo is None:
        raise ValueError(
            f"Tipo debe ser 'Materia prima' o 'Empacado', se recibió '{valor}'"
        )
    return tipo


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


def _apply_preferred_supplier(db: Session, item: InventoryItem, fila: list[Any]) -> None:
    """Spec 138 (ajuste pedido por el usuario): la columna Proveedor preferido
    vincula (o crea) el proveedor y marca como preferida su relación con este
    insumo -- creándola si no existía todavía, ya que la plantilla ya no trae
    presentación ni factor de conversión (FR-001/FR-002), esa relación nueva
    queda con esos dos campos en `NULL` hasta que el administrador la completa
    desde el módulo de proveedores (estado intermedio, a refinar en una spec
    futura). Si la columna viene vacía, no toca nada (no borra la preferida
    actual). Nunca modifica presentación/factor/último costo de una relación ya
    existente."""
    if len(fila) <= _SUPPLIER:
        return
    nombre_proveedor = fila[_SUPPLIER]
    if not nombre_proveedor or not str(nombre_proveedor).strip():
        return

    supplier = _resolve_supplier(db, str(nombre_proveedor))

    candidatos = db.execute(
        select(SupplierItem).where(
            SupplierItem.inventory_item_id == item.id,
            SupplierItem.supplier_id == supplier.id,
            SupplierItem.active.is_(True),
        )
    ).scalars().all()

    if len(candidatos) > 1:
        raise ValueError(
            f"hay más de una relación activa con el proveedor '{supplier.name}' para "
            f"este insumo; resuélvelo desde el módulo de proveedores antes de reimportar"
        )

    if candidatos:
        relation = candidatos[0]
        if relation.preferred:
            return
    else:
        relation = SupplierItem(
            supplier_id=supplier.id, inventory_item_id=item.id,
            presentation=None, conversion_factor=None, active=True,
        )
        db.add(relation)
        db.flush()

    try:
        supplier_items.set_preferred(db, relation.id)
    except HTTPException as exc:
        detail = exc.detail["detail"] if isinstance(exc.detail, dict) else exc.detail
        raise ValueError(str(detail)) from exc


def validar_y_aplicar_fila(db: Session, fila: list[Any]) -> dict:
    """data-model.md, reglas de validación: valida y aplica una fila de forma
    independiente, con su propio commit. Devuelve
    `{ok, accion, motivo}` -- `accion` es "creado"/"actualizado" cuando `ok` es
    `True`.

    FR-007: la columna Unidad solo se usa al crear un insumo nuevo -- en una fila
    que actualiza un insumo existente (código ya registrado), se ignora por
    completo, sin resolver ni crear ninguna `UnitMeasure`."""
    try:
        tipo = _resolve_type(fila[_TYPE])
        nombre = fila[_NAME]
        if not nombre:
            raise ValueError("Nombre es obligatorio")
        stock = _parse_decimal(fila[_STOCK], "Stock")
        minimo = _parse_decimal(fila[_MIN], "Mínimo")
        costo = _parse_decimal(fila[_COST], "Costo")
        code = str(fila[_CODE]).strip() if fila[_CODE] else None

        existente = None
        if code:
            existente = db.execute(
                select(InventoryItem).where(InventoryItem.code == code)
            ).scalar_one_or_none()

        campos = dict(
            name=str(nombre).strip(), type=tipo,
            current_stock=stock, min_stock=minimo, unit_cost=costo,
        )
        if existente is None:
            campos["unit_measure_id"] = _resolve_unit(db, fila[_UNIT]).id

        item, creado = service.resolve_or_create_by_code(db, code, **campos)
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
    job.finished_at = utc_now_naive()
    db.commit()
