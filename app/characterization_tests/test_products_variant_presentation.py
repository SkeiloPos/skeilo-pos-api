"""Tests de la nueva funcionalidad — spec 084-fix-promociones-productos, US1 + enmienda
2026-09-20 (US7, A-79): la variante de un producto se identifica por una presentación del
catálogo y **no tiene nombre propio**.

Sustituye a la versión de US1 (2026-09-17), que fijaba el nombre libre ("Sin presentación") y
la cascada de renombre (FR-004). Ambos comportamientos dejaron de existir por A-79
(registro-de-anomalias.md): el nombre de la variante se lee de su `Presentation`.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_products_variant_presentation -v
"""
import unittest
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import fixtures as fx
from app.api.v1.products.service import ProductService
from app.api.v1.products.schemas import ProductCreate, ProductUpdate
from app.api.v1.catalog.schemas import VariantSaveIn
from app.api.v1.catalog.service import DEFAULT_PRESENTATION_NAME
from app.api.v1.presentations.router import update_presentation
from app.api.v1.presentations.schemas import PresentationUpdate
from app.models.presentation import Presentation
from app.models.product_variant import ProductVariant


def _variant(db, variant_id):
    db.expire_all()
    return db.get(ProductVariant, variant_id)


def _variants_of(db, product_id):
    db.expire_all()
    return db.execute(
        select(ProductVariant)
        .where(ProductVariant.product_id == product_id)
        .order_by(ProductVariant.display_order)
    ).scalars().all()


class SchemaSinNombreTests(unittest.TestCase):
    """A-79: la estructura de la variante la da su presentación, no un nombre libre.

    spec 099-nombre-tamano-variante (2026-10-06) reabre esto de forma controlada y
    documentada (ver su `research.md`, "Contexto histórico"): agrega `ProductVariant.name`
    como un alias comercial **opcional y puramente cosmético** que convive con
    `presentation_id` sin reemplazarlo. Las dos primeras pruebas de esta clase, que antes
    afirmaban la ausencia total de `name`, ahora afirman que sigue siendo opcional y que
    `presentation_id` sigue siendo la única fuente estructural del nombre.
    """

    def test_la_variante_tiene_columna_name_opcional_y_cosmetica(self):
        self.assertTrue(hasattr(ProductVariant, "name"))
        self.assertIn("name", ProductVariant.__table__.columns)
        self.assertFalse(ProductVariant.__table__.c.name.nullable)

    def test_variant_save_in_declara_name_opcional(self):
        self.assertIn("name", VariantSaveIn.model_fields)
        self.assertFalse(VariantSaveIn.model_fields["name"].is_required())
        self.assertIn("presentation_id", VariantSaveIn.model_fields)

    def test_presentation_id_es_obligatorio_en_la_tabla(self):
        self.assertFalse(ProductVariant.__table__.c.presentation_id.nullable)


class AsociarPresentacionTests(unittest.TestCase):
    """FR-001/FR-003 (enmendados): elegir o cambiar la presentación es el único modo de
    nombrar una variante."""

    def setUp(self):
        self.db = fx.new_session()
        self.service = ProductService()

    def test_asociar_una_presentacion_al_guardar_nombra_la_variante(self):
        product = fx.make_product(self.db)
        grande = fx.make_presentation(self.db, name="Grande")
        self.db.commit()

        self.service.update_product(
            self.db, fx.make_tenant_stub(), product.id,
            ProductUpdate(variants=[
                VariantSaveIn(price=Decimal("5000"), presentation_id=grande.id),
            ]),
        )
        (variant,) = _variants_of(self.db, product.id)
        self.assertEqual(variant.presentation_name, "Grande")
        self.assertEqual(variant.presentation_id, grande.id)

    def test_reasociar_a_otra_presentacion_cambia_el_nombre_visible(self):
        product = fx.make_product(self.db)
        grande = fx.make_presentation(self.db, name="Grande")
        mediana = fx.make_presentation(self.db, name="Mediana")
        v = fx.make_variant(self.db, product, presentation_id=grande.id)
        self.db.commit()

        self.service.update_product(
            self.db, fx.make_tenant_stub(), product.id,
            ProductUpdate(variants=[
                VariantSaveIn(id=v.id, price=v.price, presentation_id=mediana.id),
            ]),
        )
        actual = _variant(self.db, v.id)
        self.assertEqual(actual.presentation_name, "Mediana")
        self.assertEqual(actual.presentation_id, mediana.id)

    def test_presentacion_inexistente_o_inactiva_rechazada_con_422(self):
        product = fx.make_product(self.db)
        inactiva = fx.make_presentation(self.db, name="Retirada", active=False)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.service.update_product(
                self.db, fx.make_tenant_stub(), product.id,
                ProductUpdate(variants=[
                    VariantSaveIn(price=Decimal("0"), presentation_id=inactiva.id),
                ]),
            )
        self.assertEqual(ctx.exception.status_code, 422)

        with self.assertRaises(HTTPException) as ctx2:
            self.service.update_product(
                self.db, fx.make_tenant_stub(), product.id,
                ProductUpdate(variants=[
                    VariantSaveIn(price=Decimal("0"), presentation_id=uuid4()),
                ]),
            )
        self.assertEqual(ctx2.exception.status_code, 422)

    def test_una_variante_puede_seguir_usando_su_presentacion_desactivada(self):
        """Desactivar una presentación solo la retira del selector: no deja sin poder
        guardar (ni sin nombre) a las variantes que ya la usan."""
        product = fx.make_product(self.db)
        retirada = fx.make_presentation(self.db, name="Retirada", active=False)
        v = fx.make_variant(self.db, product, presentation_id=retirada.id, price=Decimal("1000"))
        self.db.commit()

        self.service.update_product(
            self.db, fx.make_tenant_stub(), product.id,
            ProductUpdate(variants=[
                VariantSaveIn(id=v.id, price=Decimal("1500"), presentation_id=retirada.id),
            ]),
        )
        actual = _variant(self.db, v.id)
        self.assertEqual(actual.price, Decimal("1500.00"))
        self.assertEqual(actual.presentation_name, "Retirada")


class PresentacionUnicaTests(unittest.TestCase):
    """FR-034: `presentation_id=None` = "Presentación única" (get-or-create)."""

    def setUp(self):
        self.db = fx.new_session()
        self.service = ProductService()

    def _unicas(self):
        return self.db.execute(
            select(Presentation).where(Presentation.name == DEFAULT_PRESENTATION_NAME)
        ).scalars().all()

    def test_none_crea_la_presentacion_unica_si_el_catalogo_no_la_tiene(self):
        product = fx.make_product(self.db)
        self.db.commit()
        self.assertEqual(self._unicas(), [])

        self.service.update_product(
            self.db, fx.make_tenant_stub(), product.id,
            ProductUpdate(variants=[VariantSaveIn(price=Decimal("3000"), presentation_id=None)]),
        )
        (unica,) = self._unicas()
        (variant,) = _variants_of(self.db, product.id)
        self.assertEqual(variant.presentation_id, unica.id)
        self.assertEqual(variant.presentation_name, "Presentación única")
        self.assertTrue(unica.active)

    def test_none_reutiliza_la_presentacion_unica_existente_aunque_este_desactivada(self):
        unica = fx.make_presentation(self.db, name=DEFAULT_PRESENTATION_NAME, active=False)
        product = fx.make_product(self.db)
        self.db.commit()

        self.service.update_product(
            self.db, fx.make_tenant_stub(), product.id,
            ProductUpdate(variants=[VariantSaveIn(price=Decimal("0"), presentation_id=None)]),
        )
        (variant,) = _variants_of(self.db, product.id)
        self.assertEqual(variant.presentation_id, unica.id)
        self.assertEqual(len(self._unicas()), 1)

    def test_dos_filas_none_del_mismo_producto_chocan_por_unicidad(self):
        product = fx.make_product(self.db)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.service.update_product(
                self.db, fx.make_tenant_stub(), product.id,
                ProductUpdate(variants=[
                    VariantSaveIn(price=Decimal("0"), presentation_id=None),
                    VariantSaveIn(price=Decimal("0"), presentation_id=None),
                ]),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_producto_sin_variantes_nace_con_la_presentacion_unica(self):
        category = fx.make_category(self.db)
        self.db.commit()
        product = self.service.create_product(
            self.db, fx.make_tenant_stub(),
            ProductCreate(name="Cono", category_id=category.id),
        )
        (variant,) = _variants_of(self.db, product.id)
        self.assertEqual(variant.presentation_name, "Presentación única")


class UnicidadPorProductoTests(unittest.TestCase):
    """FR-006: dos variantes del mismo producto no pueden compartir presentación."""

    def setUp(self):
        self.db = fx.new_session()
        self.service = ProductService()

    def test_dos_variantes_del_mismo_producto_no_comparten_presentacion(self):
        product = fx.make_product(self.db)
        grande = fx.make_presentation(self.db, name="Grande")
        fx.make_variant(self.db, product, presentation_id=grande.id)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.service.update_product(
                self.db, fx.make_tenant_stub(), product.id,
                ProductUpdate(variants=[
                    VariantSaveIn(price=Decimal("0"), presentation_id=grande.id),
                ]),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_la_presentacion_de_una_variante_desactivada_sigue_ocupada(self):
        """Soft-delete: la fila desactivada sigue ocupando la presentación; el 409 ofrece
        reactivarla en vez de crear otra."""
        product = fx.make_product(self.db)
        grande = fx.make_presentation(self.db, name="Grande")
        vieja = fx.make_variant(self.db, product, presentation_id=grande.id, active=False)
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            self.service.update_product(
                self.db, fx.make_tenant_stub(), product.id,
                ProductUpdate(variants=[
                    VariantSaveIn(price=Decimal("0"), presentation_id=grande.id),
                ]),
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("desactivada", ctx.exception.detail["error"])
        self.assertEqual(ctx.exception.detail["variant_id"], str(vieja.id))

    def test_guardar_sin_cambios_la_misma_variante_no_choca_consigo_misma(self):
        product = fx.make_product(self.db)
        grande = fx.make_presentation(self.db, name="Grande")
        v = fx.make_variant(self.db, product, presentation_id=grande.id, price=Decimal("1000"))
        self.db.commit()

        self.service.update_product(
            self.db, fx.make_tenant_stub(), product.id,
            ProductUpdate(variants=[
                VariantSaveIn(id=v.id, price=Decimal("1200"), presentation_id=grande.id),
            ]),
        )
        self.assertEqual(_variant(self.db, v.id).price, Decimal("1200.00"))

    def test_misma_presentacion_en_productos_distintos_esta_permitida(self):
        grande = fx.make_presentation(self.db, name="Grande")
        p1 = fx.make_product(self.db)
        p2 = fx.make_product(self.db)
        fx.make_variant(self.db, p1, presentation_id=grande.id)
        self.db.commit()

        self.service.update_product(
            self.db, fx.make_tenant_stub(), p2.id,
            ProductUpdate(variants=[
                VariantSaveIn(price=Decimal("0"), presentation_id=grande.id),
            ]),
        )
        (variant,) = _variants_of(self.db, p2.id)
        self.assertEqual(variant.presentation_id, grande.id)


class RenombrarPresentacionTests(unittest.TestCase):
    """FR-030/FR-031 (A-79): renombrar una presentación llega a todas sus variantes sin
    escribir en `product_variants` y sin poder chocar con otras variantes."""

    def setUp(self):
        self.db = fx.new_session()

    def test_renombrar_se_refleja_en_todas_las_variantes(self):
        grande = fx.make_presentation(self.db, name="Grande")
        v1 = fx.make_variant(self.db, fx.make_product(self.db), presentation_id=grande.id)
        v2 = fx.make_variant(self.db, fx.make_product(self.db), presentation_id=grande.id)
        otra = fx.make_variant(self.db, fx.make_product(self.db), name="Mediana")
        self.db.commit()

        update_presentation(grande.id, PresentationUpdate(name="Extra Grande"), self.db, None)

        self.assertEqual(_variant(self.db, v1.id).presentation_name, "Extra Grande")
        self.assertEqual(_variant(self.db, v2.id).presentation_name, "Extra Grande")
        self.assertEqual(_variant(self.db, otra.id).presentation_name, "Mediana")

    def test_renombrar_a_un_nombre_ya_usado_en_el_catalogo_sigue_siendo_409(self):
        grande = fx.make_presentation(self.db, name="Grande")
        fx.make_presentation(self.db, name="Mediana")
        self.db.commit()

        with self.assertRaises(HTTPException) as ctx:
            update_presentation(grande.id, PresentationUpdate(name="Mediana"), self.db, None)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_ya_no_hay_conflicto_por_otra_variante_del_mismo_producto(self):
        """Antes (FR-004, versión 2026-09-17) renombrar chocaba con el nombre de otra
        variante del mismo producto; ya no hay un nombre de variante contra el que chocar."""
        grande = fx.make_presentation(self.db, name="Grande")
        product = fx.make_product(self.db)
        v1 = fx.make_variant(self.db, product, presentation_id=grande.id)
        fx.make_variant(self.db, product, name="Súper")
        self.db.commit()

        update_presentation(grande.id, PresentationUpdate(name="Gigante"), self.db, None)
        self.assertEqual(_variant(self.db, v1.id).presentation_name, "Gigante")


if __name__ == "__main__":
    unittest.main()
