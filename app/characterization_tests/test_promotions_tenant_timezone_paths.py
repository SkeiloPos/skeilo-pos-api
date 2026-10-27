"""Spec 138, Historia 2 (FR-006, SC-003) — la vigencia de promociones se evalúa con la zona del TENANT
en todos los caminos de cobro.

Antes de esta spec ocho llamadas a `auto_discount` no recibían el tenant y caían en la zona global de
respaldo (Bogotá): un negocio de Ciudad de México veía descontar una promoción 18:00–20:00 a las 17:30 de
SU hora. Cada camino se prueba contra la misma promoción (50 % llevando 2, ventana 18:00–20:00) y los
mismos instantes:

    instante (UTC)       Bogotá   Ciudad de México   Madrid          ¿aplica?
    2026-10-25 23:30Z    18:30    17:30              00:30 (26/10)   solo Bogotá
    2026-10-25 22:30Z    17:30    16:30              23:30           ninguna (control)
    2026-10-26 00:30Z    19:30    18:30              01:30           Bogotá y México (control positivo)

Caminos: venta directa, cobro de mesa unificado y dividido, su factura previa (`compute_bill`), mesas
fusionadas (`group_bill`), aprobación de un pago QR, confirmación de efectivo y porción en efectivo de un
pago mixto. Los pedidos llevan su instante congelado (`promotion_evaluated_at`, spec 073) como los crea
`create_order`; la venta directa fija el reloj de su módulo.

    python -m unittest app.characterization_tests.test_promotions_tenant_timezone_paths -v
"""
import unittest
from datetime import datetime, time, timezone
from decimal import Decimal

from sqlalchemy import select

from app.api.v1.menu import router as menu_router
from app.api.v1.orders import checkout, tables_advanced
from app.api.v1.sales import service as sales_service
from app.api.v1.sales.schemas import PaymentIn, SaleCreate, SaleItemIn
from app.api.v1.table_sessions import service as ts_service
from app.api.v1.table_sessions.schemas import CloseSessionIn
from app.characterization_tests import orders_fixtures as fx
from app.characterization_tests.cart_fixtures import frozen_now
from app.characterization_tests.fixtures import tenant_en
from app.models.customer_order import CustomerOrder
from app.models.sale import Sale

CONO = Decimal("8000")
SUBTOTAL = CONO * 2
DESCUENTO = CONO  # 50 % sobre 2 conos de 8.000
BOGOTA, MEXICO, MADRID = "America/Bogota", "America/Mexico_City", "Europe/Madrid"


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


I_23_30 = _utc(2026, 10, 25, 23, 30)
I_22_30 = _utc(2026, 10, 25, 22, 30)
I_00_30 = _utc(2026, 10, 26, 0, 30)

# (zona, instante, ¿aplica la promoción 18:00–20:00?)
CASOS = [
    (BOGOTA, I_23_30, True),    # 18:30
    (MEXICO, I_23_30, False),   # 17:30
    (MADRID, I_23_30, False),   # 00:30 del 26/10
    (BOGOTA, I_22_30, False),   # control: 17:30
    (MEXICO, I_22_30, False),   # 16:30
    (MADRID, I_22_30, False),   # 23:30
    (BOGOTA, I_00_30, True),    # control positivo: 19:30
    (MEXICO, I_00_30, True),    # 18:30
    (MADRID, I_00_30, False),   # 01:30
]


def _catalogo(db, *, desde=time(18, 0), hasta=time(20, 0)):
    """Variante de 8.000 con receta (el cobro descuenta inventario) y la promoción 50 % llevando 2."""
    product = fx.make_product(db, category=fx.make_category(db))
    variant = fx.make_variant(db, product=product, price=CONO)
    insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
    fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
    promo = fx.make_promotion(db, status="active", start_time=desde, end_time=hasta)
    fx.add_rule_to_promotion(db, promo, type="percent", value=Decimal("50"), min_qty=2, variants=[variant])
    return variant, promo


# ------------------------------------------------------------------ un helper por camino
# Cada uno devuelve el descuento por promoción que ese camino aplicó.

def _venta_directa(zona, instante) -> Decimal:
    db = fx.new_session()
    variant, _ = _catalogo(db)
    shift = fx.make_cash_shift(db)
    efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
    db.commit()
    data = SaleCreate(
        cash_shift_id=shift.id,
        items=[SaleItemIn(product_variant_id=variant.id, quantity=2, options=[])],
        payments=[PaymentIn(payment_method_id=efectivo.id, amount=SUBTOTAL)],
    )
    with frozen_now(instante, module="app.api.v1.sales.service"):
        sale = sales_service.checkout(db, data, fx.make_user_double(), tenant=tenant_en(zona))
    return sale.discount


def _sesion_de_mesa(db, instante):
    table = fx.make_dining_table(db, status="ocupada")
    ts = fx.make_table_session(db, table=table)
    ana = fx.make_participant(db, table_session=ts, display_name="Ana", display_label="Ana")
    variant, _ = _catalogo(db)
    order = fx.make_customer_order(db, ts, status="abierta", promotion_evaluated_at=instante)
    fx.make_order_item(db, order, variant, quantity=2, participant_id=ana.id, estado_cocina="listo")
    shift = fx.make_cash_shift(db, register=fx.make_cash_register(db))
    efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
    db.commit()
    return ts, ana, shift, efectivo


def _pago(method_id) -> dict:
    return PaymentIn(payment_method_id=method_id, amount=SUBTOTAL).model_dump(mode="json")


def _cobro_de_mesa_unificado(zona, instante) -> Decimal:
    db = fx.new_session()
    ts, _ana, shift, efectivo = _sesion_de_mesa(db, instante)
    data = CloseSessionIn.model_validate({
        "cash_shift_id": str(shift.id), "billing_mode": "unified", "payments": [_pago(efectivo.id)],
    })
    ts_service.close_session(db, ts.id, data, fx.make_user_double(), tenant=tenant_en(zona))
    return db.execute(select(Sale).where(Sale.table_session_id == ts.id)).scalar_one().discount


def _cobro_de_mesa_dividido(zona, instante) -> Decimal:
    db = fx.new_session()
    ts, ana, shift, efectivo = _sesion_de_mesa(db, instante)
    data = CloseSessionIn.model_validate({
        "cash_shift_id": str(shift.id), "billing_mode": "split",
        "splits": [{"participant_id": str(ana.id), "payments": [_pago(efectivo.id)]}],
    })
    ts_service.close_session(db, ts.id, data, fx.make_user_double(), tenant=tenant_en(zona))
    venta = db.execute(select(Sale).where(Sale.table_session_id == ts.id)).scalar_one()
    # En `split` el descuento por promoción NO se suma a `Sale.discount` (lo trae el bloque de cada comensal,
    # `bloque.discount`); el motor deja su efecto en el snapshot `applied_promotions`.
    return sum((Decimal(e["amount"]) for e in venta.applied_promotions or []), Decimal("0"))


def _factura_previa(zona, instante) -> Decimal:
    db = fx.new_session()
    ts, _ana, _shift, _efectivo = _sesion_de_mesa(db, instante)
    bill = ts_service.compute_bill(db, ts.id, tenant=tenant_en(zona))
    return SUBTOTAL - bill.total


def _mesas_fusionadas(zona, instante) -> Decimal:
    db = fx.new_session()
    variant, _ = _catalogo(db)
    pedidos = []
    for _ in range(2):
        ts = fx.make_table_session(db, table=fx.make_dining_table(db, status="ocupada"))
        order = fx.make_customer_order(db, ts, status="abierta", promotion_evaluated_at=instante)
        fx.make_order_item(db, order, variant, quantity=2, estado_cocina="listo")
        pedidos.append(order)
    db.commit()
    group_id = tables_advanced.merge_orders(db, [o.id for o in pedidos])["merged_group_id"]
    bill = tables_advanced.group_bill(db, group_id, tenant=tenant_en(zona))
    return (SUBTOTAL * 2 - bill["total"]) / 2  # descuento por pedido


def _pedido_qr(instante, *, metodo, mixto=False):
    """Pedido de comensal por QR en `recibida` con su instante congelado y un intento de pago pendiente."""
    db = fx.new_session()
    ts = fx.make_table_session(db, table=fx.make_dining_table(db))
    participant = fx.make_participant(db, table_session=ts)
    variant, _ = _catalogo(db)
    order = fx.make_customer_order(
        db, ts, participant=participant, status="recibida", channel="QR_MENU", promotion_evaluated_at=instante,
    )
    fx.make_order_item(db, order, variant, quantity=2, estado_cocina="pendiente")
    kw = {}
    if mixto:
        nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
        metodo = fx.make_payment_method(db, name="Pago mixto", is_cash=False, type="mixed")
        kw = dict(cash_amount=Decimal("6000"), transfer_amount=Decimal("10000"), transfer_method_id=nequi.id)
    else:
        metodo = fx.make_payment_method(
            db, name="Efectivo" if metodo == "efectivo" else "Nequi",
            is_cash=metodo == "efectivo", type="cash" if metodo == "efectivo" else "transfer",
        )
    attempt = fx.make_payment_attempt(db, order, metodo, status="pendiente", **kw)
    shift = fx.make_cash_shift(db)
    db.commit()
    return db, order, attempt, shift


def _venta_del_pedido(db, order) -> Sale:
    return db.execute(select(Sale).where(Sale.customer_order_id == order.id)).scalar_one()


def _aprobacion_de_pago_qr(zona, instante) -> Decimal:
    db, order, attempt, shift = _pedido_qr(instante, metodo="transferencia")
    checkout.approve_payment_attempt(db, attempt.id, shift.id, fx.make_user_double(), tenant=tenant_en(zona))
    return _venta_del_pedido(db, order).discount


def _confirmacion_de_efectivo(zona, instante) -> Decimal:
    db, order, attempt, shift = _pedido_qr(instante, metodo="efectivo")
    checkout.confirm_cash_payment_attempt(
        db, attempt.id, SUBTOTAL, shift.id, fx.make_user_double(), tenant=tenant_en(zona),
    )
    return _venta_del_pedido(db, order).discount


def _porcion_en_efectivo_de_pago_mixto(zona, instante) -> Decimal:
    db, order, attempt, shift = _pedido_qr(instante, metodo=None, mixto=True)
    user, tenant = fx.make_user_double(), tenant_en(zona)
    # La transferencia se verifica primero (no genera venta); la porción en efectivo es la que cierra el cobro.
    checkout.approve_payment_attempt(db, attempt.id, shift.id, user, tenant=tenant)
    checkout.confirm_cash_payment_attempt(db, attempt.id, Decimal("0"), shift.id, user, tenant=tenant)
    return _venta_del_pedido(db, order).discount


CAMINOS = {
    "venta directa": _venta_directa,
    "cobro de mesa unificado": _cobro_de_mesa_unificado,
    "cobro de mesa dividido": _cobro_de_mesa_dividido,
    "factura previa de la sesión (compute_bill)": _factura_previa,
    "mesas fusionadas (group_bill)": _mesas_fusionadas,
    "aprobación de un pago QR": _aprobacion_de_pago_qr,
    "confirmación de efectivo": _confirmacion_de_efectivo,
    "porción en efectivo de un pago mixto": _porcion_en_efectivo_de_pago_mixto,
}


class TestPromocionesEnLaZonaDelTenant(unittest.TestCase):
    def test_cada_camino_evalua_la_vigencia_en_la_zona_del_tenant(self):
        for camino, calcular in CAMINOS.items():
            for zona, instante, aplica in CASOS:
                with self.subTest(camino=camino, zona=zona, instante=instante.isoformat()):
                    esperado = DESCUENTO if aplica else Decimal("0")
                    self.assertEqual(calcular(zona, instante), esperado)


class TestVistaPreviaMenuPublicoYCobroCoinciden(unittest.TestCase):
    """US2-4: para el mismo instante y tenant, lo que anuncia el menú público, lo que muestra la vista previa
    del pedido y lo que cobra el cierre de mesa coinciden."""

    def test_los_tres_coinciden_en_cada_zona_e_instante(self):
        for zona, instante, aplica in CASOS:
            with self.subTest(zona=zona, instante=instante.isoformat()):
                db = fx.new_session()
                ts, _ana, shift, efectivo = _sesion_de_mesa(db, instante)
                tenant = tenant_en(zona)
                order = db.execute(select(CustomerOrder).where(CustomerOrder.table_session_id == ts.id)).scalar_one()

                en_el_menu = len(menu_router._build_menu_promotions(db, instante, tenant)) == 1
                en_la_vista_previa = checkout.compute_checkout_preview(db, order.id, tenant).discount == DESCUENTO
                data = CloseSessionIn.model_validate({
                    "cash_shift_id": str(shift.id), "billing_mode": "unified", "payments": [_pago(efectivo.id)],
                })
                ts_service.close_session(db, ts.id, data, fx.make_user_double(), tenant=tenant)
                en_el_cobro = db.execute(select(Sale).where(Sale.table_session_id == ts.id)).scalar_one().discount == DESCUENTO

                self.assertEqual((en_el_menu, en_la_vista_previa, en_el_cobro), (aplica, aplica, aplica))


class TestHoraRepetidaDeMadrid(unittest.TestCase):
    """Edge case de la spec: el 25/10/2026 Madrid pasa de las 03:00 a las 02:00 y la hora 02:00–03:00 ocurre
    dos veces. Una ventana 02:00–03:00 aplica en las DOS pasadas (00:30Z y 01:30Z son ambas las 02:30 locales)."""

    def test_la_ventana_de_la_hora_repetida_aplica_en_las_dos_pasadas(self):
        for instante in (_utc(2026, 10, 25, 0, 30), _utc(2026, 10, 25, 1, 30)):
            with self.subTest(instante=instante.isoformat()):
                db = fx.new_session()
                variant, _ = _catalogo(db, desde=time(2, 0), hasta=time(3, 0))
                ts = fx.make_table_session(db, table=fx.make_dining_table(db, status="ocupada"))
                order = fx.make_customer_order(db, ts, status="abierta", promotion_evaluated_at=instante)
                fx.make_order_item(db, order, variant, quantity=2, estado_cocina="listo")
                db.commit()
                self.assertEqual(
                    checkout.compute_checkout_preview(db, order.id, tenant_en(MADRID)).discount, DESCUENTO
                )


class TestVentanaDeMadrugadaAlEsteDeUtc(unittest.TestCase):
    """US6-4: una promoción con ventana 00:00–02:00 al este de UTC. A las 00:30 locales de Madrid el día UTC todavía es
    el anterior; la ventana se evalúa en la hora LOCAL del negocio, no en la UTC."""

    def _aplica(self, zona, instante) -> bool:
        db = fx.new_session()
        variant, _ = _catalogo(db, desde=time(0, 0), hasta=time(2, 0))
        ts = fx.make_table_session(db, table=fx.make_dining_table(db, status="ocupada"))
        order = fx.make_customer_order(db, ts, status="abierta", promotion_evaluated_at=instante)
        fx.make_order_item(db, order, variant, quantity=2, estado_cocina="listo")
        db.commit()
        return checkout.compute_checkout_preview(db, order.id, tenant_en(zona)).discount == DESCUENTO

    def test_aplica_a_las_00_30_locales_de_madrid_aunque_el_dia_utc_sea_el_anterior(self):
        instante = _utc(2026, 11, 24, 23, 30)  # 00:30 del 25/11 en Madrid (UTC+1)
        self.assertEqual(instante.date().isoformat(), "2026-11-24")
        self.assertTrue(self._aplica(MADRID, instante))

    def test_en_las_otras_zonas_ese_mismo_instante_no_esta_en_la_ventana(self):
        instante = _utc(2026, 11, 24, 23, 30)  # 18:30 Bogotá · 17:30 Ciudad de México
        self.assertFalse(self._aplica(BOGOTA, instante))
        self.assertFalse(self._aplica(MEXICO, instante))

    def test_a_las_02_30_locales_de_madrid_la_ventana_ya_terminó(self):
        self.assertFalse(self._aplica(MADRID, _utc(2026, 11, 25, 1, 30)))  # 02:30 en Madrid

    def test_a_la_1_30_locales_sigue_dentro(self):
        self.assertTrue(self._aplica(MADRID, _utc(2026, 11, 25, 0, 30)))  # 01:30 en Madrid


if __name__ == "__main__":
    unittest.main()
