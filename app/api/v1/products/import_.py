"""Importación masiva de la carta (spec 143). Un archivo propio, separado de
`export.py` -- ambos comparten la definición de columnas
(`schemas.PRODUCT_IMPORT_SHEETS`) pero tienen validación y dependencias
distintas (plan.md, Structure Decision): el import depende de la tabla de jobs
y, en producción, de la tarea Celery que lo invoca; el export no depende de
ninguno de los dos.

Cada fila se valida y se aplica de forma independiente, con su propio commit --
nunca una transacción de todo el archivo (FR-005a/FR-015, data-model.md): así,
si el proceso se interrumpe a mitad de camino, lo ya aplicado queda aplicado tal
cual, y reintentar resubiendo el mismo archivo no duplica nada (el
emparejamiento es por código). El import NO reutiliza `ProductService`
(research.md D1): opera directamente sobre `Product`/`ProductVariant`/
`RecipeItem`/`ProductTopping`.

Las hojas se procesan completas, una por una, en el orden Productos →
Presentaciones y precios → Receta → Adiciones (FR-007, research.md D4) -- una
fila de una hoja posterior puede referenciar una fila creada (y ya committeada)
por una fila anterior de una hoja previa del mismo archivo, sin necesidad de un
registro en memoria: para cuando se procesa "Presentaciones y precios", todo lo
que "Productos" logró aplicar ya está en la base de datos."""
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.core.timezone import utc_now_naive
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.recipe_item import RecipeItem
from app.models.product_topping import ProductTopping
from app.models.category import Category
from app.models.presentation import Presentation
from app.models.inventory_item import InventoryItem
from app.api.v1.products import service
from app.api.v1.products.schemas import PRODUCT_IMPORT_SHEETS

_PREPARATION_LABELS = {"Preparado": "prepared", "Empacado": "packaged"}
_BOOL_TRUE = {"sí", "si", "verdadero", "true", "1", "x"}
_BOOL_FALSE = {"no", "falso", "false", "0"}

# Orden de procesamiento fijo (FR-007, research.md D4) -- independiente del
# orden físico de las hojas en el archivo subido.
_ORDEN_HOJAS = (
    "Productos", "Presentaciones y precios", "Receta", "Adiciones",
)


def validar_estructura(hojas_headers: dict[str, list[Any]]) -> list[str] | None:
    """FR-010: compara las cabeceras de cada hoja presente contra la plantilla
    correspondiente (nombre y orden exactos). Un archivo con solo algunas de
    las 4 hojas es válido (edge case del spec); una hoja con un nombre que no
    corresponde a ninguna de las 4 esperadas se trata como una hoja renombrada
    -- no como una hoja simplemente ausente -- y es un problema de estructura.
    Devuelve la lista de problemas, o `None` si el archivo es válido."""
    problemas: list[str] = []
    encontro_alguna = False
    for nombre_hoja, headers in hojas_headers.items():
        esperado = PRODUCT_IMPORT_SHEETS.get(nombre_hoja)
        if esperado is None:
            problemas.append(f"hoja desconocida: '{nombre_hoja}'")
            continue
        encontro_alguna = True
        headers = list(headers)
        if headers == esperado:
            continue
        if len(headers) != len(esperado):
            problemas.append(
                f"hoja '{nombre_hoja}': se esperaban {len(esperado)} columnas, "
                f"se encontraron {len(headers)}"
            )
        for i, nombre_esperado in enumerate(esperado):
            recibido = headers[i] if i < len(headers) else None
            if recibido != nombre_esperado:
                problemas.append(
                    f"hoja '{nombre_hoja}', columna {i + 1}: se esperaba "
                    f"'{nombre_esperado}', se encontró '{recibido}'"
                )
    if not encontro_alguna:
        problemas.append(
            "el archivo no contiene ninguna de las hojas esperadas (Productos, "
            "Presentaciones y precios, Receta, Adiciones)"
        )
    return problemas or None


def _parse_bool(valor: Any, campo: str) -> bool | None:
    """`None` = columna vacía -- el llamador decide si eso significa "conservar
    el valor actual" (actualización) o "usar el default de creación" (FR-005a,
    data-model.md)."""
    if valor is None or str(valor).strip() == "":
        return None
    texto = str(valor).strip().lower()
    if texto in _BOOL_TRUE:
        return True
    if texto in _BOOL_FALSE:
        return False
    raise ValueError(f"{campo} debe ser un valor booleano (Sí/No), se recibió '{valor}'")


def _parse_decimal(valor: Any, campo: str, *, allow_zero: bool = True) -> Decimal:
    if valor is None or valor == "":
        raise ValueError(f"{campo} es obligatorio")
    try:
        numero = Decimal(str(valor))
    except InvalidOperation as exc:
        raise ValueError(f"{campo} debe ser numérico, se recibió '{valor}'") from exc
    if numero < 0:
        raise ValueError(f"{campo} no puede ser negativo")
    if not allow_zero and numero <= 0:
        raise ValueError(f"{campo} debe ser mayor que cero")
    return numero


def _parse_int(valor: Any, campo: str) -> int:
    if valor is None or str(valor).strip() == "":
        raise ValueError(f"{campo} es obligatorio")
    try:
        numero = int(Decimal(str(valor)))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{campo} debe ser un entero, se recibió '{valor}'") from exc
    if numero < 0:
        raise ValueError(f"{campo} no puede ser negativo")
    return numero


def resolve_or_create_category(db: Session, nombre: Any) -> Category:
    """research.md D5: busca una categoría existente por nombre case-insensitive;
    la crea (con el texto exacto del archivo, sin normalizar a mayúscula --
    visible para el comensal) si no hay coincidencia. Reactiva una coincidencia
    inactiva. Falla si hay más de una coincidencia ambigua."""
    texto = str(nombre).strip()
    candidatos = db.execute(
        select(Category).where(func.lower(Category.name) == texto.lower())
    ).scalars().all()
    if len(candidatos) > 1:
        raise ValueError(
            f"hay más de una categoría que coincide con '{texto}' sin distinguir "
            f"mayúsculas/minúsculas; corrígelo manualmente antes de reimportar"
        )
    if candidatos:
        categoria = candidatos[0]
        if not categoria.active:
            categoria.active = True
            db.add(categoria)
            db.flush()
        return categoria

    max_order = db.execute(select(func.max(Category.display_order))).scalar() or 0
    categoria = Category(
        name=texto, active=True, visible_en_qr=True, display_order=max_order + 1,
    )
    db.add(categoria)
    db.flush()
    return categoria


def resolve_or_create_presentation(db: Session, nombre: Any) -> Presentation:
    """research.md D5: mismo criterio que `resolve_or_create_category`, aplicado
    a `presentations.name`."""
    texto = str(nombre).strip()
    candidatos = db.execute(
        select(Presentation).where(func.lower(Presentation.name) == texto.lower())
    ).scalars().all()
    if len(candidatos) > 1:
        raise ValueError(
            f"hay más de una presentación que coincide con '{texto}' sin distinguir "
            f"mayúsculas/minúsculas; corrígelo manualmente antes de reimportar"
        )
    if candidatos:
        presentacion = candidatos[0]
        if not presentacion.active:
            presentacion.active = True
            db.add(presentacion)
            db.flush()
        return presentacion

    presentacion = Presentation(name=texto, active=True)
    db.add(presentacion)
    db.flush()
    return presentacion


def _ensure_sku_unique(db: Session, sku: str, exclude_id=None) -> None:
    stmt = select(ProductVariant.id).where(ProductVariant.sku == sku)
    if exclude_id is not None:
        stmt = stmt.where(ProductVariant.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise ValueError(f"el SKU '{sku}' ya está en uso por otra presentación")


def _assign_variant_display_order(db: Session, variant: ProductVariant, desired_order: int) -> None:
    """data-model.md: si `desired_order` coincide con el de otra variante del
    mismo producto, se reasigna esa otra (y la cadena que siga) corriéndola
    hacia adelante -- igual criterio (dos pasadas, negativos primero) que
    `catalog.service._assign_display_orders` -- en vez de fallar la fila."""
    if variant.display_order == desired_order:
        return
    hay_conflicto = db.execute(
        select(ProductVariant.id).where(
            ProductVariant.product_id == variant.product_id,
            ProductVariant.id != variant.id,
            ProductVariant.display_order == desired_order,
        )
    ).first() is not None
    if hay_conflicto:
        otras = db.execute(
            select(ProductVariant).where(
                ProductVariant.product_id == variant.product_id,
                ProductVariant.id != variant.id,
                ProductVariant.display_order >= desired_order,
            )
        ).scalars().all()
        otras.sort(key=lambda v: v.display_order)
        for v in otras:
            v.display_order = -(v.display_order + 1)
        db.flush()
        for v in otras:
            v.display_order = -v.display_order
        db.flush()
    variant.display_order = desired_order
    db.flush()


def validar_y_aplicar_fila_producto(db: Session, fila: list[Any]) -> dict:
    """Hoja Productos: Código (col. 1) resuelve actualización o creación; Nombre
    y Categoría son obligatorios; Tipo de preparación debe ser 'Preparado' o
    'Empacado'; las 5 columnas booleanas conservan el valor existente si vienen
    vacías en una actualización (nunca se infiere `false`, FR-005a), y usan el
    default de creación si vienen vacías al crear."""
    code = str(fila[0]).strip() if fila[0] else None
    try:
        nombre = fila[1]
        if not nombre or not str(nombre).strip():
            raise ValueError("Nombre es obligatorio")
        nombre = str(nombre).strip()

        categoria_valor = fila[2]
        if not categoria_valor or not str(categoria_valor).strip():
            raise ValueError("Categoría es obligatoria")

        tipo_valor = fila[3]
        tipo_label = str(tipo_valor).strip() if tipo_valor else ""
        if tipo_label not in _PREPARATION_LABELS:
            raise ValueError(
                f"Tipo de preparación debe ser 'Preparado' o 'Empacado', se "
                f"recibió '{tipo_valor}'"
            )
        preparation_type = _PREPARATION_LABELS[tipo_label]

        descripcion_valor = fila[4]
        descripcion = str(descripcion_valor).strip() if descripcion_valor not in (None, "") else None

        activo = _parse_bool(fila[5], "Activo")
        disponible = _parse_bool(fila[6], "Disponible")
        rastrea = _parse_bool(fila[7], "Rastrea inventario")
        es_adicion = _parse_bool(fila[8], "Es adición")
        visible_qr = _parse_bool(fila[9], "Visible en menú QR")

        existente = None
        if code:
            existente = db.execute(
                select(Product).where(Product.code == code)
            ).scalar_one_or_none()

        categoria = resolve_or_create_category(db, categoria_valor)

        if existente is not None:
            existente.name = nombre
            existente.category_id = categoria.id
            existente.preparation_type = preparation_type
            existente.description = descripcion
            if activo is not None:
                existente.active = activo
            if disponible is not None:
                existente.available = disponible
            if rastrea is not None:
                existente.tracks_inventory = rastrea
            if es_adicion is not None:
                existente.is_topping = es_adicion
            if visible_qr is not None:
                existente.visible_en_qr = visible_qr
            db.flush()
            db.commit()
            return {"ok": True, "accion": "actualizado", "motivo": None}

        producto = Product(
            code=code or service.generate_next_product_code(db),
            name=nombre,
            category_id=categoria.id,
            preparation_type=preparation_type,
            description=descripcion,
            active=activo if activo is not None else True,
            available=disponible if disponible is not None else True,
            tracks_inventory=rastrea if rastrea is not None else False,
            is_topping=es_adicion if es_adicion is not None else False,
            visible_en_qr=(
                visible_qr if visible_qr is not None
                else not (es_adicion if es_adicion is not None else False)
            ),
        )
        db.add(producto)
        db.flush()
        db.commit()
        return {"ok": True, "accion": "creado", "motivo": None}
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "accion": None, "motivo": str(exc)}


def validar_y_aplicar_fila_presentacion(db: Session, fila: list[Any]) -> dict:
    """Hoja Presentaciones y precios: Código de producto debe existir (ya en el
    sistema o creado por una fila anterior de Productos, ya committeada);
    Presentación se busca/crea; la combinación producto+presentación identifica
    una variante existente o crea una nueva; Precio ≥ 0 obligatorio; Orden
    entero ≥ 0 obligatorio, con reasignación determinística en colisión."""
    try:
        code_producto = str(fila[0]).strip() if fila[0] else None
        if not code_producto:
            raise ValueError("Código de producto es obligatorio")
        producto = db.execute(
            select(Product).where(Product.code == code_producto)
        ).scalar_one_or_none()
        if producto is None:
            raise ValueError(f"no existe ningún producto con código '{code_producto}'")

        nombre_presentacion = fila[1]
        if not nombre_presentacion or not str(nombre_presentacion).strip():
            raise ValueError("Presentación es obligatoria")
        presentacion = resolve_or_create_presentation(db, nombre_presentacion)

        precio = _parse_decimal(fila[4], "Precio")
        orden = _parse_int(fila[5], "Orden")
        sku_valor = fila[2]
        sku = str(sku_valor).strip() if sku_valor not in (None, "") else None
        nombre_comercial_valor = fila[3]
        nombre_comercial = str(nombre_comercial_valor).strip() if nombre_comercial_valor not in (None, "") else ""
        activo = _parse_bool(fila[6], "Activo")

        variante = db.execute(
            select(ProductVariant).where(
                ProductVariant.product_id == producto.id,
                ProductVariant.presentation_id == presentacion.id,
            )
        ).scalar_one_or_none()

        if variante is not None:
            if sku is not None and sku != variante.sku:
                _ensure_sku_unique(db, sku, exclude_id=variante.id)
                variante.sku = sku
            variante.name = nombre_comercial
            variante.price = precio
            if activo is not None:
                variante.active = activo
            db.flush()
            _assign_variant_display_order(db, variante, orden)
            db.commit()
            return {"ok": True, "accion": "actualizado", "motivo": None}

        if sku is not None:
            _ensure_sku_unique(db, sku)
        max_order = db.execute(
            select(func.max(ProductVariant.display_order)).where(
                ProductVariant.product_id == producto.id
            )
        ).scalar() or 0
        variante = ProductVariant(
            product_id=producto.id, presentation_id=presentacion.id,
            sku=sku, name=nombre_comercial, price=precio,
            active=activo if activo is not None else True,
            display_order=max_order + 1,
        )
        db.add(variante)
        db.flush()
        _assign_variant_display_order(db, variante, orden)
        db.commit()
        return {"ok": True, "accion": "creado", "motivo": None}
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "accion": None, "motivo": str(exc)}


def validar_y_aplicar_fila_receta(db: Session, fila: list[Any]) -> dict:
    """Hoja Receta: Código de producto + Presentación deben resolver a una
    variante existente; Insumo se resuelve por nombre o código contra el
    inventario ya existente -- nunca se crea (FR-008); Cantidad > 0."""
    try:
        code_producto = str(fila[0]).strip() if fila[0] else None
        if not code_producto:
            raise ValueError("Código de producto es obligatorio")
        nombre_presentacion = fila[1]
        if not nombre_presentacion or not str(nombre_presentacion).strip():
            raise ValueError("Presentación es obligatoria")
        presentacion_texto = str(nombre_presentacion).strip()

        producto = db.execute(
            select(Product).where(Product.code == code_producto)
        ).scalar_one_or_none()
        if producto is None:
            raise ValueError(f"no existe ningún producto con código '{code_producto}'")

        variante = db.execute(
            select(ProductVariant)
            .join(Presentation, Presentation.id == ProductVariant.presentation_id)
            .where(
                ProductVariant.product_id == producto.id,
                func.lower(Presentation.name) == presentacion_texto.lower(),
            )
        ).scalar_one_or_none()
        if variante is None:
            raise ValueError(
                f"no existe ninguna presentación '{presentacion_texto}' para el "
                f"producto '{code_producto}'"
            )

        insumo_valor = fila[2]
        if not insumo_valor or not str(insumo_valor).strip():
            raise ValueError("Insumo es obligatorio")
        insumo_texto = str(insumo_valor).strip()
        insumo = db.execute(
            select(InventoryItem).where(
                (func.lower(InventoryItem.name) == insumo_texto.lower())
                | (func.lower(InventoryItem.code) == insumo_texto.lower())
            )
        ).scalar_one_or_none()
        if insumo is None:
            raise ValueError(f"no existe ningún insumo con nombre o código '{insumo_texto}'")

        cantidad = _parse_decimal(fila[3], "Cantidad", allow_zero=False)

        item = db.execute(
            select(RecipeItem).where(
                RecipeItem.product_variant_id == variante.id,
                RecipeItem.inventory_item_id == insumo.id,
            )
        ).scalar_one_or_none()
        if item is not None:
            item.quantity = cantidad
            db.flush()
            db.commit()
            return {"ok": True, "accion": "actualizado", "motivo": None}

        item = RecipeItem(
            product_variant_id=variante.id, inventory_item_id=insumo.id, quantity=cantidad,
        )
        db.add(item)
        db.flush()
        db.commit()
        return {"ok": True, "accion": "creado", "motivo": None}
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "accion": None, "motivo": str(exc)}


def validar_y_aplicar_fila_adicion(db: Session, fila: list[Any]) -> dict:
    """Hoja Adiciones: ambos códigos deben existir; el producto adición debe
    tener `is_topping=true` (FR-009); un producto no puede ser adición de sí
    mismo."""
    try:
        code_producto = str(fila[0]).strip() if fila[0] else None
        code_adicion = str(fila[1]).strip() if fila[1] else None
        if not code_producto:
            raise ValueError("Código de producto es obligatorio")
        if not code_adicion:
            raise ValueError("Código de producto adición es obligatorio")
        if code_producto == code_adicion:
            raise ValueError("un producto no puede ser adición de sí mismo")

        producto = db.execute(
            select(Product).where(Product.code == code_producto)
        ).scalar_one_or_none()
        if producto is None:
            raise ValueError(f"no existe ningún producto con código '{code_producto}'")

        adicion = db.execute(
            select(Product).where(Product.code == code_adicion)
        ).scalar_one_or_none()
        if adicion is None:
            raise ValueError(f"no existe ningún producto con código '{code_adicion}'")
        if not adicion.is_topping:
            raise ValueError(
                f"el producto '{code_adicion}' no está marcado como adición (Es adición)"
            )

        relacion = db.execute(
            select(ProductTopping).where(
                ProductTopping.product_id == producto.id,
                ProductTopping.topping_product_id == adicion.id,
            )
        ).scalar_one_or_none()
        if relacion is not None:
            return {"ok": True, "accion": "actualizado", "motivo": None}

        relacion = ProductTopping(product_id=producto.id, topping_product_id=adicion.id)
        db.add(relacion)
        db.flush()
        db.commit()
        return {"ok": True, "accion": "creado", "motivo": None}
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "accion": None, "motivo": str(exc)}


_VALIDADORES = {
    "Productos": validar_y_aplicar_fila_producto,
    "Presentaciones y precios": validar_y_aplicar_fila_presentacion,
    "Receta": validar_y_aplicar_fila_receta,
    "Adiciones": validar_y_aplicar_fila_adicion,
}


def procesar_archivo_importacion(db: Session, job, hojas: dict[str, list[list[Any]]]) -> None:
    """Recorre las hojas presentes en el orden fijo de FR-007, aplica cada fila
    de forma independiente y deja el `job` (`ProductImportJob`) en `finalizado`
    con los conteos y errores acumulados. Solo la hoja Productos pre-escanea
    códigos duplicados *dentro del mismo archivo* (edge case del spec: ambas
    filas fallan, ninguna se aplica) -- una vez committeada la primera,
    `validar_y_aplicar_fila_producto` ya no vería un duplicado como tal, sino
    como una actualización legítima.

    Cancelación cooperativa: cada fila hace su propio commit, lo que expira
    `job` en la sesión -- si otra sesión marca el job como ya no `procesando`,
    el loop corta aquí mismo, dejando el status que haya puesto quien lo hizo
    (mismo patrón que `inventory.import_.procesar_archivo_importacion`)."""
    for hoja in _ORDEN_HOJAS:
        filas = hojas.get(hoja)
        if not filas:
            continue
        validar_fn = _VALIDADORES[hoja]

        duplicados: set[str] = set()
        if hoja == "Productos":
            codigos_vistos: dict[str, int] = {}
            for fila in filas:
                code = str(fila[0]).strip() if fila[0] else None
                if code:
                    codigos_vistos[code] = codigos_vistos.get(code, 0) + 1
            duplicados = {code for code, veces in codigos_vistos.items() if veces > 1}

        for numero, fila in enumerate(filas, start=1):
            if job.status != "procesando":
                return
            code = str(fila[0]).strip() if fila[0] else None
            if hoja == "Productos" and code and code in duplicados:
                resultado = {
                    "ok": False,
                    "motivo": f"el código '{code}' aparece más de una vez en este archivo",
                }
            else:
                resultado = validar_fn(db, fila)

            if resultado["ok"]:
                if resultado["accion"] == "creado":
                    job.created_count += 1
                else:
                    job.updated_count += 1
            else:
                job.failed_count += 1
                job.errors = [*job.errors, {
                    "hoja": hoja, "fila": numero, "codigo": code, "motivo": resultado["motivo"],
                }]
            db.add(job)
            db.commit()

    job.status = "finalizado"
    job.finished_at = utc_now_naive()
    db.commit()
