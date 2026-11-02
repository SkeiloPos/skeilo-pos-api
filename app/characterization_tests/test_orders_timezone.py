"""Spec 030 — Historia 2: `orders/schemas.py` usa el mismo mecanismo central
(`UtcDatetime`) que Ventas (A-50, registro-de-anomalias.md).

    python -m unittest app.characterization_tests.test_orders_timezone -v
"""
import unittest
import uuid
from datetime import datetime

from app.api.v1.orders import schemas as orders_schemas


class TestOrderCreatedAtUtcSerialization(unittest.TestCase):
    def test_created_at_lleva_offset_utc_explicito(self):
        naive = datetime(2026, 8, 24, 12, 53, 7)
        resp = orders_schemas.OrderResponse.model_construct(
            id=uuid.uuid4(), channel="POS", status="pendiente", version=0,
            created_at=naive,
        )
        dumped = resp.model_dump(mode="json")["created_at"]
        self.assertTrue(dumped.endswith("+00:00"), dumped)


class TestPaymentAttemptUtcSerialization(unittest.TestCase):
    def test_created_at_y_resolved_at_llevan_offset_utc_explicito(self):
        naive = datetime(2026, 8, 24, 12, 53, 7)
        resp = orders_schemas.PaymentAttemptResponse.model_construct(
            id=uuid.uuid4(), order_id=uuid.uuid4(), payment_method_id=uuid.uuid4(),
            payment_method_name="Nequi", is_cash=False, status="confirmado",
            created_at=naive, resolved_at=naive,
        )
        dumped = resp.model_dump(mode="json")
        self.assertTrue(dumped["created_at"].endswith("+00:00"), dumped["created_at"])
        self.assertTrue(dumped["resolved_at"].endswith("+00:00"), dumped["resolved_at"])

    def test_resolved_at_nulo_se_mantiene_nulo(self):
        naive = datetime(2026, 8, 24, 12, 53, 7)
        resp = orders_schemas.PaymentAttemptResponse.model_construct(
            id=uuid.uuid4(), order_id=uuid.uuid4(), payment_method_id=uuid.uuid4(),
            payment_method_name="Nequi", is_cash=False, status="pendiente",
            created_at=naive, resolved_at=None,
        )
        self.assertIsNone(resp.model_dump(mode="json")["resolved_at"])


if __name__ == "__main__":
    unittest.main()
