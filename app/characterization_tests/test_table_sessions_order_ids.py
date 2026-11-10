"""Tests de cobro independiente de pedidos por mesa (spec 156).

Cubre los Escenarios 1, 2, 5 y 6 de `specs/156-cobro-independiente-pedidos-
mesa/quickstart.md` contra `compute_bill`/`charge_session` (y la función
interna que ambos comparten, `_resolve_orders_to_charge`). A diferencia de
`test_table_sessions_service.py`, estos tests NO llevan el prefijo "CONGELA"
en sus docstrings: `order_ids` es comportamiento nuevo, no existente.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_table_sessions_order_ids -v
"""
from decimal import Decimal
import unittest

from fastapi import HTTPException

from app.characterization_tests import table_sessions_fixtures as fx
from app.api.v1.table_sessions import service
from app.api.v1.table_sessions.schemas import CloseSessionIn
from app.api.v1.sales.schemas import PaymentIn
from app.models.sale import Sale
from sqlalchemy import select

PRECIO = Decimal("10000")


class TestOrderIdsSeleccionDeCobro(unittest.TestCase):
    # ------------------------------------------------------------- Helpers

    def _seed_pedidos(self, db, ts, *, cantidad: int):
        """`cantidad` pedidos independientes (`Pedido N`) en la misma sesión,
        cada uno con una línea cobrable por `PRECIO`."""
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)

        pedidos = []
        for _ in range(cantidad):
            pedido = fx.make_customer_order(db, ts, status="entregada")
            fx.make_order_item(db, pedido, variant)
            pedidos.append(pedido)
        return pedidos

    def _seed_sesion(self, *, cantidad_pedidos: int):
        """Mesa + sesión activa con `cantidad_pedidos` pedidos pendientes de
        cobro + caja abierta."""
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        pedidos = self._seed_pedidos(db, ts, cantidad=cantidad_pedidos)

        register = fx.make_cash_register(db)
        shift = fx.make_cash_shift(db, register=register)
        method = fx.make_payment_method(db)
        cashier = fx.make_user_double()

        db.commit()
        return dict(
            db=db, table=table, ts=ts, pedidos=pedidos,
            shift=shift, method=method, cashier=cashier,
        )

    def _pago(self, method_id, amount):
        return PaymentIn(payment_method_id=method_id, amount=amount).model_dump(mode="json")

    def _charge_data(self, shift_id, method_id, *, order_ids=None, amount):
        payload = {
            "cash_shift_id": str(shift_id),
            "billing_mode": "unified",
            "payments": [self._pago(method_id, amount)],
        }
        if order_ids is not None:
            payload["order_ids"] = [str(oid) for oid in order_ids]
        return CloseSessionIn.model_validate(payload)

    def _ventas(self, db, ts_id) -> list[Sale]:
        db.flush()
        return list(db.execute(select(Sale).where(Sale.table_session_id == ts_id)).scalars())

    # ----------------------------- Escenario 1 (US1): cobrar un único pedido

    def test_compute_bill_con_order_ids_acota_el_total_al_subconjunto(self):
        """Comportamiento NUEVO (spec 156, FR-001/FR-003): `compute_bill`
        con `order_ids=[pedido_42.id]` calcula `total`/`order_ids` solo de
        ese pedido, sin incluir al Pedido 41."""
        s = self._seed_sesion(cantidad_pedidos=2)
        pedido_41, pedido_42 = s["pedidos"]

        resp = service.compute_bill(s["db"], s["ts"].id, order_ids=[pedido_42.id])

        self.assertEqual(resp.order_ids, [pedido_42.id])
        self.assertEqual(resp.total, PRECIO)

    def test_charge_session_con_order_ids_cobra_solo_ese_pedido(self):
        """Comportamiento NUEVO (spec 156, FR-001): cobrar con `order_ids`
        de un solo pedido lo deja `PAID` sin tocar el otro pedido pendiente
        de la misma sesión; la sesión y la mesa siguen activas (no se
        liberan -- `/charge` nunca libera la mesa)."""
        s = self._seed_sesion(cantidad_pedidos=2)
        pedido_41, pedido_42 = s["pedidos"]
        data = self._charge_data(s["shift"].id, s["method"].id, order_ids=[pedido_42.id], amount=PRECIO)

        resp = service.charge_session(s["db"], s["ts"].id, data, s["cashier"])

        self.assertEqual(len(resp.sale_ids), 1)
        s["db"].refresh(pedido_42)
        s["db"].refresh(pedido_41)
        s["db"].refresh(s["ts"])
        self.assertEqual(pedido_42.payment_status, "PAID")
        self.assertEqual(pedido_41.payment_status, "UNPAID")
        self.assertEqual(s["ts"].status, "active")

    # ----------------------------------------------------------- Escenario 5

    def test_order_ids_con_id_ya_pagado_responde_409(self):
        """Comportamiento NUEVO (spec 156, D4 de research.md): un id ya
        `PAID` dentro de `order_ids` hace fallar la petición completa con
        409, listando el id rechazado -- tanto desde `compute_bill` como
        desde `charge_session`."""
        s = self._seed_sesion(cantidad_pedidos=2)
        pedido_41, pedido_42 = s["pedidos"]
        pedido_42.payment_status = "PAID"
        s["db"].commit()

        with self.assertRaises(HTTPException) as ctx:
            service.compute_bill(s["db"], s["ts"].id, order_ids=[pedido_41.id, pedido_42.id])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(str(pedido_42.id), ctx.exception.detail["order_ids"])

        data = self._charge_data(
            s["shift"].id, s["method"].id,
            order_ids=[pedido_41.id, pedido_42.id], amount=PRECIO * 2,
        )
        with self.assertRaises(HTTPException) as ctx2:
            service.charge_session(s["db"], s["ts"].id, data, s["cashier"])
        self.assertEqual(ctx2.exception.status_code, 409)
        self.assertIn(str(pedido_42.id), ctx2.exception.detail["order_ids"])

    def test_order_ids_de_otra_sesion_responde_409(self):
        """Comportamiento NUEVO (spec 156): un id que no pertenece a ESTA
        sesión (es de otra mesa) también se rechaza con 409, aunque sí sea
        un pedido facturable en su propia sesión."""
        s = self._seed_sesion(cantidad_pedidos=1)
        otro = self._seed_sesion(cantidad_pedidos=1)
        pedido_ajeno = otro["pedidos"][0]

        with self.assertRaises(HTTPException) as ctx:
            service.compute_bill(s["db"], s["ts"].id, order_ids=[pedido_ajeno.id])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(str(pedido_ajeno.id), ctx.exception.detail["order_ids"])

    # ----------------------------------------------------------- Escenario 6

    def test_mesa_con_un_solo_pedido_order_ids_no_cambia_el_resultado(self):
        """Comportamiento NUEVO (spec 156, Principio II): con un único
        pedido pendiente, cobrar sin `order_ids` y cobrar con
        `order_ids=[ese id]` producen el mismo resultado observable (una
        `Sale`, pedido `PAID`) -- el caso por defecto no cambió."""
        sin_seleccion = self._seed_sesion(cantidad_pedidos=1)
        pedido_sin = sin_seleccion["pedidos"][0]
        data_sin = self._charge_data(sin_seleccion["shift"].id, sin_seleccion["method"].id, amount=PRECIO)
        resp_sin = service.charge_session(sin_seleccion["db"], sin_seleccion["ts"].id, data_sin, sin_seleccion["cashier"])

        con_seleccion = self._seed_sesion(cantidad_pedidos=1)
        pedido_con = con_seleccion["pedidos"][0]
        data_con = self._charge_data(
            con_seleccion["shift"].id, con_seleccion["method"].id,
            order_ids=[pedido_con.id], amount=PRECIO,
        )
        resp_con = service.charge_session(con_seleccion["db"], con_seleccion["ts"].id, data_con, con_seleccion["cashier"])

        self.assertEqual(len(resp_sin.sale_ids), len(resp_con.sale_ids))
        sin_seleccion["db"].refresh(pedido_sin)
        con_seleccion["db"].refresh(pedido_con)
        self.assertEqual(pedido_sin.payment_status, pedido_con.payment_status)
        self.assertEqual(pedido_sin.payment_status, "PAID")

    # ----------------------------- Escenario 2 (US2): cobrar varios juntos

    def test_charge_session_con_varios_order_ids_cobra_ambos_en_una_sola_llamada(self):
        """Comportamiento NUEVO (spec 156, FR-002): `order_ids` con dos
        pedidos deja ambos `PAID` tras una sola llamada, generando una
        `Sale` (billing_mode='unified') que cubre a los dos."""
        s = self._seed_sesion(cantidad_pedidos=2)
        pedido_41, pedido_42 = s["pedidos"]
        data = self._charge_data(
            s["shift"].id, s["method"].id,
            order_ids=[pedido_41.id, pedido_42.id], amount=PRECIO * 2,
        )

        resp = service.charge_session(s["db"], s["ts"].id, data, s["cashier"])

        self.assertEqual(len(resp.sale_ids), 1)
        s["db"].refresh(pedido_41)
        s["db"].refresh(pedido_42)
        self.assertEqual(pedido_41.payment_status, "PAID")
        self.assertEqual(pedido_42.payment_status, "PAID")

    def test_charge_session_con_dos_de_tres_order_ids_deja_el_tercero_pendiente(self):
        """Acceptance scenario 2 de User Story 2 (`spec.md`): mesa con tres
        pedidos pendientes, `order_ids` con solo dos de ellos -- el tercero
        permanece `UNPAID` tras el cobro."""
        s = self._seed_sesion(cantidad_pedidos=3)
        pedido_41, pedido_42, pedido_43 = s["pedidos"]
        data = self._charge_data(
            s["shift"].id, s["method"].id,
            order_ids=[pedido_41.id, pedido_42.id], amount=PRECIO * 2,
        )

        service.charge_session(s["db"], s["ts"].id, data, s["cashier"])

        s["db"].refresh(pedido_41)
        s["db"].refresh(pedido_42)
        s["db"].refresh(pedido_43)
        self.assertEqual(pedido_41.payment_status, "PAID")
        self.assertEqual(pedido_42.payment_status, "PAID")
        self.assertEqual(pedido_43.payment_status, "UNPAID")

    # ----------------------------- Soporte de US3 (visibilidad tras cobro parcial)

    def test_bill_sin_order_ids_tras_cobro_parcial_solo_refleja_lo_pendiente(self):
        """Acceptance scenario 2 de User Story 1, reutilizado como soporte
        de US3: tras cobrar el Pedido 42 de forma independiente, `GET
        /bill` sin `order_ids` ya no lo incluye -- el total mostrado
        refleja solo el Pedido 41 pendiente."""
        s = self._seed_sesion(cantidad_pedidos=2)
        pedido_41, pedido_42 = s["pedidos"]
        data = self._charge_data(s["shift"].id, s["method"].id, order_ids=[pedido_42.id], amount=PRECIO)
        service.charge_session(s["db"], s["ts"].id, data, s["cashier"])

        resp = service.compute_bill(s["db"], s["ts"].id)

        self.assertEqual(resp.order_ids, [pedido_41.id])
        self.assertEqual(resp.total, PRECIO)


if __name__ == "__main__":
    unittest.main()
