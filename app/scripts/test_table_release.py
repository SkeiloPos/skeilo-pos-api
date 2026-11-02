"""Test de liberación de la mesa: cuándo vuelve a `libre` y cuándo NO.

No hay pytest en el proyecto, así que es un script autoejecutable:

    python -m app.scripts.test_table_release

La regla que cubre, en sus dos mitades (ambas necesarias):

  - **nadie activo** — si queda un comensal dentro, la mesa sigue ocupada aunque
    quien se fue no hubiera pedido nada;
  - **nada que cobrar** — si hay pedidos vivos la mesa **no** se libera aunque no
    quede nadie. Eso es un descuadre real que debe ver el personal; taparlo
    automáticamente escondería dinero sin cobrar.

Y el barrido, que es lo que cubre a quien cierra la pestaña sin pulsar "Salir":
la ventana corta se mide sobre la **última actividad real**, no sobre `expires_at`
—que ya viene desplazado 4 h por la ventana deslizante—; compararlo mal haría que
el barrido no disparase nunca dentro de esas 4 h, que es justo el agujero a cerrar.

Crea su propia mesa desechable y la borra al terminar.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, text

from app.core.config import settings
from app.core.db import with_db
from app.core.scheduler import sweep_orphan_sessions
from app.models.customer_order import CustomerOrder
from app.models.dining_table import DiningTable
from app.models.product_variant import ProductVariant
from app.models.session_participant import SessionParticipant
from app.models.table_session import TableSession
from app.api.v1.cart import service as cart_service
from app.api.v1.table_sessions.service import try_release_if_empty


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
        raise AssertionError(f"{label}: esperado {esperado}, obtenido {actual}")
    print(f"  ok  · {label}")


def _status(schema, table_id) -> str:
    with with_db(schema) as db:
        return db.get(DiningTable, table_id).status


def _nueva_mesa(schema) -> object:
    with with_db(schema) as db:
        table = DiningTable(number=9000 + int(uuid4().int % 900),
                            name=f"mesa-rel-{uuid4().hex[:6]}")
        db.add(table)
        db.commit()
        db.refresh(table)
        return table.id


def _pedido(schema, table_id, participant_id, table_session_id, status_pedido):
    """Inserta un pedido directo (sin pasar por el carrito) en el estado dado."""
    with with_db(schema) as db:
        variant = db.execute(select(ProductVariant).limit(1)).scalar_one_or_none()
        if variant is None:
            raise SystemExit("El tenant no tiene variantes; crea catálogo primero.")
        order = CustomerOrder(
            dining_table_id=table_id,
            table_session_id=table_session_id,
            participant_id=participant_id,
            channel="QR_MENU",
            order_type="DINE_IN",
            status=status_pedido,
        )
        db.add(order)
        db.commit()
        return order.id


def _envejecer(schema, participant_id, minutos_inactivo):
    """Simula que el comensal lleva N minutos sin actividad.

    `expires_at` = última actividad + SESSION_TTL_MINUTES, así que para fingir N
    minutos de inactividad hay que retroceder ese mismo desplazamiento.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ultima_actividad = now - timedelta(minutes=minutos_inactivo)
    with with_db(schema) as db:
        p = db.get(SessionParticipant, participant_id)
        p.expires_at = ultima_actividad + timedelta(minutes=settings.SESSION_TTL_MINUTES)
        db.commit()


def _cleanup(schema: str, table_ids) -> None:
    with with_db(schema) as db:
        for table_id in table_ids:
            t = str(table_id)
            db.execute(text(f'DELETE FROM "{schema}".order_items WHERE order_id IN '
                            f'(SELECT id FROM "{schema}".customer_orders WHERE dining_table_id = :t)'),
                       {"t": t})
            db.execute(text(f'DELETE FROM "{schema}".customer_orders WHERE dining_table_id = :t'), {"t": t})
            db.execute(text(f'''
                DELETE FROM "{schema}".carts WHERE participant_id IN (
                    SELECT id FROM "{schema}".session_participants WHERE dining_table_id = :t)
            '''), {"t": t})
            db.execute(text(f'DELETE FROM "{schema}".session_participants WHERE dining_table_id = :t'), {"t": t})
            db.execute(text(f'DELETE FROM "{schema}".table_sessions WHERE dining_table_id = :t'), {"t": t})
            db.execute(text(f'DELETE FROM "{schema}".dining_tables WHERE id = :t'), {"t": t})
        db.commit()


def main():
    tenant_id, schema = _tenant()
    print(f"Liberación de mesa abandonada (tenant: {schema})")
    mesas = []

    try:
        # --- 1. un comensal escanea y se va sin pedir ------------------------
        t1 = _nueva_mesa(schema); mesas.append(t1)
        with with_db(schema) as db:
            r = cart_service.open_session(db, tenant_id, db.get(DiningTable, t1), "Ana")
        _check("escanear ocupa la mesa", _status(schema, t1), "ocupada")

        with with_db(schema) as db:
            cart_service.leave_session(db, db.get(SessionParticipant, r.participant_id))
        _check("el último comensal sale → mesa libre", _status(schema, t1), "libre")
        with with_db(schema) as db:
            _check("y la sesión queda cerrada",
                   db.get(TableSession, r.table_session_id).status, "closed")

        # --- 2. dos comensales: sale uno, la mesa sigue ocupada --------------
        t2 = _nueva_mesa(schema); mesas.append(t2)
        with with_db(schema) as db:
            a = cart_service.open_session(db, tenant_id, db.get(DiningTable, t2), "Ana")
        with with_db(schema) as db:
            b = cart_service.open_session(db, tenant_id, db.get(DiningTable, t2), "Beto")

        with with_db(schema) as db:
            cart_service.leave_session(db, db.get(SessionParticipant, a.participant_id))
        _check("sale uno de dos → la mesa SIGUE ocupada", _status(schema, t2), "ocupada")

        with with_db(schema) as db:
            cart_service.leave_session(db, db.get(SessionParticipant, b.participant_id))
        _check("sale el segundo → mesa libre", _status(schema, t2), "libre")

        # --- 3. comensal con pedido vivo que se va --------------------------
        t3 = _nueva_mesa(schema); mesas.append(t3)
        with with_db(schema) as db:
            c = cart_service.open_session(db, tenant_id, db.get(DiningTable, t3), "Ana")
        _pedido(schema, t3, c.participant_id, c.table_session_id, "pendiente")

        with with_db(schema) as db:
            cart_service.leave_session(db, db.get(SessionParticipant, c.participant_id))
        _check("se va dejando un pedido sin cobrar → mesa SIGUE ocupada",
               _status(schema, t3), "ocupada")

        # --- 4. el pedido estaba cancelado: sí libera -----------------------
        t4 = _nueva_mesa(schema); mesas.append(t4)
        with with_db(schema) as db:
            d = cart_service.open_session(db, tenant_id, db.get(DiningTable, t4), "Ana")
        _pedido(schema, t4, d.participant_id, d.table_session_id, "cancelada")

        with with_db(schema) as db:
            cart_service.leave_session(db, db.get(SessionParticipant, d.participant_id))
        _check("un pedido cancelado no retiene la mesa", _status(schema, t4), "libre")

        # --- 5. barrido: inactivo más de la ventana, sin pedidos ------------
        t5 = _nueva_mesa(schema); mesas.append(t5)
        with with_db(schema) as db:
            e = cart_service.open_session(db, tenant_id, db.get(DiningTable, t5), "Ana")
        _envejecer(schema, e.participant_id, settings.EMPTY_SESSION_TTL_MINUTES + 1)

        sweep_orphan_sessions()
        _check("el barrido suelta la mesa abandonada sin pedir",
               _status(schema, t5), "libre")

        # --- 6. barrido: inactivo pero DENTRO de la ventana -----------------
        t6 = _nueva_mesa(schema); mesas.append(t6)
        with with_db(schema) as db:
            f = cart_service.open_session(db, tenant_id, db.get(DiningTable, t6), "Ana")
        _envejecer(schema, f.participant_id, max(settings.EMPTY_SESSION_TTL_MINUTES - 10, 1))

        sweep_orphan_sessions()
        _check("dentro de la ventana el barrido NO la toca",
               _status(schema, t6), "ocupada")

        # --- 7. barrido: inactivo pero con pedido vivo ----------------------
        t7 = _nueva_mesa(schema); mesas.append(t7)
        with with_db(schema) as db:
            g = cart_service.open_session(db, tenant_id, db.get(DiningTable, t7), "Ana")
        _pedido(schema, t7, g.participant_id, g.table_session_id, "pendiente")
        _envejecer(schema, g.participant_id, settings.EMPTY_SESSION_TTL_MINUTES + 1)

        sweep_orphan_sessions()
        _check("con un pedido vivo el barrido NO la suelta",
               _status(schema, t7), "ocupada")

        # --- 8. try_release_if_empty es idempotente -------------------------
        with with_db(schema) as db:
            _check("liberar una sesión ya cerrada no hace nada",
                   # spec 089: devuelve las sesiones que cerró (lista vacía = no liberó nada).
                   bool(try_release_if_empty(db, r.table_session_id)), False)

        print("\nTODO OK ✔")
    finally:
        _cleanup(schema, mesas)


if __name__ == "__main__":
    main()
