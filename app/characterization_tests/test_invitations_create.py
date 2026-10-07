"""POST /invitations (spec 037, US1).

Tests de la nueva funcionalidad — no son characterization tests: no existía
ningún endpoint de invitaciones antes de esta spec. Ejercita
`create_invitation` directamente (sin `TestClient`, research.md sección
Testing) usando `auth_fixtures` (SQLite en memoria).

    python -m unittest app.characterization_tests.test_invitations_create -v
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from app.characterization_tests import auth_fixtures as af
from app.api.v1.invitations.router import create_invitation
from app.api.v1.invitations.schemas import InvitationCreate, InvitationResponse
from app.core.dependencies import require_tenant_admin
from app.core.models import User, UserInvitation
from app.models.plan import Plan


def _make_admin(db, tenant):
    role = af.make_role(db, name="ADMIN")
    return af.make_user(db, tenant=tenant, role=role, password="claveActual1")


def _count(db, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return db.execute(stmt).scalar_one()


class CreateInvitationTests(unittest.TestCase):
    def setUp(self):
        self.db = af.new_session()
        self.tenant = af.make_tenant(self.db)
        self.admin = _make_admin(self.db, self.tenant)
        self.cashier_role = af.make_role(self.db, name="CASHIER")
        self.db.commit()

    def _create(self, email="cajero1@acme.com", role="CASHIER", tenant=None, admin=None, name="Ana Pérez"):
        with patch("app.api.v1.invitations.router.send_email") as mock_send:
            resp = create_invitation(
                InvitationCreate(email=email, role=role, name=name),
                tenant=tenant or self.tenant,
                admin=admin or self.admin,
                db=self.db,
            )
        return resp, mock_send

    # ---------------------------------------------------------- Acceptance Scenarios 2-3

    def test_creacion_exitosa_sin_user_nuevo_ni_contrasena_en_la_respuesta(self):
        resp, mock_send = self._create()

        self.assertEqual(resp.email, "cajero1@acme.com")
        self.assertEqual(resp.role_name, "CASHIER")

        invitation = self.db.execute(select(UserInvitation)).scalar_one()
        self.assertEqual(invitation.status, "pending")
        self.assertEqual(invitation.email, "cajero1@acme.com")

        # Solo self.admin, ningún `User` nuevo (Acceptance Scenario 2).
        self.assertEqual(_count(self.db, User), 1)

        mock_send.assert_called_once()
        serialized = str(InvitationResponse.model_validate(resp).model_dump())
        self.assertNotIn("password", serialized.lower())
        self.assertNotIn(invitation.password_hash, serialized)

    def test_correo_se_normaliza_antes_de_comparar_y_guardar(self):
        resp, _ = self._create(email="  Cajero1@ACME.com  ")
        self.assertEqual(resp.email, "cajero1@acme.com")

    # ---------------------------------------------------------- Edge case FR-015 (carrera)

    def test_invitacion_pendiente_duplicada_es_409_solo_una_fila_sobrevive(self):
        self._create(email="dup@acme.com")

        with patch("app.api.v1.invitations.router.send_email") as mock_send:
            with self.assertRaises(HTTPException) as ctx:
                create_invitation(
                    InvitationCreate(email="dup@acme.com", role="CASHIER", name="Ana Pérez"),
                    tenant=self.tenant, admin=self.admin, db=self.db,
                )
        mock_send.assert_not_called()  # SC-006: nunca se envía correo para un duplicado

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("invitación pendiente", ctx.exception.detail)
        self.assertEqual(_count(self.db, UserInvitation, UserInvitation.email == "dup@acme.com"), 1)

    # ---------------------------------------------------------- FR-014 (unicidad por tenant, no global)

    def test_mismo_correo_en_dos_tenants_distintos_no_colisiona(self):
        # Role es una tabla global (no por tenant) — se reutiliza el mismo
        # `self.cashier_role` en vez de crear un segundo "CASHIER" duplicado.
        other_tenant = af.make_tenant(self.db)
        other_admin = _make_admin(self.db, other_tenant)
        self.db.commit()

        resp1, _ = self._create(email="compartido@acme.com", tenant=self.tenant, admin=self.admin)
        resp2, _ = self._create(email="compartido@acme.com", tenant=other_tenant, admin=other_admin)

        self.assertNotEqual(resp1.id, resp2.id)

    # ---------------------------------------------------------- Acceptance Scenario 5 + Clarification 1

    def test_correo_de_user_activo_es_409(self):
        af.make_user(self.db, tenant=self.tenant, role=self.cashier_role, email="ya-existe@acme.com")
        self.db.commit()

        with patch("app.api.v1.invitations.router.send_email") as mock_send:
            with self.assertRaises(HTTPException) as ctx:
                create_invitation(
                    InvitationCreate(email="ya-existe@acme.com", role="CASHIER", name="Ana Pérez"),
                    tenant=self.tenant, admin=self.admin, db=self.db,
                )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("usuario", ctx.exception.detail)
        mock_send.assert_not_called()  # SC-006

    def test_correo_de_user_inactivo_tambien_es_409_mismo_mensaje(self):
        af.make_user(
            self.db, tenant=self.tenant, role=self.cashier_role,
            email="baja@acme.com", active=False,
        )
        self.db.commit()

        with patch("app.api.v1.invitations.router.send_email") as mock_send:
            with self.assertRaises(HTTPException) as ctx:
                create_invitation(
                    InvitationCreate(email="baja@acme.com", role="CASHIER", name="Ana Pérez"),
                    tenant=self.tenant, admin=self.admin, db=self.db,
                )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("usuario", ctx.exception.detail)
        mock_send.assert_not_called()  # SC-006

    # ---------------------------------------------------------- Acceptance Scenario 4

    def test_cashier_no_puede_invitar(self):
        cashier = af.make_user(self.db, tenant=self.tenant, role=self.cashier_role)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            require_tenant_admin(user=cashier)
        self.assertEqual(ctx.exception.status_code, 403)

    # ---------------------------------------------------------- FR-018 (plan vencido)

    def test_tenant_con_plan_vencido_es_403_sin_enviar_correo(self):
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        expired_tenant = af.make_tenant(self.db, plan_vence_en=past)
        admin = _make_admin(self.db, expired_tenant)
        self.db.commit()

        with patch("app.api.v1.invitations.router.send_email") as mock_send:
            with self.assertRaises(HTTPException) as ctx:
                create_invitation(
                    InvitationCreate(email="nuevo@acme.com", role="CASHIER", name="Ana Pérez"),
                    tenant=expired_tenant,
                    admin=admin,
                    db=self.db,
                )
        self.assertEqual(ctx.exception.status_code, 403)
        mock_send.assert_not_called()

    # ---------------------------------------------------------- research.md Decisión 5

    def test_limite_de_plan_cuenta_invitaciones_pendientes_sin_ningun_user_real(self):
        # Cupo de 2: 1 ya lo ocupa el propio ADMIN (un `User` real); el
        # cupo restante lo agota la primera invitación *pendiente* (sin
        # ningún otro `User` real) — distingue el conteo extendido de T007.
        plan = Plan(name="plan-limitado", usuarios_limit=2)
        self.db.add(plan)
        self.db.flush()
        limited_tenant = af.make_tenant(self.db, plan_id=plan.id)
        admin = _make_admin(self.db, limited_tenant)
        self.db.commit()

        self._create(email="primero@acme.com", tenant=limited_tenant, admin=admin)

        with self.assertRaises(HTTPException) as ctx:
            self._create(email="segundo@acme.com", tenant=limited_tenant, admin=admin)
        self.assertEqual(ctx.exception.status_code, 403)

    # ---------------------------------------------------------- FR-012

    def test_fallo_de_envio_no_deja_invitacion_persistida(self):
        with patch(
            "app.api.v1.invitations.router.send_email", side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                create_invitation(
                    InvitationCreate(email="fallo@acme.com", role="CASHIER", name="Ana Pérez"),
                    tenant=self.tenant,
                    admin=self.admin,
                    db=self.db,
                )
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(_count(self.db, UserInvitation, UserInvitation.email == "fallo@acme.com"), 0)

    # ---------------------------------------------------------- spec 096 (FR-041, FR-057, A-121, D16)

    def test_valor_obligatorio_de_plantilla_faltante_produce_502_y_no_crea_la_invitacion(self):
        """D16/A-121: FR-041 impide generar un correo con huecos, y FR-057
        trata ese fallo como un caso más de "el correo no sale" — mismo 502,
        mismo rollback, sin vía de error nueva. Antes de esta spec el valor
        vacío se interpolaba y la invitación quedaba creada; con el cambio
        de comportamiento ya registrado en A-121, ahora falla la operación
        completa."""
        from app.core.mail_templates import ErrorDePlantillaDeCorreo

        with patch(
            "app.api.v1.invitations.router.construir_correo_invitacion",
            side_effect=ErrorDePlantillaDeCorreo("Faltan campos obligatorios: nombre_negocio"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                create_invitation(
                    InvitationCreate(email="hueco@acme.com", role="CASHIER", name="Ana Pérez"),
                    tenant=self.tenant,
                    admin=self.admin,
                    db=self.db,
                )
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(_count(self.db, UserInvitation, UserInvitation.email == "hueco@acme.com"), 0)

    # ---------------------------------------------------------- spec 091 (A-100): nombre completo

    def test_el_nombre_se_persiste_recortado_en_nfc_y_va_al_correo_y_a_la_respuesta(self):
        # spec 096 (FR-059, A-113): el router pasa de armar el cuerpo con
        # `invitation_email_body` a pedirle un `EmailContent` a
        # `construir_correo_invitacion`. Se reapunta el parcheo al
        # constructor nuevo, conservando intacto lo que este test afirma:
        # que el nombre normalizado llega a la plantilla.
        from app.core.mail_templates import EmailContent

        with patch("app.api.v1.invitations.router.send_email") as mock_send, patch(
            "app.api.v1.invitations.router.construir_correo_invitacion",
            return_value=EmailContent(subject="asunto", html_body="<p>cuerpo</p>", text_body="cuerpo"),
        ) as mock_body:
            resp = create_invitation(
                InvitationCreate(email="maria@acme.com", role="CASHIER", name="  Mari\u0301a Pérez  "),
                tenant=self.tenant, admin=self.admin, db=self.db,
            )
        mock_send.assert_called_once()
        self.assertEqual(mock_body.call_args.kwargs["nombre_persona"], "María Pérez")
        self.assertEqual(resp.name, "María Pérez")
        self.assertEqual(InvitationResponse.model_validate(resp).name, "María Pérez")
        invitation = self.db.execute(select(UserInvitation)).scalar_one()
        self.assertEqual(invitation.name, "María Pérez")

    def test_dos_invitaciones_con_el_mismo_nombre_y_correos_distintos_se_aceptan(self):
        self._create(email="uno@acme.com", name="Ana Pérez")
        self._create(email="dos@acme.com", name="Ana Pérez")
        self.assertEqual(_count(self.db, UserInvitation), 2)

    def test_nombre_con_tilde_enie_apostrofe_y_guion_se_guarda_identico(self):
        self._create(email="jose@acme.com", name="José Ñañez O'Brien-Díaz")
        invitation = self.db.execute(select(UserInvitation)).scalar_one()
        self.assertEqual(invitation.name, "José Ñañez O'Brien-Díaz")

    # ---------------------------------------------------------- spec 091 (US4): validación del nombre

    def _assert_nombre_rechazado(self, payload: dict, message: str):
        """Cuerpo inválido: 422 con `detail[0].loc == ["body","name"]` (vía el
        schema), nada se crea, no se envía correo y no se consume cupo del plan."""
        with patch("app.api.v1.invitations.router.send_email") as mock_send:
            with self.assertRaises(ValidationError) as ctx:
                InvitationCreate(**payload)
        errors = ctx.exception.errors()
        self.assertEqual(errors[0]["loc"], ("name",))
        self.assertEqual(errors[0]["msg"], f"Value error, {message}")
        mock_send.assert_not_called()
        self.assertEqual(_count(self.db, UserInvitation), 0)

    def test_tabla_de_422_del_nombre(self):
        base = {"email": "nuevo@acme.com", "role": "CASHIER"}
        required = "El nombre es obligatorio"
        length = "El nombre debe tener entre 2 y 100 caracteres"
        fmt = "El nombre solo puede contener letras, espacios, apóstrofes y guiones, y al menos dos letras"
        cases = [
            ({}, required),                                  # clave ausente
            ({"name": ""}, required),
            ({"name": "   "}, required),
            ({"name": "A"}, length),
            ({"name": "a" * 101}, length),
            ({"name": "<script>alert(1)</script>"}, fmt),
            ({"name": "Ana3"}, fmt),
            ({"name": "Ana@"}, fmt),
            ({"name": "Иван"}, fmt),
            ({"name": "Ana 😀"}, fmt),
        ]
        for extra, message in cases:
            with self.subTest(extra=extra):
                self._assert_nombre_rechazado({**base, **extra}, message)

    def test_nombre_con_espacios_en_los_extremos_se_guarda_recortado(self):
        self._create(email="ana@acme.com", name=" Ana ")
        self.assertEqual(self.db.execute(select(UserInvitation)).scalar_one().name, "Ana")


if __name__ == "__main__":
    unittest.main()
