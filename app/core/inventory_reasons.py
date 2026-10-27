"""Catálogo de motivos y referencias del kardex (RF-015 / RF-016).

`inventory_movements.reason` es texto libre a nivel de BD (sin CheckConstraint,
decisión deliberada para no bloquear motivos nuevos). Este módulo centraliza los
valores canónicos para que los movimientos sean **agregables por motivo** en
reportes; antes eran literales sueltos y distintos en cada llamada
("Consumo de receta en venta", "Consumo de receta en consolidación", …), lo que
hacía imposible sumar por motivo.

El motivo dice *por qué* se movió el stock; `reference_type` + `reference_id`
dicen *de dónde* viene el movimiento. Son ortogonales, igual que `type`:

    type='out', reason=VENTA,  reference_type=REF_ORDER    → consumo de un pedido
    type='in',  reason=COMPRA, reference_type=REF_PURCHASE → recepción de compra
    type='in',  reason=AJUSTE, reference_type=REF_ORDER_VOID → reversa de anulación

Nótese que AJUSTE es válido como entrada **y** como salida: `type` lleva la
dirección, el motivo no.
"""

# --------------------------------------------------------------------- Motivos

#: Salida por consumo de una venta o un pedido de mesa (explosión de receta).
VENTA = "venta"

#: Entrada por recepción de mercancía de un proveedor.
COMPRA = "compra"

#: Corrección manual de existencias, en cualquier dirección. También cubre las
#: reversas automáticas (anulación de ítem, cancelación temprana de pedido).
AJUSTE = "ajuste"

#: Salida por producto dañado o inutilizable.
DANO = "daño"

#: Salida por producto vencido.
VENCIMIENTO = "vencimiento"

#: Salida por consumo del propio negocio (staff, degustación, cortesía).
CONSUMO_INTERNO = "consumo_interno"

#: Motivos válidos como entrada / como salida. No se aplican como constraint de
#: BD; sirven para validar en la capa de API cuando el motivo lo elige el usuario.
ENTRADA_REASONS = (COMPRA, AJUSTE)
SALIDA_REASONS = (VENTA, DANO, VENCIMIENTO, CONSUMO_INTERNO, AJUSTE)

# ----------------------------------------------------------------- Referencias

REF_SALE = "sale"
REF_ORDER = "order"
REF_ORDER_VOID = "order_void"
REF_PURCHASE = "purchase"
REF_PURCHASE_VOID = "purchase_void"
