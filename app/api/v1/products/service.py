"""Service de productos (catálogo simple para heladería).

Un producto pertenece a una categoría y tiene 1..N variantes vendibles (precio +
receta viven en la variante). Al crear un producto sin variantes explícitas, hereda
una por cada presentación activa asociada a su categoría (spec 083, FR-005) o, si no
tiene ninguna, nace con la variante default 'Presentación única' (FR-006, A-74) para
ser vendible de inmediato; se agregan más desde el módulo catalog.
"""
import logging
import re
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy.sql import Select

from app.core.audit import record_audit
from app.core.crud import get_or_404, ensure_unique
from app.core.models import Tenant, User
from app.core.plan_limits import ensure_module_access
from app.core.timezone import utc_now_naive
from app.core.asset_refs import (
    ImageDecision,
    delete_if_unreferenced,
    resolve_image_change,
)
from app.core.storage import FOLDER_PRODUCTS, deletable_key, delete_object, normalize_asset_ref
from app.models.product import Product
from app.models.product_variant import ProductVariant
from app.models.product_topping import ProductTopping
from app.models.category import Category
from app.models.category_presentation import CategoryPresentation
from app.models.presentation import Presentation
from app.api.v1.catalog.service import (
    ensure_default_variant,
    _save_variant_entry,
    _assign_display_orders,
)
from app.api.v1.catalog.schemas import VariantSaveIn, VariantResponse
from app.api.v1.products.schemas import (
    ProductCreate,
    ProductUpdate,
    ProductResponse,
    ProductDetailResponse,
    VariantDetailResponse,
    ProductSaveResponse,
    VariantSaveOut,
    RecipeItemResponse,
    VariantOptionGroupResponse,
    ToppingSummaryResponse,
)

logger = logging.getLogger(__name__)

_NUMERIC_CODE = re.compile(r"^\d+$")


def generate_next_product_code(db: Session) -> str:
    """Código secuencial con cero a la izquierda (spec 143, research.md D3) -- mismo
    criterio que `inventory/service.generate_next_code`: el siguiente entero después
    del código numérico más alto ya asignado en este tenant, con al menos 4 dígitos.
    Códigos no numéricos que el administrador haya fijado a mano no interfieren con
    la secuencia automática -- se ignoran al buscar el máximo."""
    codes = db.execute(select(Product.code)).scalars().all()
    max_numeric = 0
    for code in codes:
        if _NUMERIC_CODE.match(code):
            max_numeric = max(max_numeric, int(code))
    return f"{max_numeric + 1:04d}"


class ProductService:
    def _validate_fks(self, db: Session, category_id: UUID | None) -> None:
        if category_id is not None:
            get_or_404(db, Category, category_id, "Category not found")

    def list_query(
        self,
        active: bool | None = None,
        search: str | None = None,
        available: bool | None = None,
        is_topping: bool | None = None,
    ) -> Select:
        # spec 097 (research.md D1): JOIN contra Category en la propia consulta, para que
        # `category_name` (ver Product.category_name) no dependa de la paginación de
        # `GET /categories` (tope 100). `joinedload` sobre una relación many-to-one no
        # multiplica filas ni exige `.unique()`.
        stmt = (
            select(Product)
            .options(joinedload(Product.category))
            .order_by(Product.created_at.desc())
        )
        if active is not None:
            stmt = stmt.where(Product.active == active)
        if available is not None:
            # spec 093 (FR-006): filtro "Disponibles"/"Agotados" de la Carta del menú.
            stmt = stmt.where(Product.available == available)
        if search:
            stmt = stmt.where(Product.name.ilike(f"%{search.strip()}%"))
        if is_topping is not None:
            # spec 129: selector de "Adiciones disponibles" en el formulario de producto.
            stmt = stmt.where(Product.is_topping == is_topping)
        return stmt

    def get_or_404(self, db: Session, id: UUID) -> Product:
        return get_or_404(db, Product, id, "Product not found")

    def get_detail_or_404(self, db: Session, id: UUID) -> Product:
        """Spec 103-detalle-producto-consolidado (research.md D2): variante de
        `get_or_404` solo para el detalle de producto -- precarga `recipe_items`/
        `option_groups` de cada variante con `selectinload` (mismo patrón que ya usa
        `menu/router.py` para evitar N+1 en su propio endpoint), para que
        `to_detail_response` no dispare una consulta perezosa por variante. No se
        generaliza `get_or_404` en sí (Principio V): ningún otro llamador necesita hoy
        esta precarga."""
        stmt = (
            select(Product)
            .where(Product.id == id)
            .options(
                selectinload(Product.variants).selectinload(ProductVariant.recipe_items),
                selectinload(Product.variants).selectinload(ProductVariant.option_groups),
                # spec 129: `topping_ids` del detalle se arma desde esta relación, sin
                # disparar una consulta perezosa por producto.
                selectinload(Product.toppings),
            )
        )
        product = db.execute(stmt).scalar_one_or_none()
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        return product

    def to_detail_response(self, product: Product, user: User) -> ProductDetailResponse:
        """Spec 093 (escenario 9, research.md D9): agrega las presentaciones activas
        con su precio al detalle de producto. Spec 103-detalle-producto-consolidado
        (research.md D1/D3): cada variante ahora también trae `recipe`/`option_groups`
        embebidos -- `recipe` solo si `user` no es Cajero (preserva FR-018 de la spec
        093, que `option_groups` nunca restringió). No se puede confiar en el mapeo
        automático de `from_attributes` porque incluiría también las presentaciones
        inactivas de `product.variants`, y porque el modelo ORM expone
        `recipe_items`/`option_groups`, no `recipe`/`option_groups`.

        `deactivated` (ajuste posterior): las presentaciones inactivas, en el shape
        `VariantResponse` -- reutiliza el mismo `product.variants` ya precargado, sin
        receta/grupos (restaurar una ya los pide aparte). Deja innecesario un segundo
        `GET /products/{id}/variants` solo para listarlas."""
        base = ProductResponse.model_validate(product)
        is_cashier = user.role is not None and user.role.name == "CASHIER"
        return ProductDetailResponse(
            **base.model_dump(),
            variants=[
                VariantDetailResponse(
                    **VariantResponse.model_validate(v).model_dump(),
                    recipe=(
                        []
                        if is_cashier
                        else [RecipeItemResponse.model_validate(r) for r in v.recipe_items]
                    ),
                    option_groups=[
                        VariantOptionGroupResponse.model_validate(g) for g in v.option_groups
                    ],
                )
                for v in product.variants
                if v.active
            ],
            deactivated=[
                VariantResponse.model_validate(v) for v in product.variants if not v.active
            ],
            topping_ids=[t.topping_product_id for t in product.toppings],
            toppings=[
                ToppingSummaryResponse(id=t.topping_product_id, name=t.topping_name)
                for t in product.toppings
            ],
        )

    def set_availability(
        self, db: Session, id: UUID, available: bool, user: User
    ) -> Product:
        """Spec 093 (FR-009/FR-011/FR-013/FR-016, research.md D3/D4/D5/D10): el único
        camino por el que Cajero (y, desde esta spec, también Admin) toca `available`.
        A diferencia de `update_product`, no acepta ningún otro campo y bloquea el
        cambio sobre un producto inactivo. Un solo `UPDATE` sin bloqueo: dos llamadas
        casi simultáneas dejan como resultado final la que el servidor procesa al
        final, sin estado intermedio inconsistente (D10)."""
        product = self.get_or_404(db, id)
        if not product.active:
            raise HTTPException(
                status_code=409,
                detail={"error": "No se puede cambiar la disponibilidad de un producto inactivo"},
            )
        previous = product.available
        product.available = available
        product.available_changed_at = utc_now_naive()
        product.available_changed_by_name = user.name
        record_audit(
            db,
            action="availability_changed",
            entity="product",
            entity_id=product.id,
            user=user,
            payload={"available": available, "previous": previous},
        )
        db.commit()
        db.refresh(product)
        return product

    def create_product(self, db: Session, tenant: Tenant, data: ProductCreate) -> Product:
        self._validate_fks(db, data.category_id)
        # spec 064, FR-011/FR-012: activar "maneja inventario" exige el módulo Inventario
        # en el plan vigente del tenant -- gating a nivel de campo (research.md Decisión 4):
        # crear un producto con tracks_inventory=False sigue funcionando sin ese módulo.
        if data.tracks_inventory:
            ensure_module_access(db, tenant, "inventario")
        # spec 088 (FR-001/FR-003/FR-004): una imagen gestionada nueva debe ser una key
        # del propio negocio y carpeta `products`, y existir en R2. Sin base: en una
        # creación no hay imagen vigente que comparar. 422/503 antes de crear nada.
        resolve_image_change(
            db, tenant_schema=tenant.schema, folder=FOLDER_PRODUCTS,
            sent=data.image_url, base_provided=False, base=None, current=None,
            is_creation=True,
        )
        if data.code:
            ensure_unique(db, Product, Product.code, data.code, "Product code already exists")
        try:
            product = Product(
                code=data.code or generate_next_product_code(db),
                category_id=data.category_id,
                name=data.name,
                description=data.description,
                preparation_type=data.preparation_type.value,
                image_url=data.image_url,
                available=data.available,
                tracks_inventory=data.tracks_inventory,
                is_topping=data.is_topping,
                # spec 129 (research.md D6): default calculado -- oculto en QR si es
                # una adición, visible en caso contrario, a menos que se envíe explícito.
                visible_en_qr=(
                    data.visible_en_qr
                    if data.visible_en_qr is not None
                    else not data.is_topping
                ),
            )
            db.add(product)
            db.flush()
            self._reconcile_toppings(db, product, data.topping_ids, is_topping=product.is_topping)
            if data.variants:
                # spec 043 (FR-001): árbol completo en la misma transacción --
                # `ensure_default_variant` no aplica, ya hay al menos una presentación explícita.
                self._save_variant_tree(db, product, data.variants)
            else:
                # spec 083 (FR-005/FR-006, research.md D6): sin variantes explícitas, el
                # producto hereda una ProductVariant por cada presentación activa asociada
                # a su categoría; sin ninguna asociada, sigue naciendo con el default único
                # (ahora "Presentación única", A-74).
                self._apply_inherited_or_default_variant(db, product)
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("Error creando producto")
            raise
        db.refresh(product)
        return product

    def update_product(self, db: Session, tenant: Tenant, id: UUID, data: ProductUpdate) -> Product:
        product = self.get_or_404(db, id)
        self._validate_fks(db, data.category_id)
        # spec 088 (research D6): la imagen se resuelve ANTES de asignar cualquier otro
        # campo, de modo que un 422/503 deja el registro exactamente como estaba. Una
        # imagen enviada solo cuenta como cambio si `image_url_base` coincide con la
        # vigente (FR-002); un formulario desactualizado no cambia la imagen y el resto
        # de los campos se guarda igualmente, en silencio.
        # spec 080: `image_url`/`image_url_base` ya vienen normalizadas a key (AssetRefIn).
        image_decision = resolve_image_change(
            db, tenant_schema=tenant.schema, folder=FOLDER_PRODUCTS,
            sent=data.image_url,
            base_provided="image_url_base" in data.model_fields_set,
            base=data.image_url_base,
            current=normalize_asset_ref(product.image_url),
            is_creation=False,
        )
        if data.code is not None and data.code != product.code:
            ensure_unique(db, Product, Product.code, data.code, "Product code already exists")
            product.code = data.code
        if data.category_id is not None:
            product.category_id = data.category_id
        if data.name is not None:
            product.name = data.name
        if data.description is not None:
            product.description = data.description
        if data.preparation_type is not None:
            product.preparation_type = data.preparation_type.value
        old_key = None
        if image_decision is ImageDecision.APPLY:
            # `deletable_key` solo devuelve una key en convención del propio negocio
            # (FR-004/FR-005): una URL de otro origen o una key histórica fuera de
            # convención queda huérfana en vez de borrarse.
            old_key = deletable_key(product.image_url, tenant.schema, FOLDER_PRODUCTS)
            product.image_url = data.image_url
        elif data.image_url is not None and normalize_asset_ref(product.image_url) == data.image_url:
            # Misma imagen (KEEP) en otra representación: una fila histórica con URL
            # absoluta pasa a guardarse como key al volver a guardarla (spec 080, FR-004).
            # No es un cambio de imagen: no se borra ni se verifica nada.
            product.image_url = data.image_url
        if data.active is not None:
            product.active = data.active
        if data.available is not None:
            product.available = data.available
        if data.tracks_inventory is not None:
            # spec 064, FR-011/FR-012: solo reevalúa el plan cuando el valor realmente
            # cambia a `True` -- un PATCH que no toca este campo, o que lo deja igual,
            # nunca dispara un 403 sorpresivo por otra edición no relacionada.
            if data.tracks_inventory and not product.tracks_inventory:
                ensure_module_access(db, tenant, "inventario")
            product.tracks_inventory = data.tracks_inventory
        if data.is_topping is not None:
            product.is_topping = data.is_topping
        if data.visible_en_qr is not None:
            product.visible_en_qr = data.visible_en_qr

        try:
            # spec 129 (FR-002/FR-016): `topping_ids` ausente = no tocar la asociación
            # existente; presente (incluida `[]`) = reemplazo total.
            if "topping_ids" in data.model_fields_set and data.topping_ids is not None:
                self._reconcile_toppings(
                    db, product, data.topping_ids, is_topping=product.is_topping
                )
            # spec 043 (FR-002): `variants` ausente del body = no tocar ninguna presentación
            # (back-compat); presente (incluida `[]`) = reconciliación completa. Todo esto entra
            # en el mismo `db.commit()` de abajo que ya persistía los campos del producto --
            # ningún cambio de esta llamada se guarda si la reconciliación falla (FR-004).
            if "variants" in data.model_fields_set:
                self._reconcile_variants(db, product, data.variants or [])
            db.commit()
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            logger.exception("Error actualizando producto")
            raise
        # A-44 / spec 021 (intacto): primero confirmar, después borrar best-effort. spec 088
        # (FR-006/FR-007): y solo si ninguna otra fila usa el archivo anterior.
        if old_key:
            delete_if_unreferenced(db, old_key, delete_object)
        db.refresh(product)
        return product

    def soft_delete(self, db: Session, id: UUID) -> Product:
        product = self.get_or_404(db, id)
        product.active = False
        db.commit()
        db.refresh(product)
        return product

    # ===================== Guardado consolidado (spec 043) =====================

    def _apply_inherited_or_default_variant(self, db: Session, product: Product) -> None:
        """Rama `else` de `create_product` cuando no llegan `variants` explícitas
        (spec 083, research.md D6). Resuelve las presentaciones activas asociadas a
        `product.category_id`; con al menos una, crea una `ProductVariant` por cada una
        (`price=0`, FR-005) reutilizando el guardado consolidado ya existente (spec 043).
        Sin ninguna, sigue llamando `ensure_default_variant` sin cambios de firma (FR-006)."""
        presentaciones: list[Presentation] = []
        if product.category_id is not None:
            presentaciones = db.execute(
                select(Presentation)
                .join(
                    CategoryPresentation,
                    CategoryPresentation.presentation_id == Presentation.id,
                )
                .where(
                    CategoryPresentation.category_id == product.category_id,
                    Presentation.active.is_(True),
                )
                .order_by(Presentation.name)
            ).scalars().all()

        if presentaciones:
            entradas = [VariantSaveIn(presentation_id=p.id) for p in presentaciones]
            self._save_variant_tree(db, product, entradas)
        else:
            ensure_default_variant(db, product)

    def _reconcile_toppings(
        self, db: Session, product: Product, topping_ids: list[UUID], *, is_topping: bool
    ) -> None:
        """Spec 129 (FR-002/FR-016): reemplazo total de las adiciones asociadas a
        `product` -- mismo criterio que `category.presentations` (spec 083): no hay
        columnas propias en la tabla puente que preservar entre un guardado y el
        siguiente. Valida (422) que ningún id sea el propio producto (auto-asociación),
        que cada id referencie un producto existente marcado `is_topping=true`, y que
        una adición (`is_topping=true`) no tenga a su vez adiciones propias (fuera de
        alcance, spec.md Assumptions). No hace `commit()` -- lo controla el llamador."""
        if not topping_ids:
            if product.toppings:
                for row in list(product.toppings):
                    db.delete(row)
            return

        if is_topping:
            raise HTTPException(
                status_code=422,
                detail={"error": "Una adición no puede tener sus propias adiciones asociadas"},
            )
        if product.id in topping_ids:
            raise HTTPException(
                status_code=422,
                detail={"error": "Un producto no puede asociarse a sí mismo como su propia adición"},
            )

        unique_ids = list(dict.fromkeys(topping_ids))
        found = db.execute(
            select(Product.id, Product.is_topping).where(Product.id.in_(unique_ids))
        ).all()
        found_by_id = {row.id: row.is_topping for row in found}
        missing = [str(i) for i in unique_ids if i not in found_by_id]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={"error": f"Adición(es) no encontrada(s): {', '.join(missing)}"},
            )
        not_toppings = [str(i) for i in unique_ids if not found_by_id[i]]
        if not_toppings:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": (
                        "Solo productos marcados como adición pueden asociarse: "
                        f"{', '.join(not_toppings)}"
                    )
                },
            )

        existing_by_topping = {row.topping_product_id: row for row in product.toppings}
        for existing_id, row in list(existing_by_topping.items()):
            if existing_id not in unique_ids:
                db.delete(row)
        for topping_id in unique_ids:
            if topping_id not in existing_by_topping:
                db.add(ProductTopping(product_id=product.id, topping_product_id=topping_id))

    def _save_variant_tree(
        self, db: Session, product: Product, entries: list[VariantSaveIn]
    ) -> None:
        """Crea todas las presentaciones iniciales de un producto nuevo, en el orden recibido
        (FR-001). No hace `commit()` -- lo controla `create_product`."""
        variants = [
            _save_variant_entry(db, product, entry, index, {})
            for index, entry in enumerate(entries)
        ]
        _assign_display_orders(db, product.id, variants)

    def _reconcile_variants(
        self, db: Session, product: Product, entries: list[VariantSaveIn]
    ) -> None:
        """Reconcilia el conjunto de presentaciones activas del producto contra `entries` (spec
        043, FR-002, data-model.md tabla de reconciliación): las entradas sin `id` se crean, las
        que traen `id` se actualizan (incluida una presentación **inactiva** -- así se reactiva,
        `RN-CAT-09`), y cualquier presentación activa existente que `entries` no mencione se
        desactiva (`RN-CAT-10`). `display_order` queda según la posición de cada entrada dentro de
        `entries`. No hace `commit()` -- lo controla `update_product`.
        """
        all_existing = db.execute(
            select(ProductVariant).where(ProductVariant.product_id == product.id)
        ).scalars().all()
        existing_by_id = {v.id: v for v in all_existing}
        kept_ids = {entry.id for entry in entries if entry.id is not None}

        for variant in all_existing:
            if variant.active and variant.id not in kept_ids:
                variant.active = False

        resolved = [
            _save_variant_entry(db, product, entry, index, existing_by_id)
            for index, entry in enumerate(entries)
        ]
        _assign_display_orders(db, product.id, resolved)

    def to_save_response(self, product: Product) -> ProductSaveResponse:
        """Arma la respuesta completa de `POST`/`PATCH`/`PUT /products` (FR-006): el producto más
        sus presentaciones activas (`product.variants` ya viene ordenado por `display_order`,
        spec 042), cada una con su receta y sus grupos de opciones resueltos -- no se puede confiar
        en el mapeo automático de Pydantic porque el modelo ORM expone `recipe_items`, no
        `recipe`."""
        base = ProductResponse.model_validate(product)
        return ProductSaveResponse(
            **base.model_dump(),
            variants=[
                VariantSaveOut(
                    id=v.id,
                    product_id=v.product_id,
                    presentation_id=v.presentation_id,
                    presentation_name=v.presentation_name,
                    name=v.name,
                    display_name=v.display_name,
                    sku=v.sku,
                    price=v.price,
                    active=v.active,
                    display_order=v.display_order,
                    recipe=[RecipeItemResponse.model_validate(r) for r in v.recipe_items],
                    option_groups=[
                        VariantOptionGroupResponse.model_validate(g) for g in v.option_groups
                    ],
                )
                for v in product.variants
                if v.active
            ],
            topping_ids=[t.topping_product_id for t in product.toppings],
            toppings=[
                ToppingSummaryResponse(id=t.topping_product_id, name=t.topping_name)
                for t in product.toppings
            ],
        )
