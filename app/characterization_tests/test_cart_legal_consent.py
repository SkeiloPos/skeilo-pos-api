"""Tests de spec 162 (gestión de documentos legales), Phase 7 Polish:

- T044: el gate de FR-016 -- `open_session` rechaza con 409 y no crea ni
  `SessionParticipant` ni `ClientConsentAcceptance` cuando el tenant no tiene
  ningún `TenantLegalDocument` publicado.
- T043: la atomicidad de FR-018 -- un fallo insertando `ClientConsentAcceptance`
  deshace también el `SessionParticipant` ya creado en la misma llamada
  (misma transacción/commit).

Ejecutar solo este módulo:

    ./env/bin/python -m unittest app.characterization_tests.test_cart_legal_consent -v
"""
import unittest
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import legal_fixtures
from app.api.v1.cart import service
from app.models.client_consent_acceptance import ClientConsentAcceptance
from app.models.session_participant import SessionParticipant


class TestFR016Gate(unittest.TestCase):
    """T044."""

    def test_sin_documento_publicado_rechaza_409_y_no_crea_nada(self):
        db = legal_fixtures.new_session()
        table = legal_fixtures.make_dining_table(db)

        with self.assertRaises(HTTPException) as cm:
            service.open_session(db, tenant_id=1, table=table, display_name="Ana", client_ip="1.2.3.4")
        self.assertEqual(cm.exception.status_code, 409)

        self.assertIsNone(db.execute(select(SessionParticipant)).scalar_one_or_none())
        self.assertIsNone(db.execute(select(ClientConsentAcceptance)).scalar_one_or_none())

    def test_con_documento_publicado_abre_sesion_y_registra_consentimiento(self):
        db = legal_fixtures.new_session()
        table = legal_fixtures.make_dining_table(db)
        document = legal_fixtures.make_tenant_legal_document(db)
        db.commit()

        response = service.open_session(
            db, tenant_id=1, table=table, display_name="Ana", client_ip="1.2.3.4",
        )

        participant = db.get(SessionParticipant, response.participant_id)
        self.assertIsNotNone(participant)
        consent = db.execute(select(ClientConsentAcceptance)).scalar_one()
        self.assertEqual(consent.session_participant_id, participant.id)
        self.assertEqual(consent.tenant_legal_document_id, document.id)
        self.assertNotEqual(consent.ip_hash, "1.2.3.4")  # nunca la IP en claro


class TestFR018Atomicity(unittest.TestCase):
    """T043."""

    def test_fallo_insertando_consentimiento_deshace_el_participante(self):
        db = legal_fixtures.new_session()
        table = legal_fixtures.make_dining_table(db)
        legal_fixtures.make_tenant_legal_document(db)
        db.commit()

        with mock.patch(
            "app.api.v1.cart.service.ip_hash", side_effect=RuntimeError("fallo simulado"),
        ):
            with self.assertRaises(RuntimeError):
                service.open_session(
                    db, tenant_id=1, table=table, display_name="Ana", client_ip="1.2.3.4",
                )

        # Ni el `SessionParticipant` ni ninguna fila de consentimiento
        # persisten: el `db.rollback()` del `except` deshizo el `flush()`
        # anterior del participante, que todavía no se había comiteado.
        self.assertIsNone(db.execute(select(SessionParticipant)).scalar_one_or_none())
        self.assertIsNone(db.execute(select(ClientConsentAcceptance)).scalar_one_or_none())

    def test_fallo_antes_de_crear_el_participante_tampoco_deja_consentimiento(self):
        """Dirección inversa: si falla algo ANTES de crear el `SessionParticipant`
        (aquí, la propia consulta del documento vigente), el código de
        consentimiento nunca llega a ejecutarse -- por construcción, un
        `SessionParticipant` nunca existe sin su `ClientConsentAcceptance`
        y viceversa."""
        db = legal_fixtures.new_session()
        table = legal_fixtures.make_dining_table(db)
        legal_fixtures.make_tenant_legal_document(db)
        db.commit()

        with mock.patch(
            "app.api.v1.cart.service._get_or_create_table_session",
            side_effect=RuntimeError("fallo simulado"),
        ):
            with self.assertRaises(RuntimeError):
                service.open_session(
                    db, tenant_id=1, table=table, display_name="Ana", client_ip="1.2.3.4",
                )

        self.assertIsNone(db.execute(select(SessionParticipant)).scalar_one_or_none())
        self.assertIsNone(db.execute(select(ClientConsentAcceptance)).scalar_one_or_none())


if __name__ == "__main__":
    unittest.main()
