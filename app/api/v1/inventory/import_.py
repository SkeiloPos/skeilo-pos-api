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

# spec 139: plantilla previa a esta spec, sin Grupo/Unidad de compra/Factor de
# conversión (compra) -- un archivo exportado entre la spec 139 y esta spec
# sigue aceptándose con esta forma (contracts/plantilla-excel-inventario.md).
_FORMAT_8_HEADERS = [
    "Código", "Nombre", "Tipo", "Unidad", "Stock", "Mínimo", "Costo",
    "Proveedor preferido",
]

# spec 109: plantilla legada previa a la spec 138, con 3 columnas más al final
# (presentación/factor de conversión/último costo). Un archivo exportado antes
# de la spec 138 sigue aceptándose con esta forma (FR-003); el contenido de
# esas 3 columnas se ignora por completo (FR-004).
_FORMAT_11_HEADERS = [
    *_FORMAT_8_HEADERS,
    "Presentación", "Factor de conversión", "Último costo",
]

# Índices de columna por formato de encabezado aceptado (data-model.md): el
# formato vigente (`INVENTORY_TEMPLATE_HEADERS`) trae Grupo y las columnas de
# unidad de compra; el formato de la spec 139 (8 columnas, sin esas 3) comparte
# los mismos índices para lo que sí tiene -- `None` marca una columna que no
# existe en ese formato. El formato legado de la spec 109 (11 columnas, con
# Presentación/Factor de conversión/Último costo en vez de Grupo/Unidad de
# compra/Factor de conversión (compra)) coincide en longitud con el formato
# vigente -- `normalizar_filas()` lo recorta a la forma de 8 columnas antes de
# que una fila individual llegue aquí, así que nunca hay ambigüedad real por
# tamaño en este punto.
_IDX_VIGENTE = {
    "CODE": 0, "NAME": 1, "TYPE": 2, "UNIT": 3, "GROUP": 4, "STOCK": 5, "MIN": 6,
    "COST": 7, "SUPPLIER": 8, "PURCHASE_UNIT": 9, "PURCHASE_FACTOR": 10,
}
_IDX_FORMATO_8 = {
    "CODE": 0, "NAME": 1, "TYPE": 2, "UNIT": 3, "GROUP": None, "STOCK": 4, "MIN": 5,
    "COST": 6, "SUPPLIER": 7, "PURCHASE_UNIT": None, "PURCHASE_FACTOR": None,
}


def _indices(fila_len: int) -> dict[str, int | None]:
    return _IDX_VIGENTE if fila_len == len(INVENTORY_TEMPLATE_HEADERS) else _IDX_FORMATO_8


def _col(fila: list[Any], idx: dict[str, int | None], key: str) -> Any:
    i = idx[key]
    if i is None or i >= len(fila):
        return None
    return fila[i]


def normalizar_filas(headers: list[Any], filas: list[list[Any]]) -> list[list[Any]]:
    """Router.py llama a esto justo después de que `validar_estructura` acepte el
    archivo, antes de encolar las filas para su procesamiento: el formato legado
    de la spec 109 (11 columnas) coincide en longitud con el formato vigente
    (también 11), pero sus últimas 3 columnas (Presentación/Factor de
    conversión/Último costo) no son Grupo/Unidad de compra/Factor de conversión
    (compra) -- se descartan aquí (ya se ignoraban por completo, FR-004),
    dejando la fila con la misma forma de 8 columnas que el formato de la spec
    139. Así, el resto del módulo (`_indices`) nunca necesita adivinar el
    formato de origen de una fila ambigua por tamaño."""
    if list(headers) == _FORMAT_11_HEADERS:
        return [list(fila[:8]) for fila in filas]
    return filas


def validar_estructura(headers: list[Any]) -> list[str] | None:
    """FR-005/FR-003: compara las cabeceras del archivo subido contra la plantilla
    vigente, la plantilla de la spec 139 (8 columnas) o la plantilla legada
    previa a la spec 138 (11 columnas), nombre y orden exactos. Devuelve la
    lista de problemas, o `None` si corresponde a alguna de las tres."""
    headers = list(headers)
    formatos_validos = (INVENTORY_TEMPLATE_HEADERS, _FORMAT_8_HEADERS, _FORMAT_11_HEADERS)
    if headers in formatos_validos:
        return None
    esperado = next(
        (f for f in formatos_validos if len(f) == len(headers)), INVENTORY_TEMPLATE_HEADERS,
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


def _resolve_or_create_group(db: Session, nombre_normalizado: str) -> UnitMeasureGroup:
    """FR-010: reutiliza un `UnitMeasureGroup` existente por `name` case-insensitive,
    o lo crea si no existe todavía."""
    grupo = db.execute(
        select(UnitMeasureGroup).where(func.upper(UnitMeasureGroup.name) == nombre_normalizado)
    ).scalar_one_or_none()
    if grupo is not None:
        return grupo
    grupo = UnitMeasureGroup(name=nombre_normalizado)
    db.add(grupo)
    db.flush()
    return grupo


def _resolve_unit(db: Session, nombre_unidad: Any, nombre_grupo: Any = None) -> UnitMeasure:
    """FR-006/FR-008/FR-009/FR-010/FR-012: vincula la columna Unidad de una fila que
    crea un insumo nuevo a una `UnitMeasure` existente del negocio (case-insensitive
    por `abbreviation`) o crea una completa -- grupo nuevo o reutilizado según la
    columna Grupo, unidad base de ese grupo, factor 1 -- si ninguna coincide. Solo se
    llama al crear: una fila que actualiza un insumo existente nunca pasa por aquí
    (FR-007/FR-013, ver `validar_y_aplicar_fila`), así que no hay riesgo de recrear la
    unidad de un insumo ya registrado. La comparación y el guardado son siempre en
    mayúscula, sin distinguir cómo venía escrita en el Excel."""
    if not nombre_unidad or not str(nombre_unidad).strip():
        raise ValueError("Unidad es obligatoria")
    texto = str(nombre_unidad).strip()
    normalizado = texto.upper()
    grupo_texto = str(nombre_grupo).strip() if nombre_grupo and str(nombre_grupo).strip() else None
    grupo_normalizado = grupo_texto.upper() if grupo_texto else None

    candidatos = db.execute(
        select(UnitMeasure).where(func.upper(UnitMeasure.abbreviation) == normalizado)
    ).scalars().all()
    if len(candidatos) > 1:
        raise ValueError(
            f"hay más de una unidad que coincide con '{texto}' sin distinguir "
            f"mayúsculas/minúsculas; corrígelo manualmente antes de reimportar"
        )
    if candidatos:
        unidad = candidatos[0]
        if grupo_normalizado is not None and unidad.group.name.upper() != grupo_normalizado:
            raise ValueError(
                f"la unidad '{texto}' ya existe en el grupo '{unidad.group.name}', distinto "
                f"al grupo '{grupo_texto}' indicado; corrígelo manualmente antes de reimportar"
            )
        return unidad

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
    if grupo_normalizado is not None:
        grupo = _resolve_or_create_group(db, grupo_normalizado)
        existing_base = next((u for u in grupo.units if u.is_base), None)
        if existing_base is not None:
            raise ValueError(
                f"el grupo '{grupo.name}' ya tiene una unidad base ('{existing_base.abbreviation}'); "
                f"no se puede crear otra unidad base '{texto}' en el mismo grupo"
            )
    else:
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


def _resolve_purchase_unit(
    db: Session, group_id: Any, nombre_unidad: Any, factor: Any,
) -> UnitMeasure:
    """research.md D7/data-model.md: resuelve o crea la `UnitMeasure` de la unidad de
    compra del proveedor preferido, acotada al mismo `group_id` que la unidad base del
    insumo (FR-015/FR-016/FR-017/FR-018). Solo se llama al crear un insumo nuevo con
    `Proveedor preferido` y `Unidad de compra` ambos presentes."""
    texto = str(nombre_unidad).strip()
    normalizado = texto.upper()
    factor_valor = None
    if factor is not None and str(factor).strip():
        factor_valor = _parse_decimal(factor, "Factor de conversión (compra)")

    en_grupo = db.execute(
        select(UnitMeasure).where(
            UnitMeasure.group_id == group_id,
            UnitMeasure.active.is_(True),
            (func.upper(UnitMeasure.abbreviation) == normalizado)
            | (func.upper(UnitMeasure.name) == normalizado),
        )
    ).scalars().all()
    if len(en_grupo) > 1:
        raise ValueError(
            f"hay más de una unidad en el grupo que coincide con '{texto}' sin distinguir "
            f"mayúsculas/minúsculas; corrígelo manualmente antes de reimportar"
        )
    if en_grupo:
        unidad = en_grupo[0]
        if factor_valor is not None and factor_valor != unidad.factor_to_base:
            raise ValueError(
                f"la unidad de compra '{texto}' ya existe con un factor de conversión de "
                f"{unidad.factor_to_base}, distinto al indicado ({factor_valor}); no se "
                f"modifica el factor ya vigente"
            )
        return unidad

    en_otro_grupo = db.execute(
        select(UnitMeasure).where(
            UnitMeasure.group_id != group_id,
            UnitMeasure.active.is_(True),
            (func.upper(UnitMeasure.abbreviation) == normalizado)
            | (func.upper(UnitMeasure.name) == normalizado),
        )
    ).scalars().all()
    if en_otro_grupo:
        raise ValueError(
            f"ya existe una unidad llamada '{texto}' en otro grupo distinto al de la unidad "
            f"base de este insumo; no se puede usar como unidad de compra"
        )

    if factor_valor is None:
        raise ValueError(
            f"Factor de conversión (compra) es obligatorio para crear la unidad de compra "
            f"'{texto}'"
        )

    unidad = UnitMeasure(
        name=normalizado, abbreviation=normalizado,
        group_id=group_id, is_base=False, factor_to_base=factor_valor,
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


def _apply_preferred_supplier(
    db: Session, item: InventoryItem, fila: list[Any], idx: dict[str, int | None], *, creado: bool,
) -> None:
    """Spec 138 (ajuste pedido por el usuario): la columna Proveedor preferido
    vincula (o crea) el proveedor y marca como preferida su relación con este
    insumo -- creándola si no existía todavía. Spec 142, solo al **crear** un
    insumo nuevo (`creado=True`, FR-021: al actualizar estas columnas se ignoran
    igual que ya se ignoraba Unidad): si la fila trae también `Unidad de compra`,
    la relación nueva queda con `unit_measure_id` resuelto por
    `_resolve_purchase_unit` en vez de con `presentation`/`conversion_factor` en
    `NULL` (FR-015/FR-019); sin unidad de compra, ambos campos quedan en `NULL`
    como antes (FR-020). Si la columna Proveedor preferido viene vacía, no toca
    nada (no borra la preferida actual) -- salvo que la fila traiga unidad de
    compra sin proveedor, lo que falla la fila completa (FR-020a). Nunca modifica
    presentación/factor/último costo/unidad de compra de una relación ya
    existente."""
    nombre_proveedor = _col(fila, idx, "SUPPLIER")
    tiene_proveedor = bool(nombre_proveedor and str(nombre_proveedor).strip())

    nombre_unidad_compra = _col(fila, idx, "PURCHASE_UNIT") if creado else None
    factor_compra = _col(fila, idx, "PURCHASE_FACTOR") if creado else None
    tiene_unidad_compra = bool(nombre_unidad_compra and str(nombre_unidad_compra).strip())
    tiene_factor_compra = factor_compra is not None and str(factor_compra).strip() != ""

    if not tiene_proveedor:
        if tiene_unidad_compra or tiene_factor_compra:
            raise ValueError(
                "Unidad de compra / Factor de conversión (compra) requieren que la fila "
                "también traiga Proveedor preferido"
            )
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

    resolved_unit = None
    if tiene_unidad_compra:
        resolved_unit = _resolve_purchase_unit(
            db, item.unit_measure.group_id, nombre_unidad_compra, factor_compra,
        )

    if candidatos:
        relation = candidatos[0]
        if relation.preferred:
            return
    else:
        relation = SupplierItem(
            supplier_id=supplier.id, inventory_item_id=item.id,
            unit_measure_id=resolved_unit.id if resolved_unit is not None else None,
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

    FR-007/FR-013: las columnas Unidad y Grupo solo se usan al crear un insumo
    nuevo -- en una fila que actualiza un insumo existente (código ya registrado),
    se ignoran por completo, sin resolver ni crear ninguna `UnitMeasure`/
    `UnitMeasureGroup`. Lo mismo aplica a Unidad de compra/Factor de conversión
    (compra), vía el `creado=False` que recibe `_apply_preferred_supplier`
    (FR-021)."""
    idx = _indices(len(fila))
    try:
        tipo = _resolve_type(_col(fila, idx, "TYPE"))
        nombre = _col(fila, idx, "NAME")
        if not nombre:
            raise ValueError("Nombre es obligatorio")
        stock = _parse_decimal(_col(fila, idx, "STOCK"), "Stock")
        minimo = _parse_decimal(_col(fila, idx, "MIN"), "Mínimo")
        costo = _parse_decimal(_col(fila, idx, "COST"), "Costo")
        code_valor = _col(fila, idx, "CODE")
        code = str(code_valor).strip() if code_valor else None

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
            campos["unit_measure_id"] = _resolve_unit(
                db, _col(fila, idx, "UNIT"), _col(fila, idx, "GROUP"),
            ).id

        item, creado = service.resolve_or_create_by_code(db, code, **campos)
        _apply_preferred_supplier(db, item, fila, idx, creado=creado)
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
    # El código (columna 1) está en el mismo índice en los tres formatos aceptados.
    codigos_vistos: dict[str, int] = {}
    for numero, fila in enumerate(filas, start=1):
        code = str(fila[0]).strip() if fila[0] else None
        if code:
            codigos_vistos[code] = codigos_vistos.get(code, 0) + 1
    duplicados = {code for code, veces in codigos_vistos.items() if veces > 1}

    for numero, fila in enumerate(filas, start=1):
        if job.status != "procesando":
            return
        code = str(fila[0]).strip() if fila[0] else None
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
