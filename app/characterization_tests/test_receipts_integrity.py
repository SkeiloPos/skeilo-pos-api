"""Tests de la nueva funcionalidad — spec 088-integridad-referencias-r2 (A-93), US5: el comprobante
del comensal (`cart/service.py`: `submit_cart` y `attach_receipt`) es siempre un archivo existente, de
la carpeta `comprobantes` y del propio negocio; en base de datos se guarda la **key** y las tres
respuestas hacia el cajero/comensal entregan una URL lista para renderizar (FR-008).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_receipts_integrity -v
"""
from datetime import datetime
from decimal import Decimal
import unittest
from unittest import mock
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from app.characterization_tests import cart_fixtures as fx
from app.api.v1.cart import service
from app.api.v1.cart.schemas import DinerPaymentAttempt
from app.api.v1.orders.schemas import CurrentPaymentAttemptSummary, PaymentAttemptResponse
from app.core.storage import StorageUnavailable
from app.models.cart import Cart
from app.models.customer_order import CustomerOrder
from app.models.order_payment_attempt import OrderPaymentAttempt

SCHEMA = "acme"
KEY = f"{SCHEMA}/comprobantes/9c1e4d1c4aa4c68ba7b32642334d084.jpg"
URL_LEGACY = f"https://example.invalid/{KEY}"
URL_ASSETS = f"https://assets.example.invalid/{KEY}"
URL_OTHER = "https://cdn.otro.com/comprobante.jpg"
EXISTS = "app.core.asset_refs.object_exists"

MSG_INVALID = "El comprobante no pertenece a este negocio o no es válido."
MSG_FOREIGN = "El comprobante debe ser un archivo subido desde la aplicación."
MSG_NOT_FOUND = "La imagen no se encontró en el almacenamiento. Sube el archivo de nuevo."


def _seed_carrito():
    db = fx.new_session()
    table = fx.make_dining_table(db)
    ts = fx.make_table_session(db, table=table)
    participant = fx.make_participant(db, table_session=ts)
    category = fx.make_category(db)
    product = fx.make_product(db, category=category)
    variant = fx.make_variant(db, product=product, price=Decimal("8000"))
    cart = fx.make_cart(db, participant=participant)
    fx.make_cart_item(db, cart, variant)
    nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
    efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
    db.commit()
    return db, participant, nequi, efectivo


def _seed_intento():
    db = fx.new_session()
    table = fx.make_dining_table(db)
    ts = fx.make_table_session(db, table=table)
    participant = fx.make_participant(db, table_session=ts)
    order = fx.make_customer_order(db, participant)
    nequi = fx.make_payment_method(db, name="Nequi", is_cash=False, type="transfer")
    db.commit()
    attempt = service.create_payment_attempt(db, participant.id, order.id, nequi.id)
    return db, participant, attempt


def _count(db, model):
    return db.execute(select(func.count()).select_from(model)).scalar_one()


class TestSubmitCartComprobante(unittest.TestCase):
    def _submit(self, db, participant, method, value, *, exists=True):
        with mock.patch(EXISTS, return_value=exists):
            return service.submit_cart(db, participant, method.id, receipt_file_url=value, tenant_schema=SCHEMA)

    def test_se_guarda_solo_la_key_con_key_directa_url_vieja_y_url_de_assets(self):
        for label, value in (("key", KEY), ("url dominio anterior", URL_LEGACY), ("url assets", URL_ASSETS)):
            with self.subTest(valor=label):
                db, participant, nequi, _ = _seed_carrito()
                order = self._submit(db, participant, nequi, value)
                attempt = db.execute(
                    select(OrderPaymentAttempt).where(OrderPaymentAttempt.order_id == order.id)
                ).scalar_one()
                self.assertEqual(attempt.receipt_file_url, KEY)

    def test_comprobantes_invalidos_son_422_sin_orden_ni_intento_y_sin_borrar_el_carrito(self):
        casos = [
            ("inventada", KEY, dict(exists=False), MSG_NOT_FOUND),
            ("de otro negocio", "globex/comprobantes/x.jpg", {}, MSG_INVALID),
            ("de otra carpeta", "acme/products/x.jpg", {}, MSG_INVALID),
            ("con recorrido", "acme/comprobantes/../x.jpg", {}, MSG_INVALID),
            ("otro origen", URL_OTHER, {}, MSG_FOREIGN),
        ]
        for label, value, kwargs, message in casos:
            with self.subTest(caso=label):
                db, participant, nequi, _ = _seed_carrito()
                with self.assertRaises(HTTPException) as ctx:
                    self._submit(db, participant, nequi, value, **kwargs)
                self.assertEqual(ctx.exception.status_code, 422)
                self.assertEqual(ctx.exception.detail, message)
                self.assertEqual(_count(db, CustomerOrder), 0)
                self.assertEqual(_count(db, OrderPaymentAttempt), 0)
                self.assertEqual(_count(db, Cart), 1)  # el carrito sigue ahí

    def test_almacenamiento_caido_es_503_sin_orden_ni_intento(self):
        db, participant, nequi, _ = _seed_carrito()
        with mock.patch(EXISTS, side_effect=StorageUnavailable("x")):
            with self.assertRaises(HTTPException) as ctx:
                service.submit_cart(db, participant, nequi.id, receipt_file_url=KEY, tenant_schema=SCHEMA)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(_count(db, CustomerOrder), 0)
        self.assertEqual(_count(db, OrderPaymentAttempt), 0)
        self.assertEqual(_count(db, Cart), 1)

    def test_efectivo_con_comprobante_sigue_siendo_422_con_su_mensaje(self):
        db, participant, _, efectivo = _seed_carrito()
        with mock.patch(EXISTS) as exists:
            with self.assertRaises(HTTPException) as ctx:
                service.submit_cart(db, participant, efectivo.id, receipt_file_url=KEY, tenant_schema=SCHEMA)
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.detail, "Un pago en efectivo no lleva comprobante")
        exists.assert_not_called()

    def test_metodo_que_ya_no_exige_comprobante_sin_comprobante_crea_la_orden(self):
        """spec 132 (A-144, FR-001/FR-023): el comensal ya no sube comprobante
        desde el QR — antes de spec 132 esto fallaba con 422 ("Este método de
        pago exige cargar un comprobante")."""
        db, participant, nequi, _ = _seed_carrito()
        order = service.submit_cart(db, participant, nequi.id, tenant_schema=SCHEMA)
        self.assertIsNone(order.current_payment_attempt.receipt_file_url)

    def test_efectivo_sin_comprobante_no_consulta_r2(self):
        db, participant, _, efectivo = _seed_carrito()
        with mock.patch(EXISTS) as exists:
            service.submit_cart(db, participant, efectivo.id, tenant_schema=SCHEMA)
        exists.assert_not_called()

    def test_sin_esquema_del_negocio_no_se_valida_en_silencio(self):
        db, participant, nequi, _ = _seed_carrito()
        with self.assertRaises(ValueError):
            service.submit_cart(db, participant, nequi.id, receipt_file_url=KEY)


class TestAttachReceipt(unittest.TestCase):
    def _attach(self, db, participant, attempt, value, *, exists=True):
        with mock.patch(EXISTS, return_value=exists):
            return service.attach_receipt(db, participant.id, attempt.id, value, tenant_schema=SCHEMA)

    def test_se_guarda_solo_la_key(self):
        for label, value in (("key", KEY), ("url dominio anterior", URL_LEGACY), ("url assets", URL_ASSETS)):
            with self.subTest(valor=label):
                db, participant, attempt = _seed_intento()
                updated = self._attach(db, participant, attempt, value)
                self.assertEqual(updated.receipt_file_url, KEY)
                self.assertEqual(updated.status, "pendiente")

    def test_comprobantes_invalidos_son_422_y_el_intento_no_cambia(self):
        casos = [
            ("inventada", KEY, dict(exists=False), MSG_NOT_FOUND),
            ("de otro negocio", "globex/comprobantes/x.jpg", {}, MSG_INVALID),
            ("de otra carpeta", "acme/logo/x.jpg", {}, MSG_INVALID),
            ("otro origen", URL_OTHER, {}, MSG_FOREIGN),
        ]
        for label, value, kwargs, message in casos:
            with self.subTest(caso=label):
                db, participant, attempt = _seed_intento()
                with self.assertRaises(HTTPException) as ctx:
                    self._attach(db, participant, attempt, value, **kwargs)
                self.assertEqual(ctx.exception.status_code, 422)
                self.assertEqual(ctx.exception.detail, message)
                db.expire_all()
                self.assertIsNone(db.get(OrderPaymentAttempt, attempt.id).receipt_file_url)

    def test_almacenamiento_caido_es_503(self):
        db, participant, attempt = _seed_intento()
        with mock.patch(EXISTS, side_effect=StorageUnavailable("x")):
            with self.assertRaises(HTTPException) as ctx:
                service.attach_receipt(db, participant.id, attempt.id, KEY, tenant_schema=SCHEMA)
        self.assertEqual(ctx.exception.status_code, 503)
        db.expire_all()
        self.assertIsNone(db.get(OrderPaymentAttempt, attempt.id).receipt_file_url)

    def test_el_409_de_comprobante_ya_adjunto_se_evalua_antes_que_la_validacion(self):
        db, participant, attempt = _seed_intento()
        self._attach(db, participant, attempt, KEY)
        with mock.patch(EXISTS) as exists:
            with self.assertRaises(HTTPException) as ctx:
                # Aunque el valor sería inválido (otro origen), el 409 va primero (US5-5).
                service.attach_receipt(db, participant.id, attempt.id, URL_OTHER, tenant_schema=SCHEMA)
        self.assertEqual(ctx.exception.status_code, 409)
        exists.assert_not_called()

    def test_efectivo_sigue_siendo_409(self):
        db = fx.new_session()
        table = fx.make_dining_table(db)
        ts = fx.make_table_session(db, table=table)
        participant = fx.make_participant(db, table_session=ts)
        order = fx.make_customer_order(db, participant)
        efectivo = fx.make_payment_method(db, name="Efectivo", is_cash=True)
        db.commit()
        attempt = service.create_payment_attempt(db, participant.id, order.id, efectivo.id)
        with self.assertRaises(HTTPException) as ctx:
            self._attach(db, participant, attempt, KEY)
        self.assertEqual(ctx.exception.status_code, 409)


def _summary_kwargs():
    return dict(id=uuid4(), status="pendiente", payment_method_name="Nequi", is_cash=False)


def _response_kwargs(receipt):
    return dict(
        id=uuid4(), order_id=uuid4(), payment_method_id=uuid4(), payment_method_name="Nequi",
        is_cash=False, status="pendiente", receipt_file_url=receipt, created_at=datetime(2026, 9, 29, 12, 0),
    )


class TestRespuestasEntreganUrlLista(unittest.TestCase):
    """Las tres superficies que exponen `receipt_file_url` (FR-008, SC-006)."""

    ASSETS_URL = f"https://assets.example.invalid/{KEY}"

    def _dumps(self, receipt):
        return {
            "PaymentAttemptResponse": PaymentAttemptResponse(**_response_kwargs(receipt)).model_dump()["receipt_file_url"],
            "CurrentPaymentAttemptSummary": CurrentPaymentAttemptSummary(
                receipt_file_url=receipt, **_summary_kwargs()
            ).model_dump()["receipt_file_url"],
            "DinerPaymentAttempt": DinerPaymentAttempt(
                id=uuid4(), order_id=uuid4(), payment_method_id=uuid4(), status="pendiente",
                receipt_file_url=receipt, created_at=datetime(2026, 9, 29, 12, 0),
            ).model_dump()["receipt_file_url"],
        }

    def test_key_se_ensambla_contra_el_dominio_de_assets(self):
        for name, value in self._dumps(KEY).items():
            self.assertEqual(value, self.ASSETS_URL, name)

    def test_fila_historica_con_url_del_dominio_anterior_se_reescribe_al_dominio_de_assets(self):
        for name, value in self._dumps(URL_LEGACY).items():
            self.assertEqual(value, self.ASSETS_URL, name)

    def test_url_de_otro_origen_sale_intacta(self):
        for name, value in self._dumps(URL_OTHER).items():
            self.assertEqual(value, URL_OTHER, name)

    def test_sin_comprobante_es_null(self):
        for name, value in self._dumps(None).items():
            self.assertIsNone(value, name)

    def test_el_orm_conserva_la_key_tras_guardar(self):
        db, participant, attempt = _seed_intento()
        with mock.patch(EXISTS, return_value=True):
            updated = service.attach_receipt(db, participant.id, attempt.id, URL_LEGACY, tenant_schema=SCHEMA)
        self.assertEqual(updated.receipt_file_url, KEY)
        self.assertEqual(DinerPaymentAttempt.model_validate(updated).model_dump()["receipt_file_url"], self.ASSETS_URL)


if __name__ == "__main__":
    unittest.main()
