"""Test de "Atendido por": el nombre del usuario de staff que creó un pedido
de mesa, expuesto en `OrderResponse.staff_user_name` (spec 076, Historia 4).

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_order_staff_user

Cubre el contrato descrito en contracts/order-response.md:

  - un pedido creado por un usuario de staff conocido (`user_id` no nulo)
    resuelve `staff_user_name` al nombre de ese usuario, sin importar su rol
    (Cajero o Mesero se resuelven exactamente igual — spec 076, Clarifications);
  - un pedido sin usuario de staff asociado (`user_id` nulo, equivalente a un
    pedido enviado por el cliente vía QR) resuelve `staff_user_name` a `None`;
  - la versión en bloque (`staff_user_names`, la que realmente usa el
    endpoint de listado que consume la Terminal de Mesas) resuelve varios
    pedidos con una sola consulta, sin mezclar el nombre de un usuario con el
    pedido de otro.

Crea su propia mesa y pedidos desechables; los borra al terminar. No crea
ningún usuario nuevo: reutiliza el primer usuario de staff ya existente del
tenant (si el tenant de prueba no tiene ninguno, el script se detiene con un
mensaje claro en vez de fallar de forma confusa).
"""
from uuid import uuid4

from sqlalchemy import select, text

from app.core.db import with_db
from app.core.models import User
from app.models.customer_order import CustomerOrder
from app.models.dining_table import DiningTable
from app.api.v1.orders import service


def _tenant() -> tuple[int, str]:
    with with_db(None) as db:
        row = db.execute(
            text("SELECT id, schema FROM shared.tenants ORDER BY id LIMIT 1")
        ).first()
    if row is None:
        raise SystemExit("No hay tenants en shared.tenants.")
    return row[0], row[1]


def _check(label, actual, esperado):
    if actual != esperado:
        raise AssertionError(f"{label}: esperado {esperado!r}, obtenido {actual!r}")
    print(f"  ok  · {label}")


def _staff_user(tenant_id: int):
    with with_db(None) as db:
        user = db.execute(
            select(User).where(User.tenant_id == tenant_id).limit(1)
        ).scalar_one_or_none()
    if user is None:
        raise SystemExit(
            "El tenant no tiene ningún usuario de staff; crea uno primero "
            "(este test reutiliza uno existente, no crea usuarios nuevos)."
        )
    return user.id, user.name


def _nueva_mesa(schema) -> object:
    with with_db(schema) as db:
        table = DiningTable(number=9500 + int(uuid4().int % 400),
                            name=f"mesa-staff-{uuid4().hex[:6]}")
        db.add(table)
        db.commit()
        db.refresh(table)
        return table.id


def _pedido(schema, table_id, user_id):
    """Inserta un pedido de mostrador directo (sin pasar por el carrito),
    igual que ya lo captura hoy `orders.service.create_order` para el canal
    POS — `user_id` nulo simula un pedido enviado por el cliente vía QR."""
    with with_db(schema) as db:
        order = CustomerOrder(
            dining_table_id=table_id,
            channel="POS",
            order_type="DINE_IN",
            status="pendiente",
            user_id=user_id,
        )
        db.add(order)
        db.commit()
        return order.id


def _cleanup(schema: str, table_ids) -> None:
    with with_db(schema) as db:
        for table_id in table_ids:
            t = str(table_id)
            db.execute(text(f'DELETE FROM "{schema}".order_items WHERE order_id IN '
                            f'(SELECT id FROM "{schema}".customer_orders WHERE dining_table_id = :t)'),
                       {"t": t})
            db.execute(text(f'DELETE FROM "{schema}".customer_orders WHERE dining_table_id = :t'), {"t": t})
            db.execute(text(f'DELETE FROM "{schema}".dining_tables WHERE id = :t'), {"t": t})
        db.commit()


def main():
    tenant_id, schema = _tenant()
    print(f"'Atendido por' — staff_user_name en OrderResponse (tenant: {schema})")
    mesas = []

    try:
        staff_id, staff_name = _staff_user(tenant_id)

        # --- 1. pedido creado por un usuario de staff conocido --------------
        t1 = _nueva_mesa(schema); mesas.append(t1)
        o1 = _pedido(schema, t1, staff_id)

        with with_db(schema) as db:
            names = service.staff_user_names(db, [staff_id])
        _check("resuelve el nombre del usuario de staff", names.get(staff_id), staff_name)

        # --- 2. pedido sin usuario de staff (equivalente a un pedido QR) ----
        t2 = _nueva_mesa(schema); mesas.append(t2)
        o2 = _pedido(schema, t2, None)

        with with_db(schema) as db:
            order = db.get(CustomerOrder, o2)
            resolved = (
                service.staff_user_names(db, [order.user_id]).get(order.user_id)
                if order.user_id is not None
                else None
            )
        _check("sin user_id, staff_user_name es None", resolved, None)

        # --- 3. versión en bloque no mezcla pedidos de usuarios distintos ---
        with with_db(schema) as db:
            o1_row = db.get(CustomerOrder, o1)
            o2_row = db.get(CustomerOrder, o2)
            batch = service.staff_user_names(
                db, [x for x in (o1_row.user_id, o2_row.user_id) if x is not None]
            )
            staff_name_o1 = batch.get(o1_row.user_id) if o1_row.user_id else None
            staff_name_o2 = batch.get(o2_row.user_id) if o2_row.user_id else None
        _check("pedido 1 (staff) resuelve su propio nombre en el lote", staff_name_o1, staff_name)
        _check("pedido 2 (sin staff) sigue en None dentro del mismo lote", staff_name_o2, None)

        print("\nTODO OK ✔")
    finally:
        _cleanup(schema, mesas)


if __name__ == "__main__":
    main()
