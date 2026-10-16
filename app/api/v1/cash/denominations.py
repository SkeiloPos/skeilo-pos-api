"""spec 121 -- desglose por denominación del arqueo de cierre (A-130 a A-134).

Lógica del contrato estricto de `POST /cash/shifts/{id}/close`, fuera de `router.py`
para no engordarlo (mismo criterio que `active_orders.py`, spec 119):

  * `reject_client_totals`: el cliente NO puede enviar valores monetarios (D-1, FR-002).
  * `resolve_count_lines`: valida las líneas contra el catálogo del tenant y las
    resuelve con su tipo y subtotal, calculados por el servidor (FR-001, FR-004,
    FR-006, FR-015).
  * `total_of`: efectivo contado = Σ subtotales; lista vacía = $0 (D-2).
  * `list_catalog`: catálogo en solo lectura (FR-009 a FR-012).

Todos los rechazos son `HTTPException(422)` con `detail` como CADENA en español, el
patrón que ya usan los demás 422 de este router y que `CashService.extractError` del
frontend muestra tal cual (research.md R4). No hay `commit` aquí: ante cualquier
rechazo el llamador no ha persistido nada (FR-007).
"""
from dataclasses import dataclass
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.denomination_catalog import DenominationCatalog

# Máximo representable por `cash_shifts.counted_amount` (`Numeric(12, 2)`). Se valida
# el TOTAL, no cada cantidad: la cantidad máxima útil (≈ 2·10⁸) ya cabe en el INTEGER
# de `quantity`, así que el chequeo del total protege también esa columna (R5).
MAX_COUNTED_AMOUNT = Decimal("9999999999.99")

# Claves con valor monetario que el cliente ya no puede enviar (D-1).
_CAMPOS_MONETARIOS_CUERPO = ("counted_amount", "total", "difference", "subtotal")
_CAMPOS_MONETARIOS_LINEA = ("subtotal", "total")

_MENSAJE_CLIENTE_DESACTUALIZADO = (
    "La aplicación está desactualizada: el total contado ahora lo calcula el servidor. "
    "Recarga la página e intenta cerrar el turno de nuevo."
)


@dataclass(frozen=True)
class CountLine:
    """Línea del conteo ya resuelta por el servidor (solo cantidad > 0)."""

    denomination: Decimal
    kind: str
    quantity: int
    subtotal: Decimal


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)


def format_pesos(valor: Decimal) -> str:
    """`$3.000`, `$9.999.999.999,99`: punto de miles y coma decimal (es-CO).
    Los decimales solo aparecen si el valor no es entero."""
    valor = Decimal(valor)
    if valor == valor.to_integral_value():
        return "$" + f"{int(valor):,}".replace(",", ".")
    entero, decimales = f"{valor:,.2f}".split(".")
    return "$" + entero.replace(",", ".") + "," + decimales


def reject_client_totals(body) -> None:
    """D-1 / FR-002: rechaza con 422 un cuerpo que traiga un valor monetario NO nulo
    del cliente, en el cuerpo (`counted_amount`, `total`, `difference`, `subtotal`) o
    dentro de alguna línea (`subtotal`, `total`). Cualquier otra clave desconocida se
    ignora, como hoy. Un valor `null` no cuenta como valor monetario (research.md R4).
    """
    extra = body.model_extra or {}
    if any(extra.get(campo) is not None for campo in _CAMPOS_MONETARIOS_CUERPO):
        raise _unprocessable(_MENSAJE_CLIENTE_DESACTUALIZADO)
    for linea in body.denominations:
        extra_linea = linea.model_extra or {}
        if any(extra_linea.get(campo) is not None for campo in _CAMPOS_MONETARIOS_LINEA):
            raise _unprocessable(_MENSAJE_CLIENTE_DESACTUALIZADO)


def list_catalog(db: Session) -> list[DenominationCatalog]:
    """Todas las denominaciones del tenant, de mayor a menor valor."""
    return list(db.execute(
        select(DenominationCatalog).order_by(DenominationCatalog.denomination.desc())
    ).scalars().all())


def resolve_count_lines(db: Session, lines) -> list[CountLine]:
    """Valida y resuelve las líneas del cierre. Orden de las verificaciones:

    1. Denominación repetida (FR-006): `Decimal` compara y hashea por valor, así que
       `10000` y `10000.00` son la misma, con independencia de la cantidad.
    2. Denominación fuera del catálogo del tenant (FR-004): se evalúan TODAS las líneas,
       también las de cantidad 0, con UNA sola consulta del catálogo.
    3. Se descartan las líneas con cantidad 0 (FR-015, D-3) y se calcula el subtotal.
    4. El total no puede exceder el máximo de `Numeric(12, 2)`.

    Devuelve solo las líneas con cantidad > 0, de mayor a menor denominación.
    """
    vistas: set[Decimal] = set()
    for linea in lines:
        if linea.denomination in vistas:
            raise _unprocessable(
                f"La denominación {format_pesos(linea.denomination)} está repetida en el conteo."
            )
        vistas.add(linea.denomination)

    tipo_por_valor = {c.denomination: c.kind for c in list_catalog(db)}
    for linea in lines:
        if linea.denomination not in tipo_por_valor:
            raise _unprocessable(
                f"La denominación {format_pesos(linea.denomination)} no está registrada "
                "para este negocio."
            )

    resueltas = [
        CountLine(
            denomination=linea.denomination,
            kind=tipo_por_valor[linea.denomination],
            quantity=linea.quantity,
            subtotal=linea.denomination * linea.quantity,
        )
        for linea in lines
        if linea.quantity > 0
    ]
    resueltas.sort(key=lambda ln: ln.denomination, reverse=True)

    if total_of(resueltas) > MAX_COUNTED_AMOUNT:
        raise _unprocessable(
            "El efectivo contado excede el monto máximo permitido "
            f"({format_pesos(MAX_COUNTED_AMOUNT)})."
        )
    return resueltas


def total_of(lines) -> Decimal:
    """Efectivo contado = Σ subtotales. Sin líneas es $0 (D-2: caja vacía)."""
    return sum((ln.subtotal for ln in lines), start=Decimal(0))
