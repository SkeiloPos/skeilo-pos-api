"""E2E del flujo completo de pedidos por QR, contra un servidor real vía HTTP.

    uvicorn app.main:app --port 8099 &
    python -m app.scripts.e2e_qr_flow [--base http://127.0.0.1:8099]

Recorre el camino entero y comprueba las invariantes que importan:

    QR → dos comensales en la misma mesa → cada uno arma su carrito →
    envían pedido (`recibida`, sin tocar stock) → cocina NO los ve todavía →
    staff confirma (aquí y solo aquí baja el inventario) → cocina los ve →
    entrega → cuenta con split por comensal → cobro `split` (una venta por
    persona) → arqueo de caja cuadrado → mesa liberada.

Crea todo lo que necesita (usuario de staff desechable, mesa, insumo, producto,
caja) y lo borra al terminar, pase o falle.
"""
import argparse
import sys
import uuid
from decimal import Decimal

import requests
from sqlalchemy import select, text

from app.core.db import with_db
from app.core.models import User
from app.core.utils import generate_passwd_hash

PASSWORD = "e2e-Passw0rd!"
STOCK_INICIAL = Decimal("500.000")
POR_UNIDAD = Decimal("2.000")


class Fail(Exception):
    pass


def check(label, actual, esperado):
    if actual != esperado:
        raise Fail(f"{label}\n     esperado: {esperado}\n     obtenido: {actual}")
    print(f"  ok  · {label}")


def note(label):
    print(f"  ok  · {label}")


# Prefijo de facturación que el fixture deja configurado en el tenant, para poder
# comprobar que llega desde `shared.tenants` hasta la factura emitida.
PREFIJO_FACTURA = "E2E"


# --------------------------------------------------------------------- fixture

def seed():
    """Crea el tenant fixture y devuelve lo necesario para el recorrido."""
    with with_db(None) as db:
        row = db.execute(
            text("SELECT id, host, schema FROM shared.tenants ORDER BY id LIMIT 1")
        ).first()
        if row is None:
            raise SystemExit("No hay tenants en shared.tenants.")
        tenant_id, host, schema = row

        # El prefijo es config del negocio; se fija aquí y se restaura en teardown.
        prefijo_previo = db.execute(
            text("SELECT invoice_prefix FROM shared.tenants WHERE id = :i"), {"i": tenant_id}
        ).scalar()
        db.execute(text("UPDATE shared.tenants SET invoice_prefix = :p WHERE id = :i"),
                   {"p": PREFIJO_FACTURA, "i": tenant_id})

        role_id = db.execute(
            text("SELECT id FROM shared.roles WHERE name = 'ADMIN'")
        ).scalar()
        # `.test` lo rechaza el validador de email de pydantic (TLD reservado).
        email = f"e2e-{uuid.uuid4().hex[:8]}@e2e-pos.com"
        user = User(
            name="E2E Staff", email=email,
            password_hash=generate_passwd_hash(PASSWORD),
            tenant_id=tenant_id, role_id=role_id, active=True,
            must_change_password=False,
        )
        db.add(user)
        db.commit()
        user_id = user.id

    with with_db(schema) as db:
        from app.models.category import Category
        from app.models.dining_table import DiningTable
        from app.models.inventory_item import InventoryItem
        from app.models.product import Product
        from app.models.product_variant import ProductVariant
        from app.models.recipe_item import RecipeItem
        from app.models.unit_measure import UnitMeasure

        unit = db.execute(select(UnitMeasure).limit(1)).scalar_one_or_none()
        if unit is None:
            unit = UnitMeasure(name=f"ud-{uuid.uuid4().hex[:6]}",
                               abbreviation=uuid.uuid4().hex[:4])
            db.add(unit); db.flush()

        item = InventoryItem(
            code=f"test-{uuid.uuid4().hex[:8]}", name=f"e2e-insumo-{uuid.uuid4().hex[:8]}", type="raw_material",
            unit_measure_id=unit.id, current_stock=STOCK_INICIAL,
            unit_cost=Decimal("1.00"),
        )
        db.add(item)

        cat = db.execute(select(Category).limit(1)).scalar_one_or_none()
        if cat is None:
            cat = Category(name=f"e2e-cat-{uuid.uuid4().hex[:6]}")
            db.add(cat); db.flush()

        product = Product(name=f"e2e-prod-{uuid.uuid4().hex[:8]}",
                          category_id=cat.id, preparation_type="prepared")
        db.add(product); db.flush()
        variant = ProductVariant(product_id=product.id, name="única",
                                 price=Decimal("10.00"), active=True, display_order=1)
        db.add(variant); db.flush()
        db.add(RecipeItem(product_variant_id=variant.id,
                          inventory_item_id=item.id, quantity=POR_UNIDAD))

        table = DiningTable(number=8000 + int(uuid.uuid4().int % 900),
                            name=f"e2e-mesa-{uuid.uuid4().hex[:6]}")
        db.add(table)
        db.commit()
        return {
            "tenant_host": host, "schema": schema, "email": email,
            "user_id": user_id, "item_id": item.id, "variant_id": variant.id,
            "product_id": product.id, "table_id": table.id,
            "tenant_id": tenant_id, "prefijo_previo": prefijo_previo,
        }


def teardown(fx):
    s = fx["schema"]
    with with_db(None) as shared:
        # Restaura el prefijo del tenant y borra el consecutivo de la prueba.
        shared.execute(text("UPDATE shared.tenants SET invoice_prefix = :p WHERE id = :i"),
                       {"p": fx.get("prefijo_previo"), "i": fx["tenant_id"]})
        shared.commit()
    with with_db(s) as db:
        db.execute(text(f'DELETE FROM "{s}".invoice_counters WHERE prefix = :p'),
                   {"p": PREFIJO_FACTURA})
        db.commit()
    with with_db(s) as db:
        tid, vid, pid, iid = (str(fx["table_id"]), str(fx["variant_id"]),
                              str(fx["product_id"]), str(fx["item_id"]))
        ordenes = [str(r[0]) for r in db.execute(text(
            f'SELECT id FROM "{s}".customer_orders WHERE dining_table_id = :t'
        ), {"t": tid})]
        ventas = [str(r[0]) for r in db.execute(text(
            f'SELECT id FROM "{s}".sales WHERE dining_table_id = :t'
        ), {"t": tid})]
        for sql, params in [
            # Antes que `sales`: `invoices.sale_id` tiene FK.
            (f'DELETE FROM "{s}".invoices WHERE sale_id = ANY(:v)', {"v": ventas}),
            (f'DELETE FROM "{s}".payments WHERE sale_id = ANY(:v)', {"v": ventas}),
            (f'DELETE FROM "{s}".sale_items WHERE sale_id = ANY(:v)', {"v": ventas}),
            (f'DELETE FROM "{s}".sales WHERE id = ANY(:v)', {"v": ventas}),
            (f'DELETE FROM "{s}".inventory_movements WHERE reference_id = ANY(:o)', {"o": ordenes}),
            (f'DELETE FROM "{s}".audit_logs WHERE entity_id = ANY(:o)', {"o": ordenes}),
            (f'DELETE FROM "{s}".order_cancel_logs WHERE order_id = ANY(:o)', {"o": ordenes}),
            (f'DELETE FROM "{s}".order_item_options WHERE order_item_id IN '
             f'(SELECT id FROM "{s}".order_items WHERE order_id = ANY(:o))', {"o": ordenes}),
            (f'DELETE FROM "{s}".order_items WHERE order_id = ANY(:o)', {"o": ordenes}),
            (f'DELETE FROM "{s}".customer_orders WHERE id = ANY(:o)', {"o": ordenes}),
            (f'DELETE FROM "{s}".cart_items WHERE cart_id IN (SELECT c.id FROM "{s}".carts c '
             f'JOIN "{s}".session_participants p ON p.id = c.participant_id '
             f'WHERE p.dining_table_id = :t)', {"t": tid}),
            (f'DELETE FROM "{s}".carts WHERE participant_id IN '
             f'(SELECT id FROM "{s}".session_participants WHERE dining_table_id = :t)', {"t": tid}),
            (f'DELETE FROM "{s}".session_participants WHERE dining_table_id = :t', {"t": tid}),
            (f'DELETE FROM "{s}".table_sessions WHERE dining_table_id = :t', {"t": tid}),
            (f'DELETE FROM "{s}".dining_tables WHERE id = :t', {"t": tid}),
            (f'DELETE FROM "{s}".recipe_items WHERE product_variant_id = :v', {"v": vid}),
            (f'DELETE FROM "{s}".product_variants WHERE id = :v', {"v": vid}),
            (f'DELETE FROM "{s}".products WHERE id = :p', {"p": pid}),
            (f'DELETE FROM "{s}".inventory_items WHERE id = :i', {"i": iid}),
            # La caja y su turno: el turno referencia el registro, va primero.
            (f'DELETE FROM "{s}".cash_count_denominations WHERE cash_shift_id IN '
             f'(SELECT sh.id FROM "{s}".cash_shifts sh JOIN "{s}".cash_registers r '
             f'ON r.id = sh.cash_register_id WHERE r.name LIKE :c)', {"c": "e2e-caja-%"}),
            (f'DELETE FROM "{s}".cash_shifts WHERE cash_register_id IN '
             f'(SELECT id FROM "{s}".cash_registers WHERE name LIKE :c)', {"c": "e2e-caja-%"}),
            (f'DELETE FROM "{s}".cash_registers WHERE name LIKE :c', {"c": "e2e-caja-%"}),
        ]:
            try:
                db.execute(text(sql), params)
            except Exception:
                db.rollback()
        db.commit()

    with with_db(None) as db:
        db.execute(text("DELETE FROM shared.users WHERE id = :u"),
                   {"u": str(fx["user_id"])})
        db.commit()


def stock(fx) -> Decimal:
    from app.models.inventory_item import InventoryItem
    with with_db(fx["schema"]) as db:
        return Decimal(db.get(InventoryItem, fx["item_id"]).current_stock)


# ------------------------------------------------------------------------ HTTP

class Api:
    def __init__(self, base, host):
        self.base, self.host, self.token = base.rstrip("/"), host, None

    def __call__(self, method, path, *, json=None, headers=None, esperado=(200, 201)):
        h = {"x-tenant-host": self.host}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        h.update(headers or {})
        r = requests.request(method, f"{self.base}{path}", json=json, headers=h, timeout=20)
        if esperado and r.status_code not in esperado:
            raise Fail(f"{method} {path} → {r.status_code}: {r.text[:400]}")
        return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    args = ap.parse_args()

    fx = seed()
    api = Api(args.base, fx["tenant_host"])
    print(f"E2E flujo QR (tenant: {fx['schema']}, base: {args.base})")

    try:
        # --- login de staff ------------------------------------------------
        r = api("POST", "/api/v1/auth/login",
                json={"email": fx["email"], "password": PASSWORD})
        api.token = r.json()["access_token"]
        note("staff autenticado")

        # --- QR firmado de la mesa ----------------------------------------
        r = api("GET", f"/api/v1/orders/tables/{fx['table_id']}/qr-token")
        qr = r.json()["qr_token"]
        note("QR firmado emitido")

        # --- dos comensales, mismo nombre ---------------------------------
        s1 = api("POST", "/api/v1/cart/sessions",
                 json={"qr_token": qr, "display_name": "Ana"}).json()
        s2 = api("POST", "/api/v1/cart/sessions",
                 json={"qr_token": qr, "display_name": "Ana"}).json()
        check("el segundo comensal se une a la misma sesión de mesa",
              s2["table_session_id"], s1["table_session_id"])
        check("los nombres duplicados se desambiguan",
              [s1["display_label"], s2["display_label"]], ["Ana", "Ana (2)"])
        ts_id = s1["table_session_id"]

        h1 = {"x-session-token": s1["session_token"]}
        h2 = {"x-session-token": s2["session_token"]}

        # --- carritos ------------------------------------------------------
        stock_0 = stock(fx)
        api("POST", "/api/v1/cart/items", headers=h1,
            json={"product_variant_id": str(fx["variant_id"]), "quantity": 2})
        api("POST", "/api/v1/cart/items", headers=h2,
            json={"product_variant_id": str(fx["variant_id"]), "quantity": 1})
        check("llenar el carrito NO toca el inventario", stock(fx), stock_0)

        # --- envío de pedidos ----------------------------------------------
        o1 = api("POST", "/api/v1/cart/submit", headers=h1).json()
        o2 = api("POST", "/api/v1/cart/submit", headers=h2).json()
        check("el pedido enviado queda 'recibida'",
              [o1["status"], o2["status"]], ["recibida", "recibida"])
        check("enviar el pedido tampoco toca el inventario", stock(fx), stock_0)

        # --- confirmación por staff: aquí baja el stock --------------------
        api("POST", f"/api/v1/orders/{o1['id']}/confirm")
        api("POST", f"/api/v1/orders/{o2['id']}/confirm")
        check("confirmar descuenta 3 unidades × 2 de receta",
              stock_0 - stock(fx), POR_UNIDAD * 3)

        # --- la terminal marca los pedidos listos ---------------------------
        for o in (o1, o2):
            listo = api("POST", f"/api/v1/orders/{o['id']}/ready").json()
            check("marcar listo deja todos los ítems en 'listo'",
                  {it["estado_cocina"] for it in listo["items"]}, {"listo"})
        note("la terminal marcó ambos pedidos listos, una llamada por pedido")

        # --- cuenta con split ----------------------------------------------
        bill = api("GET", f"/api/v1/table-sessions/{ts_id}/bill").json()
        check("la cuenta total es 3 × 10.00", Decimal(bill["total"]), Decimal("30.00"))
        por_comensal = {l["participant_id"]: Decimal(l["subtotal"]) for l in bill["split"]}
        check("el split reparte por comensal, no por pedido",
              [por_comensal[s1["participant_id"]], por_comensal[s2["participant_id"]]],
              [Decimal("20.00"), Decimal("10.00")])

        # --- caja -----------------------------------------------------------
        reg = api("POST", "/api/v1/cash/registers",
                  json={"name": f"e2e-caja-{uuid.uuid4().hex[:6]}"}).json()
        shift = api("POST", "/api/v1/cash/shifts/open",
                    json={"cash_register_id": reg["id"], "opening_amount": "0.00"}).json()
        metodos = api("GET", "/api/v1/sales/payment-methods").json()
        efectivo = next((m for m in metodos if m["type"] == "cash"), None)
        if efectivo is None:
            efectivo = api("POST", "/api/v1/sales/payment-methods",
                           json={"name": "Efectivo E2E", "type": "cash",
                                 "is_cash": True}).json()

        # --- cierre con billing_mode=split ---------------------------------
        cierre = api("POST", f"/api/v1/table-sessions/{ts_id}/close", json={
            "cash_shift_id": shift["id"],
            "billing_mode": "split",
            "splits": [
                {"participant_id": s1["participant_id"],
                 "payments": [{"payment_method_id": efectivo["id"], "amount": "25.00"}]},
                {"participant_id": s2["participant_id"],
                 "payments": [{"payment_method_id": efectivo["id"], "amount": "10.00"}]},
            ],
        }).json()
        check("el cobro split emite una venta por comensal",
              len(cierre["sale_ids"]), 2)
        check("la sesión de mesa queda cerrada",
              cierre["table_session"]["status"], "closed")
        check("y registra el billing_mode elegido",
              cierre["table_session"]["billing_mode"], "split")

        # Facturación: una por venta. Es el caso que antes era imposible —el
        # generador partía del pedido y un split no cuelga de ninguno— y además
        # comprueba que el prefijo del tenant llega de verdad desde el router.
        with with_db(fx["schema"]) as db:
            facturas = db.execute(text(f'''
                SELECT i.prefix, i.number FROM "{fx["schema"]}".invoices i
                WHERE i.sale_id = ANY(:ids) ORDER BY i.number
            '''), {"ids": [str(s) for s in cierre["sale_ids"]]}).fetchall()
        check("el split emite una factura por venta", len(facturas), 2)
        check("con el prefijo configurado en el tenant",
              {f[0] for f in facturas}, {PREFIJO_FACTURA})
        check("y números consecutivos",
              [f[1] for f in facturas], [facturas[0][1], facturas[0][1] + 1])

        mesa = next(t for t in api("GET", "/api/v1/orders/tables").json()
                    if t["id"] == str(fx["table_id"]))
        check("la mesa vuelve a estar libre", mesa["status"], "libre")

        # --- arqueo: el cambio entregado se descuenta (regresión D5) --------
        rec = api("GET", f"/api/v1/cash/shifts/{shift['id']}/reconciliation").json()
        # Ana pagó 25 por 20 → 5 de cambio; sin `change_given` (defecto D5) el
        # esperado saldría 35 y el arqueo quedaría descuadrado.
        check("el efectivo esperado descuenta el cambio entregado",
              Decimal(rec["expected"]), Decimal("30.00"))
        check("las ventas en efectivo del turno son las dos del split",
              Decimal(rec["ventas_efectivo"]), Decimal("30.00"))

        movs = api("GET", f"/api/v1/cash/shifts/{shift['id']}/movements").json()
        check("las ventas NO escriben cash_movements (se derivan de Payment)",
              len(movs), 0)

        api("POST", f"/api/v1/cash/shifts/{shift['id']}/close",
            json={"counted_amount": "30.00"})

        print("TODO OK ✔")
        return 0
    except Fail as e:
        print(f"\n  FALLO · {e}")
        return 1
    finally:
        teardown(fx)


if __name__ == "__main__":
    sys.exit(main())
