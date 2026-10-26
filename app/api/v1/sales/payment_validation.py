"""Validación compartida de "métodos distintos" (spec 135, A-146).

Invocada tanto desde `build_sale` (cobro de pedido/mostrador, US1) como desde
`cash/router.py::add_movement` (ingreso de caja mixto, US3) — mismo criterio de
rechazo, mismo texto de mensaje (solo cambia `entity_label`), para que el
cajero reciba el mismo error sin importar en qué pantalla repitió el método.
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.payment import PaymentMethod


def assert_distinct_payment_methods(db: Session, lines: list, entity_label: str) -> None:
    """Rechaza con 422 si dos o más `lines` comparten `payment_method_id`.

    `lines` es cualquier lista de objetos con atributo `payment_method_id`
    (`PaymentIn`, `CashMovementLineIn`, etc.)."""
    seen: set = set()
    for line in lines:
        if line.payment_method_id in seen:
            method = db.get(PaymentMethod, line.payment_method_id)
            nombre = method.name if method else line.payment_method_id
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"El método de pago «{nombre}» no puede usarse en más de una "
                f"línea de este {entity_label}.",
            )
        seen.add(line.payment_method_id)
