"""Tests de spec 162 (gestión de documentos legales), Phase 7 Polish:

- T041: máquina de estados borrador/publicado/superado, para documentos de
  plataforma y para la plantilla legal maestra (`legal_platform_service.py`).
- T042: validación de la lista cerrada de variables de la plantilla maestra.

Ejecutar solo este módulo:

    ./env/bin/python -m unittest app.characterization_tests.test_legal_platform_service -v
"""
from datetime import datetime, timezone
import unittest
from uuid import uuid4

from fastapi import HTTPException

from pydantic import ValidationError

from app.characterization_tests import legal_fixtures
from app.api.v1.super_admin import legal_platform_service as service
from app.api.v1.super_admin import legal_schemas


class TestPlatformDocumentStateMachine(unittest.TestCase):
    """T041 — `platform_legal_documents`."""

    def test_crear_borrador_asigna_version_1(self):
        db = legal_fixtures.new_session()
        doc = service.create_platform_document_draft(
            db, tipo="terminos", contenido_markdown="v1"
        )
        self.assertEqual(doc.version, 1)
        self.assertEqual(doc.estado, "borrador")

    def test_crear_segundo_borrador_del_mismo_tipo_incrementa_version(self):
        db = legal_fixtures.new_session()
        service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="v1")
        doc2 = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="v2")
        self.assertEqual(doc2.version, 2)

    def test_editar_borrador_ok(self):
        db = legal_fixtures.new_session()
        doc = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="v1")
        edited = service.edit_platform_document_draft(db, doc.id, contenido_markdown="v1-editado")
        self.assertEqual(edited.contenido_markdown, "v1-editado")

    def test_editar_documento_no_borrador_rechaza_409(self):
        db = legal_fixtures.new_session()
        doc = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="v1")
        service.publish_platform_document(
            db, doc.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )
        with self.assertRaises(HTTPException) as cm:
            service.edit_platform_document_draft(db, doc.id, contenido_markdown="otro")
        self.assertEqual(cm.exception.status_code, 409)

    def test_publicar_pasa_version_anterior_a_superado_y_conserva_su_contenido(self):
        db = legal_fixtures.new_session()
        v1 = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="contenido v1")
        service.publish_platform_document(
            db, v1.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )
        v2 = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="contenido v2")
        service.publish_platform_document(
            db, v2.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )

        history = service.get_platform_document_history(db, "terminos")
        by_version = {h.version: h for h in history}
        self.assertEqual(by_version[1].estado, "superado")
        self.assertEqual(by_version[1].contenido_markdown, "contenido v1")
        self.assertEqual(by_version[2].estado, "publicado")

    def test_a_lo_sumo_un_publicado_por_tipo_bajo_carrera(self):
        """FR-004: dos publicaciones concurrentes del mismo `tipo` -- la
        segunda en comitear choca contra el índice único parcial y recibe
        409 en vez de dejar dos filas `publicado` visibles a la vez."""
        db = legal_fixtures.new_session()
        a = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="a")
        b = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="b")

        service.publish_platform_document(
            db, a.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )
        # `b` ya no está en `borrador` tras forzar el estado a mano para simular
        # la carrera -- se fuerza `estado='publicado'` directamente para
        # saltarse el propio guard de "ya no es borrador" y ejercitar
        # específicamente el índice único parcial subyacente.
        b.estado = "publicado"
        with self.assertRaises(Exception):
            db.commit()
        db.rollback()

        # El estado de `a` (la que sí ganó limpiamente) sigue intacto.
        self.assertEqual(db.get(type(a), a.id).estado, "publicado")

    def test_publicar_sin_estar_en_borrador_rechaza_409(self):
        db = legal_fixtures.new_session()
        doc = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="v1")
        service.publish_platform_document(
            db, doc.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )
        with self.assertRaises(HTTPException) as cm:
            service.publish_platform_document(
                db, doc.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
            )
        self.assertEqual(cm.exception.status_code, 409)

    def test_eliminar_borrador_ok(self):
        db = legal_fixtures.new_session()
        doc = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="v1")
        service.delete_platform_document_draft(db, doc.id)
        with self.assertRaises(HTTPException) as cm:
            service._get_platform_document_or_404(db, doc.id)
        self.assertEqual(cm.exception.status_code, 404)

    def test_eliminar_documento_publicado_rechaza_409_y_no_elimina(self):
        db = legal_fixtures.new_session()
        doc = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="v1")
        service.publish_platform_document(
            db, doc.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )
        with self.assertRaises(HTTPException) as cm:
            service.delete_platform_document_draft(db, doc.id)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(service._get_platform_document_or_404(db, doc.id).estado, "publicado")

    def test_publicar_borrador_vacio_rechaza_422_y_no_publica(self):
        """`create` permite contenido vacío a propósito (primer borrador sin
        versión previa de la que partir), pero publicarlo así dejaría al
        tenant con un snapshot vacío -- debe bloquearse antes de que pase a
        `publicado`."""
        db = legal_fixtures.new_session()
        doc = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="")
        with self.assertRaises(HTTPException) as cm:
            service.publish_platform_document(
                db, doc.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
            )
        self.assertEqual(cm.exception.status_code, 422)
        self.assertEqual(service._get_platform_document_or_404(db, doc.id).estado, "borrador")

    def test_publicar_borrador_de_solo_espacios_rechaza_422(self):
        db = legal_fixtures.new_session()
        doc = service.create_platform_document_draft(db, tipo="terminos", contenido_markdown="   \n  ")
        with self.assertRaises(HTTPException) as cm:
            service.publish_platform_document(
                db, doc.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
            )
        self.assertEqual(cm.exception.status_code, 422)


class TestMasterTemplateStateMachine(unittest.TestCase):
    """T041 — `master_legal_templates` (misma máquina de estados)."""

    def test_publicar_pasa_version_anterior_a_superado_sin_tocar_su_contenido(self):
        db = legal_fixtures.new_session()
        v1 = service.create_master_template_draft(db, contenido_markdown="{{nombre_negocio}} v1")
        service.publish_master_template(
            db, v1.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )
        v2 = service.create_master_template_draft(db, contenido_markdown="{{nombre_negocio}} v2")
        service.publish_master_template(
            db, v2.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )

        history = service.get_master_template_history(db)
        by_version = {h.version: h for h in history}
        self.assertEqual(by_version[1].estado, "superado")
        self.assertEqual(by_version[1].contenido_markdown, "{{nombre_negocio}} v1")
        self.assertEqual(by_version[2].estado, "publicado")

    def test_eliminar_borrador_ok(self):
        db = legal_fixtures.new_session()
        template = service.create_master_template_draft(db, contenido_markdown="v1")
        service.delete_master_template_draft(db, template.id)
        with self.assertRaises(HTTPException) as cm:
            service._get_master_template_or_404(db, template.id)
        self.assertEqual(cm.exception.status_code, 404)

    def test_eliminar_plantilla_publicada_rechaza_409_y_no_elimina(self):
        db = legal_fixtures.new_session()
        template = service.create_master_template_draft(db, contenido_markdown="v1")
        service.publish_master_template(
            db, template.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )
        with self.assertRaises(HTTPException) as cm:
            service.delete_master_template_draft(db, template.id)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(service._get_master_template_or_404(db, template.id).estado, "publicado")

    def test_editar_plantilla_no_borrador_rechaza_409(self):
        db = legal_fixtures.new_session()
        template = service.create_master_template_draft(db, contenido_markdown="v1")
        service.publish_master_template(
            db, template.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
        )
        with self.assertRaises(HTTPException) as cm:
            service.edit_master_template_draft(db, template.id, contenido_markdown="otro")
        self.assertEqual(cm.exception.status_code, 409)

    def test_publicar_plantilla_vacia_rechaza_422_y_no_publica(self):
        db = legal_fixtures.new_session()
        template = service.create_master_template_draft(db, contenido_markdown="")
        with self.assertRaises(HTTPException) as cm:
            service.publish_master_template(
                db, template.id, fecha_vigencia=datetime.now(timezone.utc), published_by_user_id=uuid4(),
            )
        self.assertEqual(cm.exception.status_code, 422)
        self.assertEqual(service._get_master_template_or_404(db, template.id).estado, "borrador")


class TestMasterTemplateVariableValidation(unittest.TestCase):
    """T042 — `legal_template.validate_variables` vía el servicio de guardado."""

    def test_crear_con_solo_variables_permitidas_ok(self):
        db = legal_fixtures.new_session()
        template = service.create_master_template_draft(
            db, contenido_markdown="{{nombre_negocio}} - {{correo_contacto}}",
        )
        self.assertEqual(template.estado, "borrador")

    def test_crear_con_variable_no_permitida_no_se_rechaza_en_create(self):
        # `create` no valida a propósito: el frontend pre-llena un borrador
        # nuevo con el contenido de la versión anterior, que puede incluir
        # una variable ya retirada de la lista permitida (p. ej. una versión
        # publicada antes de que se le quitara `dias_retencion_datos`) — si
        # create también validara, ese borrador sería imposible de crear, ni
        # siquiera para poder abrirlo y corregirlo.
        db = legal_fixtures.new_session()
        template = service.create_master_template_draft(
            db, contenido_markdown="{{nombre_negocio}} {{codigo_promocional}} {{otra_no_permitida}}",
        )
        self.assertEqual(template.estado, "borrador")

    def test_publicar_con_variable_no_permitida_rechaza_422_con_nombres_exactos(self):
        db = legal_fixtures.new_session()
        template = service.create_master_template_draft(
            db, contenido_markdown="{{nombre_negocio}} {{codigo_promocional}} {{otra_no_permitida}}",
        )
        with self.assertRaises(HTTPException) as cm:
            service.publish_master_template(
                db,
                template.id,
                fecha_vigencia=datetime(2026, 1, 1, tzinfo=timezone.utc),
                published_by_user_id=uuid4(),
            )
        self.assertEqual(cm.exception.status_code, 422)
        self.assertEqual(
            cm.exception.detail["variables_no_permitidas"],
            ["codigo_promocional", "otra_no_permitida"],
        )

    def test_editar_borrador_con_variable_no_permitida_rechaza_422(self):
        db = legal_fixtures.new_session()
        template = service.create_master_template_draft(db, contenido_markdown="{{nombre_negocio}}")
        with self.assertRaises(HTTPException) as cm:
            service.edit_master_template_draft(
                db, template.id, contenido_markdown="{{nombre_negocio}} {{inventado}}",
            )
        self.assertEqual(cm.exception.status_code, 422)
        self.assertEqual(cm.exception.detail["variables_no_permitidas"], ["inventado"])


class TestEsquemasRechazanContenidoVacioAlEditar(unittest.TestCase):
    """El body de `POST .../{id}` (crear) puede llegar vacío a propósito
    (primer borrador sin versión previa), pero el de `PATCH` (edición
    explícita del admin) no debería aceptar guardar la nada -- ese fue el
    hueco que dejaba publicar contenido vacío en producción."""

    def test_patch_platform_document_rechaza_contenido_vacio(self):
        with self.assertRaises(ValidationError):
            legal_schemas.PlatformLegalDocumentPatch(contenido_markdown="")

    def test_patch_platform_document_rechaza_solo_espacios(self):
        with self.assertRaises(ValidationError):
            legal_schemas.PlatformLegalDocumentPatch(contenido_markdown="   ")

    def test_patch_master_template_rechaza_contenido_vacio(self):
        with self.assertRaises(ValidationError):
            legal_schemas.MasterLegalTemplatePatch(contenido_markdown="")

    def test_post_master_template_in_sigue_permitiendo_vacio(self):
        # `create` (POST) debe seguir aceptando vacío -- es el borrador
        # inicial sin versión previa de la que partir.
        schema = legal_schemas.MasterLegalTemplateIn(contenido_markdown="")
        self.assertEqual(schema.contenido_markdown, "")


if __name__ == "__main__":
    unittest.main()
