"""Schemas de administración de promociones — spec 063 (modelo por conjunto
explícito de variantes).

`contracts/administracion-promociones.md` §1. La entrada queda en **dos tipos**
(`percent`, `package_price`); `PromotionResponse.type` es `str` libre para poder
serializar las promociones que la migración `063a` dejó `finished` con su `type`
histórico (`combo`/`qty_price`/`fixed`/`qty_price_presentation`, FR-025).
"""
from enum import Enum
from uuid import UUID
from datetime import datetime, time
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.core.timezone import UtcDatetime


class PromotionType(str, Enum):
    """Tipos **vivos** — los únicos que una promoción nueva puede tener
    (FR-002, A-62). `package_price`: `value` = precio total de `min_qty`
    unidades cualesquiera del conjunto de variantes."""
    PERCENT = "percent"
    PACKAGE_PRICE = "package_price"


class PromotionStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    FINISHED = "finished"


def _validate_csv(value: str | None, low: int, high: int, label: str) -> str | None:
    """`days_of_week` era texto libre acotado por longitud: un `"lunes,martes"`
    se guardaba tal cual y la promoción no aplicaba nunca, sin ningún error
    visible."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    parts = [p.strip() for p in value.split(",") if p.strip()]
    nums = []
    for p in parts:
        if not p.isdigit() or not (low <= int(p) <= high):
            raise ValueError(f"{label} debe ser una lista de enteros {low}..{high}")
        nums.append(int(p))
    return ",".join(str(n) for n in sorted(set(nums)))


class _VigenciaMixin(BaseModel):
    @model_validator(mode="after")
    def _normalize_days(self):
        if "days_of_week" in self.model_fields_set:
            self.days_of_week = _validate_csv(self.days_of_week, 0, 6, "days_of_week")
        return self

    @model_validator(mode="after")
    def _time_window_pair(self):
        # Una ventana a medias no significa nada y hace creer al admin que
        # configuró un happy hour.
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError("start_time y end_time deben configurarse juntos")
        return self

    @model_validator(mode="after")
    def _date_range(self):
        # spec 127: `PromotionUpdate` SÍ lleva `starts_at` ahora (editable,
        # solo en `draft` -- la guarda de estado vive en `service.update()`),
        # para poder corregir una fecha de inicio que quedó mal calculada por
        # el defecto de zona horaria que esta spec corrige (FR-005).
        starts_at = getattr(self, "starts_at", None)
        if starts_at and self.ends_at and self.ends_at.date() < starts_at.date():
            raise ValueError("ends_at no puede ser anterior a starts_at")
        return self


class _PromotionRules(BaseModel):
    """Reglas que dependen del `type` y valen igual en create y shape."""

    @model_validator(mode="after")
    def _percent_range(self):
        if self.type == PromotionType.PERCENT and self.value is not None and self.value > 100:
            raise ValueError("Un descuento porcentual no puede superar 100")
        return self

    @model_validator(mode="after")
    def _package_price_positive(self):
        # FR-002 / FR-006: `value` es el precio total del paquete; `0` no es una
        # promoción. `min_qty >= 1` ya lo cubre el `Field(ge=1)`.
        if (
            self.type == PromotionType.PACKAGE_PRICE
            and self.value is not None
            and self.value <= 0
        ):
            raise ValueError("El precio de paquete debe ser mayor que 0")
        return self

    @model_validator(mode="after")
    def _package_price_min_qty(self):
        # FR-025 (spec 083, sesión 2026-09-17): retira, para reglas nuevas o
        # editadas, el patrón `min_qty = 1` ("precio unitario especial") de
        # spec 063 — no retroactivo, una regla ya guardada con `min_qty = 1`
        # sigue vigente hasta que se edite (create/update_shape comparten este
        # mismo schema, así que ambos caminos quedan cubiertos a la vez).
        if self.type == PromotionType.PACKAGE_PRICE and self.min_qty < 2:
            raise ValueError("En promociones por paquete, el mínimo es de 2 unidades")
        return self


def _no_repeats(variant_ids: list[UUID] | None) -> list[UUID] | None:
    if variant_ids is None:
        return None
    if len(variant_ids) != len(set(variant_ids)):
        raise ValueError("El conjunto de variantes no puede repetir una variante")
    return variant_ids


class PromotionRuleIn(_PromotionRules):
    """spec 063 (revisión 2026-09-01, FR-001/FR-001a): una regla dentro de
    `PromotionCreate.rules` / `PromotionShapeUpdate.rules` — la combinación
    (tipo, valor, cantidad mínima) más su propio conjunto de variantes. Antes
    de esta revisión, estos campos vivían sueltos en `PromotionCreate` /
    `PromotionShapeUpdate` (una promoción = una combinación)."""
    type: PromotionType
    value: Decimal = Field(..., ge=0, max_digits=12, decimal_places=2)
    min_qty: int = Field(1, ge=1)
    # FR-001a: conjunto explícito de variantes elegibles de ESTA regla. >= 1,
    # sin repetidos dentro de la misma regla (la disjunción entre reglas de
    # una misma promoción se valida en el servicio, `_guard_variant_overlap`).
    variant_ids: list[UUID] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _variant_ids_unique(self):
        _no_repeats(self.variant_ids)
        return self


class PromotionCreate(_VigenciaMixin):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    # Nace en borrador: activar es una acción explícita, no un default.
    status: PromotionStatus = PromotionStatus.DRAFT
    # FR-012: `starts_at` es obligatoria.
    starts_at: datetime
    ends_at: datetime | None = None
    days_of_week: str | None = Field(None, max_length=20, examples=["0,1,2,3,4"])
    start_time: time | None = None
    end_time: time | None = None
    # FR-001: una promoción agrupa una o más reglas, capturadas en la misma
    # sesión del formulario (creación por lote, Clarifications 2026-09-01).
    # spec 083 (FR-020, Clarifications 2026-09-16): una promoción `draft`
    # puede crearse SIN reglas todavía -- la pantalla de creación la guarda
    # de inmediato al presionar "Continuar", antes de configurar ninguna
    # regla de precio. `status=active` sigue exigiendo al menos una regla
    # (validado abajo, mismo mensaje que ya usa `change_status`).
    rules: list[PromotionRuleIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_on_create(self):
        if self.status == PromotionStatus.FINISHED:
            raise ValueError("Una promoción no puede crearse finalizada")
        if self.status == PromotionStatus.ACTIVE and not self.rules:
            raise ValueError("Una promoción necesita al menos una regla")
        return self


class PromotionUpdate(_VigenciaMixin):
    """Campos escalares de la **promoción** (`PATCH /promotions/{id}`).

    spec 063 (revisión 2026-09-01, FR-018): `type`/`value`/`min_qty`/
    `variant_ids` ya no existen aquí — viven en cada `PromotionRule` y solo se
    editan por `PATCH /{id}/shape`, y solo en `draft` (el bloque completo de
    reglas queda de solo lectura fuera de `draft`, no campo por campo).

    `starts_at` (spec 127, FR-005): editable, pero solo mientras la
    promoción sigue en `draft` (guarda de estado en `service.update()`, más
    estricta que el resto de estos campos, que también se pueden editar en
    `paused`) — una vez que una promoción llegó a estar `active` alguna vez,
    su fecha de inicio ya no se toca. Existe para que una promoción guardada
    con el defecto de zona horaria que esta spec corrige (día UTC en vez de
    local) pueda corregirse manualmente desde el formulario de edición, sin
    necesidad de una migración de datos (ver `spec.md`, Assumptions)."""

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    days_of_week: str | None = Field(None, max_length=20)
    start_time: time | None = None
    end_time: time | None = None


class PromotionShapeUpdate(BaseModel):
    """Cambio de forma (`PATCH /promotions/{id}/shape`): solo válido en
    `draft`. spec 063 (revisión 2026-09-01, FR-001a/FR-018): `rules`
    **reemplaza la lista completa** de reglas de la promoción — no hay
    endpoint para editar una sola regla suelta. El servicio revalida FR-001a
    (variante repetida entre reglas del payload) / FR-016 / FR-014 contra la
    lista nueva completa."""
    rules: list[PromotionRuleIn] = Field(..., min_length=1)


class PromotionStatusUpdate(BaseModel):
    status: PromotionStatus


class PromotionDuplicate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    # spec 084 (A-83): si ya existe una promoción con ese nombre, `true` la reemplaza (se
    # elimina con todas sus reglas y la copia ocupa su nombre); `false` (por defecto) devuelve
    # 409 como siempre. Nunca reemplaza una promoción `active`.
    replace_existing: bool = False


class PromotionVariantResponse(BaseModel):
    """Una variante del conjunto elegible, con su descripción y precio normal
    vigente (FR-005)."""
    product_variant_id: UUID
    description: str
    unit_price: Decimal


class PromotionRuleResponse(BaseModel):
    """spec 063 (revisión 2026-09-01): una regla en `PromotionResponse.rules`,
    con su resumen legible (FR-005)."""
    id: UUID
    # `str` libre, no el enum: las reglas que la migración `063c` generó a
    # partir de una promoción `finished` de `063a` conservan su `type`
    # histórico (FR-025), y `promotion_rules.type` no lleva `CHECK` de
    # valores (data-model.md, corrección del hallazgo F1).
    type: str
    value: Decimal
    min_qty: int
    # spec 063: condición en lenguaje llano (FR-005). `None` para una regla de
    # tipo viejo.
    condition_text: str | None = None
    variants: list[PromotionVariantResponse] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)


class PromotionResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    status: str
    # spec 138 (FR-002): ÚNICA excepción al offset UTC explícito de la API. Son la hora de pared local del
    # tenant (vigencia de la promoción), no un instante absoluto: se guardan y se devuelven sin offset a
    # propósito. Debe coincidir con `INSTANTES_SIN_OFFSET_PERMITIDOS` de `test_contrato_instantes_api.py`.
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    days_of_week: str | None = None
    start_time: time | None = None
    end_time: time | None = None
    # spec 063: marca de "finalizada por la migración" (FR-025).
    closed_by_refactor_at: UtcDatetime | None = None
    # spec 063 (revisión 2026-09-01, FR-001): reemplaza type/value/min_qty/
    # condition_text/variants sueltos — cada regla lleva los suyos.
    rules: list[PromotionRuleResponse] = Field(default_factory=list)
    model_config = ConfigDict(from_attributes=True)


class OverlapConflict(BaseModel):
    """Cuerpo del 409 de FR-014 (solape real bloqueado, entre reglas de
    promociones distintas)."""
    error: str
    conflicts: list["OverlapConflictEntry"] = Field(default_factory=list)


class OverlapConflictEntry(BaseModel):
    promotion_id: UUID
    promotion_name: str
    # spec 063 (revisión 2026-09-01): qué regla de esa promoción entra en
    # conflicto.
    rule_id: UUID
    variant_ids: list[UUID] = Field(default_factory=list)


class RuleVariantConflict(BaseModel):
    """Cuerpo del 409 de FR-001a (variante repetida entre dos reglas de la
    **misma** promoción, dentro del mismo payload)."""
    error: str
    rule_index_a: int
    rule_index_b: int
    variant_ids: list[UUID] = Field(default_factory=list)


OverlapConflict.model_rebuild()
