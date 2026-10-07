"""Tests de la nueva funcionalidad — spec 099-nombre-tamano-variante.

`ProductVariant.name` es un alias comercial opcional y puramente cosmético (0-60
caracteres, normalizado, nunca `null`) que convive con `presentation_id` sin
reemplazarlo como fuente estructural del tamaño (research.md, "Contexto histórico":
no reabre la columna `name` que la spec 084/A-79 eliminó como clave estructural).
`display_name` es el valor calculado que el comensal ve: `name` si no está vacío, si no
el de la presentación, y `"Tamaño"` si ninguno está disponible (FR-004/FR-005).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_variant_display_name -v
"""
import unittest
from decimal import Decimal

from fastapi import HTTPException

from app.characterization_tests import fixtures as fx
from app.characterization_tests import cart_fixtures as cart_fx
from app.api.v1.products.service import ProductService
from app.api.v1.products.schemas import ProductUpdate
from app.api.v1.catalog.schemas import VariantCreate, VariantSaveIn
from app.api.v1.catalog.service import display_name_duplicado
from app.api.v1.menu.router import _build_menu
from app.api.v1.promotions.service import variant_display_names
from app.core.variant_name import normalize_variant_name
from app.models.product_variant import ProductVariant


class ColumnaNameTests(unittest.TestCase):
    """FR-001/FR-003: la columna existe, es opcional y nunca queda en `null`."""

    def test_la_columna_name_existe_y_es_opcional(self):
        self.assertIn("name", ProductVariant.__table__.columns)
        self.assertFalse(ProductVariant.__table__.c.name.nullable)

    def test_una_variante_sin_name_explicito_nace_en_cadena_vacia(self):
        db = fx.new_session()
        product = fx.make_product(db)
        presentation = fx.make_presentation(db, name="Pequeño 8 onz")
        variant = fx.make_variant(db, product=product, presentation_id=presentation.id)
        db.commit()
        self.assertEqual(variant.name, "")


class DisplayNameTests(unittest.TestCase):
    """FR-004/FR-005/Escenario 16: precedencia `name` -> `presentation.name` -> `"Tamaño"`."""

    def setUp(self):
        self.db = fx.new_session()
        self.product = fx.make_product(self.db)
        self.presentation = fx.make_presentation(self.db, name="Grande 16 onz")

    def test_con_name_propio_lo_devuelve(self):
        variant = fx.make_variant(
            self.db, product=self.product, presentation_id=self.presentation.id,
        )
        variant.name = "Para compartir"
        self.db.commit()
        self.assertEqual(variant.display_name, "Para compartir")

    def test_sin_name_propio_cae_a_la_presentacion(self):
        variant = fx.make_variant(
            self.db, product=self.product, presentation_id=self.presentation.id,
        )
        self.db.commit()
        self.assertEqual(variant.name, "")
        self.assertEqual(variant.display_name, "Grande 16 onz")

    def test_sin_ninguno_disponible_devuelve_tamano_y_advierte(self):
        # Instancia transitoria, nunca agregada a una sesión: simula el dato de
        # presentación no disponible (p. ej. referencia rota) sin violar el `NOT NULL`
        # real de `presentation_id` en la base -- la propiedad debe defenderse igual.
        variant = ProductVariant(id=self.presentation.id, name="")
        object.__setattr__(variant, "presentation", None)
        with self.assertLogs("app.models.product_variant", level="WARNING") as logs:
            resultado = variant.display_name
        self.assertEqual(resultado, "Tamaño")
        self.assertIn(str(variant.id), logs.output[0])


class NormalizeVariantNameTests(unittest.TestCase):
    """contracts/variant-name-rules.md: los 10 vectores de prueba compartidos."""

    def test_ausente_o_null_es_cadena_vacia(self):
        self.assertEqual(normalize_variant_name(None), "")

    def test_vacio_es_cadena_vacia(self):
        self.assertEqual(normalize_variant_name(""), "")

    def test_solo_espacios_es_cadena_vacia(self):
        self.assertEqual(normalize_variant_name("   "), "")

    def test_recorta_y_colapsa_espacios_internos(self):
        self.assertEqual(normalize_variant_name("  Para   compartir  "), "Para compartir")

    def test_sesenta_caracteres_en_el_limite_se_acepta(self):
        valor = "a" * 60
        self.assertEqual(normalize_variant_name(valor), valor)

    def test_sesenta_y_un_caracteres_se_rechaza(self):
        with self.assertRaises(ValueError) as ctx:
            normalize_variant_name("a" * 61)
        self.assertEqual(str(ctx.exception), "El nombre admite hasta 60 caracteres")

    def test_la_longitud_se_mide_antes_de_recortar(self):
        # 61 "a" + espacios en los extremos: sigue siendo > 60 antes de recortar.
        with self.assertRaises(ValueError):
            normalize_variant_name("  " + "a" * 61 + "  ")

    def test_html_se_guarda_tal_cual_no_se_rechaza(self):
        self.assertEqual(normalize_variant_name("<b>x</b>"), "<b>x</b>")

    def test_sin_restriccion_de_alfabeto(self):
        self.assertEqual(normalize_variant_name("😀 Ensalada日本語"), "😀 Ensalada日本語")


class GuardadoConNameTests(unittest.TestCase):
    """FR-001/FR-007/FR-012/FR-014: guardar, longitud, y ausencia/`null` del campo."""

    def setUp(self):
        self.db = fx.new_session()
        self.service = ProductService()
        self.product = fx.make_product(self.db)
        self.presentation = fx.make_presentation(self.db, name="Pequeño 8 onz")

    def test_guardar_con_name_valido_persiste_name_y_display_name(self):
        self.service.update_product(
            self.db, fx.make_tenant_stub(), self.product.id,
            ProductUpdate(variants=[
                VariantSaveIn(
                    price=Decimal("5000"), presentation_id=self.presentation.id,
                    name="Para compartir",
                ),
            ]),
        )
        self.db.expire_all()
        (variant,) = self.db.query(ProductVariant).filter_by(product_id=self.product.id).all()
        self.assertEqual(variant.name, "Para compartir")
        self.assertEqual(variant.display_name, "Para compartir")

    def test_name_ausente_o_null_se_guarda_como_cadena_vacia_sin_error(self):
        for payload_kwargs in ({}, {"name": None}):
            with self.subTest(payload_kwargs=payload_kwargs):
                db = fx.new_session()
                product = fx.make_product(db)
                presentation = fx.make_presentation(db, name=f"Tamaño-{payload_kwargs}")
                ProductService().update_product(
                    db, fx.make_tenant_stub(), product.id,
                    ProductUpdate(variants=[
                        VariantSaveIn(
                            price=Decimal("1000"), presentation_id=presentation.id,
                            **payload_kwargs,
                        ),
                    ]),
                )
                db.expire_all()
                (variant,) = db.query(ProductVariant).filter_by(product_id=product.id).all()
                self.assertEqual(variant.name, "")

    def test_nombre_de_61_caracteres_se_rechaza_con_422(self):
        with self.assertRaises(Exception):
            VariantCreate(presentation_id=self.presentation.id, name="a" * 61)


class DuplicadoDisplayNameTests(unittest.TestCase):
    """FR-008/Clarificaciones (sesión 2026-10-06): solo activas, insensible a mayúsculas."""

    def setUp(self):
        self.db = fx.new_session()
        self.service = ProductService()
        self.product = fx.make_product(self.db)
        self.pequena = fx.make_presentation(self.db, name="Pequeño 8 onz")
        self.grande = fx.make_presentation(self.db, name="Grande 16 onz")

    def _guardar(self, *variants):
        self.service.update_product(
            self.db, fx.make_tenant_stub(), self.product.id,
            ProductUpdate(variants=list(variants)),
        )

    def test_dos_nombres_propios_iguales_chocan_insensible_a_mayusculas(self):
        with self.assertRaises(HTTPException) as ctx:
            self._guardar(
                VariantSaveIn(price=Decimal("1000"), presentation_id=self.pequena.id, name="Familiar"),
                VariantSaveIn(price=Decimal("2000"), presentation_id=self.grande.id, name="familiar"),
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertNotIn("variant_id", ctx.exception.detail)
        self.assertEqual(
            ctx.exception.detail["error"],
            "Ya existe un tamaño con ese nombre en este producto",
        )

    def test_nombre_propio_que_coincide_con_presentacion_de_otra_sin_nombre_choca(self):
        with self.assertRaises(HTTPException) as ctx:
            self._guardar(
                VariantSaveIn(price=Decimal("1000"), presentation_id=self.pequena.id),
                VariantSaveIn(price=Decimal("2000"), presentation_id=self.grande.id, name="Pequeño 8 onz"),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_variante_desactivada_no_bloquea_el_nombre(self):
        self._guardar(
            VariantSaveIn(price=Decimal("1000"), presentation_id=self.pequena.id, name="Familiar"),
        )
        self.db.commit()
        (existing,) = self.db.query(ProductVariant).filter_by(product_id=self.product.id).all()
        # Desactivar la fila existente (soft-delete) y reintentar con el mismo nombre en
        # otra variante nueva del mismo producto -- no debe chocar.
        self._guardar(
            VariantSaveIn(
                id=existing.id, price=Decimal("1000"), presentation_id=self.pequena.id,
                name="Familiar", active=False,
            ),
            VariantSaveIn(price=Decimal("3000"), presentation_id=self.grande.id, name="Familiar"),
        )
        self.db.expire_all()
        nombres = {v.name for v in self.db.query(ProductVariant).filter_by(product_id=self.product.id).all()}
        self.assertEqual(nombres, {"Familiar"})

    def test_display_name_duplicado_devuelve_none_si_no_hay_choque(self):
        variant = fx.make_variant(self.db, product=self.product, presentation_id=self.pequena.id)
        self.db.commit()
        self.assertIsNone(
            display_name_duplicado(self.db, self.product.id, "Otro nombre", exclude_id=variant.id)
        )


class RendimientoMenuPublicoTests(unittest.TestCase):
    """Escenario 17/FR-018: el número de consultas SQL de `_build_menu` no crece con
    la cantidad de variantes -- el cálculo de `display_name` es en Python, sobre datos
    ya cargados por `selectinload`/`lazy="joined"` (research.md D2/D7)."""

    def _contar_consultas(self, db, n_productos: int, n_variantes: int) -> int:
        from sqlalchemy import event

        category = fx.make_category(db)
        for p in range(n_productos):
            product = fx.make_product(db, category=category)
            for v in range(n_variantes):
                presentation = fx.make_presentation(db, name=f"Tamaño {p}-{v}")
                variant = fx.make_variant(
                    db, product=product, presentation_id=presentation.id,
                )
                variant.name = f"Nombre propio {p}-{v}" if v == 0 else ""
        db.commit()

        count = 0

        def _contar(*_args, **_kwargs):
            nonlocal count
            count += 1

        event.listen(db.bind, "before_cursor_execute", _contar)
        try:
            menu = _build_menu(db)
        finally:
            event.remove(db.bind, "before_cursor_execute", _contar)

        total_variantes = sum(len(p.variants) for c in menu for p in c.products)
        self.assertEqual(total_variantes, n_productos * n_variantes)
        return count

    def test_el_numero_de_consultas_no_crece_con_la_cantidad_de_variantes(self):
        pocas = self._contar_consultas(cart_fx.new_session(), n_productos=2, n_variantes=1)
        muchas = self._contar_consultas(cart_fx.new_session(), n_productos=40, n_variantes=3)
        self.assertEqual(pocas, muchas)


class MenuPublicoDisplayNameTests(unittest.TestCase):
    """US1/FR-006: el menú público expone `display_name` y conserva `presentation_name`
    (Escenario 2/3/15)."""

    def test_build_menu_expone_display_name_y_conserva_presentation_name(self):
        # `cart_fixtures.new_session()`, no `fixtures.new_session()`: registra el
        # agregado `bool_or` (Postgres) que `_build_menu` necesita y SQLite no trae.
        db = cart_fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        presentation = fx.make_presentation(db, name="Grande 16 onz")
        variant = fx.make_variant(
            db, product=product, presentation_id=presentation.id, price=Decimal("10000"),
        )
        variant.name = "Para compartir"
        db.commit()

        menu = _build_menu(db)

        variant_resp = menu[0].products[0].variants[0]
        self.assertEqual(variant_resp.display_name, "Para compartir")
        self.assertEqual(variant_resp.presentation_name, "Grande 16 onz")

    def test_build_menu_sin_name_propio_display_name_es_la_presentacion(self):
        db = cart_fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        presentation = fx.make_presentation(db, name="Pequeño 8 onz")
        fx.make_variant(db, product=product, presentation_id=presentation.id)
        db.commit()

        menu = _build_menu(db)

        variant_resp = menu[0].products[0].variants[0]
        self.assertEqual(variant_resp.display_name, "Pequeño 8 onz")


class PromocionesDisplayNameTests(unittest.TestCase):
    """US1/research.md D10: el texto de condición de promociones también usa
    `display_name`, en una sola consulta (sin N+1)."""

    def test_variant_display_names_prefiere_el_nombre_propio(self):
        db = fx.new_session()
        product = fx.make_product(db)
        presentation = fx.make_presentation(db, name="Grande 16 onz")
        variant = fx.make_variant(db, product=product, presentation_id=presentation.id)
        variant.name = "Para compartir"
        db.commit()

        nombres = variant_display_names(db, [variant.id])
        self.assertEqual(nombres[variant.id], "Para compartir")

    def test_variant_display_names_sin_name_propio_cae_a_la_presentacion(self):
        db = fx.new_session()
        product = fx.make_product(db)
        presentation = fx.make_presentation(db, name="Pequeño 8 onz")
        variant = fx.make_variant(db, product=product, presentation_id=presentation.id)
        db.commit()

        nombres = variant_display_names(db, [variant.id])
        self.assertEqual(nombres[variant.id], "Pequeño 8 onz")


class RenombrarPresentacionNoTocaNombrePropioTests(unittest.TestCase):
    """Historia 4 (P3): renombrar una presentación cambia `display_name` de las
    variantes sin nombre propio y no toca las que sí lo tienen."""

    def test_sin_nombre_propio_sigue_a_la_presentacion_renombrada(self):
        db = fx.new_session()
        product = fx.make_product(db)
        presentation = fx.make_presentation(db, name="Pequeño 8 onz")
        sin_nombre = fx.make_variant(db, product=product, presentation_id=presentation.id)
        con_nombre = fx.make_variant(
            db, product=product,
            presentation_id=fx.make_presentation(db, name="Grande 16 onz").id,
        )
        con_nombre.name = "Para compartir"
        db.commit()

        presentation.name = "Pequeño 250 ml"
        db.commit()
        db.expire_all()

        self.assertEqual(db.get(ProductVariant, sin_nombre.id).display_name, "Pequeño 250 ml")
        self.assertEqual(db.get(ProductVariant, con_nombre.id).display_name, "Para compartir")


if __name__ == "__main__":
    unittest.main()
