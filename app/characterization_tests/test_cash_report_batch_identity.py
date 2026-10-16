"""Comportamiento NUEVO (spec 119 US6): identidad del reporte con el de antes de
la reescritura en lote -- el snapshot del cierre vive en FILAS (FR-013/FR-017,
research.md D9), no en columnas JSONB, y el reporte de período devuelve los
mismos valores que recalcularlo desde las ventas (SC-008, FR-022).

`test_cash_shift_sales_report.py` y `test_cash_close_active_orders.py` NO se
tocan en US6 (SC-008): este archivo cubre lo que ellos no ven. El helper
`_seed_sale` es una copia a propósito -- importarlo acoplaría este archivo al
otro.

Cada turno va en su PROPIA caja: el índice parcial `idx_open_shift_per_register`
(`postgresql_where=...`) se vuelve UNIQUE total bajo SQLite y no se remueve del
metadata para no alterar otros tests.

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_cash_report_batch_identity -v
"""
import random
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.cash import reports, router
from app.api.v1.cash.schemas import ShiftClose

# spec 121 (D-1/D-2, A-130/A-131): ya no existe el cierre "sin conteo". Estos tests
# cierran con efectivo contado $0 (lista vacía), que casi siempre difiere del esperado
# y exige observación; el usuario que cierra ya no se descarta (FR-029).
_NOTA_CIERRE = "Cierre de prueba sin conteo (spec 121: D-1/D-2)"
from app.models.sale import Sale, SaleItem
from app.models.shift_sales_report import (
    ShiftSalesReport, ShiftSalesReportCategory, ShiftSalesReportProduct,
)


def _seed_sale(db, shift, variant, *, total: Decimal, quantity: int = 1,
               description: str | None = None, sold_at: datetime | None = None) -> Sale:
    """Venta pagada del turno `shift` -- sin `Payment` (el arqueo de efectivo no
    es el objeto de esta spec). Desde la spec 121 (D-1/D-2, A-130/A-131) el cierre
    ya siempre concilia: los tests cierran con contado $0 y una observación
    (`_NOTA_CIERRE`) en vez de saltarse el arqueo con `ShiftClose()` vacío."""
    sale = Sale(
        id=uuid4(), cash_shift_id=shift.id, user_id=uuid4(), user_name="Cajero de prueba",
        customer_name="Mostrador", subtotal=total, total=total, status="paid",
        sold_at=sold_at or datetime.now(),
    )
    db.add(sale)
    db.flush()
    db.add(SaleItem(
        id=uuid4(), sale_id=sale.id, product_variant_id=variant.id,
        description=description or f"producto-{variant.id}",
        options=[], quantity=quantity, unit_price=total / quantity, line_total=total,
    ))
    db.flush()
    return sale


class TestSnapshotEnFilas(unittest.TestCase):
    """FR-013/FR-017, research.md D9/D15: al cerrar, el desglose COMPLETO (no
    top-10) queda en filas de dos tablas hijas y la cabecera solo lleva totales."""

    def test_cerrar_un_turno_con_12_productos_guarda_las_12_filas_y_no_solo_el_top_10(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        category = fx.make_category(db, name="Helados")
        variants = []
        for i in range(12):
            product = fx.make_product(db, category=category, name=f"Producto {i:02d}")
            variants.append(fx.make_variant(db, product=product, price=Decimal("1000")))
        db.commit()
        for i, variant in enumerate(variants):
            _seed_sale(db, shift, variant, total=Decimal(1000 * (i + 1)), quantity=i + 1,
                       description=f"Producto {i:02d}")
        db.commit()

        expected = reports.compute_shift_sales_report(db, shift)
        router.close_shift(shift.id, ShiftClose(close_note=_NOTA_CIERRE), db, fx.make_user_double())

        productos = db.execute(
            select(ShiftSalesReportProduct).where(ShiftSalesReportProduct.cash_shift_id == shift.id)
        ).scalars().all()
        categorias = db.execute(
            select(ShiftSalesReportCategory).where(ShiftSalesReportCategory.cash_shift_id == shift.id)
        ).scalars().all()

        self.assertGreater(len(productos), reports.TOP_PRODUCTS_LIMIT)
        self.assertEqual(len(productos), 12)
        self.assertEqual(len(categorias), 1)

        guardado = {
            str(p.product_variant_id): (p.name, p.units, Decimal(p.revenue)) for p in productos
        }
        esperado = {
            r["product_variant_id"]: (r["name"], r["units"], Decimal(r["revenue"]))
            for r in expected["products_breakdown"]
        }
        self.assertEqual(guardado, esperado)

        cat = categorias[0]
        self.assertEqual(str(cat.category_id), expected["categories_breakdown"][0]["category_id"])
        self.assertEqual(cat.category_name, "Helados")
        self.assertEqual(cat.units, expected["categories_breakdown"][0]["units"])
        self.assertEqual(Decimal(cat.revenue), Decimal(expected["categories_breakdown"][0]["revenue"]))

    def test_la_cabecera_guarda_los_totales_y_ya_no_tiene_atributos_de_desglose(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        variant = fx.make_variant(db, price=Decimal("10000"))
        db.commit()
        _seed_sale(db, shift, variant, total=Decimal("30000"), quantity=3)
        _seed_sale(db, shift, variant, total=Decimal("20000"), quantity=2)
        db.commit()

        router.close_shift(shift.id, ShiftClose(close_note=_NOTA_CIERRE), db, fx.make_user_double())

        cabecera = db.execute(
            select(ShiftSalesReport).where(ShiftSalesReport.cash_shift_id == shift.id)
        ).scalar_one()
        self.assertEqual(Decimal(cabecera.total_sales), Decimal("50000"))
        self.assertEqual(cabecera.order_count, 2)
        self.assertEqual(Decimal(cabecera.avg_ticket), Decimal("25000.00"))
        self.assertFalse(hasattr(ShiftSalesReport, "products_breakdown"))
        self.assertFalse(hasattr(ShiftSalesReport, "categories_breakdown"))

    def test_turno_sin_ventas_deja_la_cabecera_en_cero_y_ninguna_fila_hija(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        db.commit()

        router.close_shift(shift.id, ShiftClose(close_note=_NOTA_CIERRE), db, fx.make_user_double())

        cabecera = db.execute(
            select(ShiftSalesReport).where(ShiftSalesReport.cash_shift_id == shift.id)
        ).scalar_one()
        self.assertEqual(Decimal(cabecera.total_sales), Decimal("0"))
        self.assertEqual(cabecera.order_count, 0)
        self.assertEqual(Decimal(cabecera.avg_ticket), Decimal("0"))
        self.assertEqual(db.execute(select(ShiftSalesReportProduct)).scalars().all(), [])
        self.assertEqual(db.execute(select(ShiftSalesReportCategory)).scalars().all(), [])

    def test_insertar_dos_veces_la_misma_variante_del_mismo_turno_viola_la_unicidad(self):
        db = fx.new_session()
        shift = fx.make_cash_shift(db)
        variant = fx.make_variant(db, price=Decimal("1000"))
        db.commit()
        for _ in range(2):
            db.add(ShiftSalesReportProduct(
                cash_shift_id=shift.id, product_variant_id=variant.id,
                name="Cono", units=1, revenue=Decimal("1000"),
            ))
        with self.assertRaises(IntegrityError):
            db.flush()
        db.rollback()


# ─── T052: identidad del reporte de período con el de antes (SC-008, FR-017, FR-022) ───

DIA = date(2026, 10, 15)
APERTURA = datetime(2026, 10, 15, 12, 0)  # = 07:00 en America/Bogota, el mismo día


def _id(n: int) -> UUID:
    """UUID fijo y ordenable. Debe contener una letra: SQLite declara la columna
    como `UUID` (afinidad NUMÉRICA) y un hex de solo dígitos se guardaría como
    entero y fallaría al leerlo."""
    return UUID(f"a0000000-0000-0000-0000-{n:012d}")


def _periodo_del_dia(db, **kw):
    return reports.compute_period_sales_report(
        db, fx.make_tenant_double(), "day", ref_date=DIA, **kw
    )


def _turno_d8(db, **kw):
    """Turno cerrado SIN cabecera de snapshot (anterior a esta spec, D8)."""
    kw.setdefault("opened_at", APERTURA)
    return fx.make_cash_shift(
        db, status="closed", closed_at=kw["opened_at"] + timedelta(hours=1), **kw
    )


def _cerrar(db, shift):
    router.close_shift(shift.id, ShiftClose(close_note=_NOTA_CIERRE), db, fx.make_user_double())


def _filas(rows):
    """`[(nombre, unidades, ingreso)]` de las filas de producto de un reporte."""
    return [(r["name"], r["units"], Decimal(r["revenue"])) for r in rows]


class TestEjemploNumericoDeUS6(unittest.TestCase):
    """Escenario 1 de US6: un turno congelado + uno abierto se recombinan sin
    sumar top-N ya recortados."""

    def test_turno_congelado_mas_turno_abierto(self):
        db = fx.new_session()
        category = fx.make_category(db, name="Helados")
        cono = fx.make_variant(db, product=fx.make_product(db, category=category, name="Cono doble"))
        malteada = fx.make_variant(db, product=fx.make_product(db, category=category, name="Malteada"))
        paleta = fx.make_variant(db, product=fx.make_product(db, category=category, name="Paleta"))
        turno_a = fx.make_cash_shift(db, opened_at=APERTURA)
        turno_b = fx.make_cash_shift(db, opened_at=APERTURA + timedelta(hours=2))
        db.commit()
        # Turno A: 6 órdenes, $86.000 -- Cono doble 10 u / $50.000 y Malteada 4 u / $36.000
        for _ in range(5):
            _seed_sale(db, turno_a, cono, total=Decimal("10000"), quantity=2, description="Cono doble")
        _seed_sale(db, turno_a, malteada, total=Decimal("36000"), quantity=4, description="Malteada")
        # Turno B: 4 órdenes, $70.000 -- Cono doble 6 u / $30.000 y Paleta 20 u / $40.000
        for _ in range(2):
            _seed_sale(db, turno_b, cono, total=Decimal("15000"), quantity=3, description="Cono doble")
        for _ in range(2):
            _seed_sale(db, turno_b, paleta, total=Decimal("20000"), quantity=10, description="Paleta")
        db.commit()
        _cerrar(db, turno_a)  # solo A queda congelado; B sigue abierto

        r = _periodo_del_dia(db)

        self.assertEqual(r["shift_ids"], [turno_a.id, turno_b.id])
        self.assertTrue(r["is_live"])
        self.assertEqual(r["total_sales"], Decimal("156000"))
        self.assertEqual(r["order_count"], 10)
        self.assertEqual(r["avg_ticket"], Decimal("15600.00"))
        self.assertEqual(_filas(r["top_products"]), [
            ("Cono doble", 16, Decimal("80000")),
            ("Paleta", 20, Decimal("40000")),
            ("Malteada", 4, Decimal("36000")),
        ])
        self.assertEqual(len(r["categories"]), 1)
        self.assertEqual(r["categories"][0]["category_name"], "Helados")
        self.assertEqual(r["categories"][0]["units"], 40)
        self.assertEqual(Decimal(r["categories"][0]["revenue"]), Decimal("156000"))


class TestNombreMostrado(unittest.TestCase):
    """FR-022, research.md D11: el nombre de una variante en el período es el del
    turno MÁS ANTIGUO donde aparece y, dentro de un turno, el mayor `description`."""

    def _variante(self, db, nombre="Cono"):
        category = fx.make_category(db, name="Helados")
        return fx.make_variant(db, product=fx.make_product(db, category=category, name=nombre))

    def test_el_nombre_es_el_del_turno_mas_antiguo_aunque_este_congelado(self):
        db = fx.new_session()
        variante = self._variante(db)
        viejo = fx.make_cash_shift(db, opened_at=APERTURA)
        nuevo = _turno_d8(db, opened_at=APERTURA + timedelta(hours=2))
        db.commit()
        _seed_sale(db, viejo, variante, total=Decimal("1000"), description="Cono (viejo)")
        _seed_sale(db, nuevo, variante, total=Decimal("1000"), description="Cono (nuevo)")
        db.commit()
        _cerrar(db, viejo)

        r = _periodo_del_dia(db)

        self.assertEqual([row["name"] for row in r["top_products"]], ["Cono (viejo)"])

    def test_el_nombre_es_el_del_turno_mas_antiguo_aunque_este_en_vivo(self):
        db = fx.new_session()
        variante = self._variante(db)
        viejo = _turno_d8(db, opened_at=APERTURA)
        nuevo = fx.make_cash_shift(db, opened_at=APERTURA + timedelta(hours=2))
        db.commit()
        _seed_sale(db, viejo, variante, total=Decimal("1000"), description="Cono (viejo)")
        _seed_sale(db, nuevo, variante, total=Decimal("1000"), description="Cono (nuevo)")
        db.commit()
        _cerrar(db, nuevo)

        r = _periodo_del_dia(db)

        self.assertEqual([row["name"] for row in r["top_products"]], ["Cono (viejo)"])

    def test_dentro_de_un_turno_gana_el_mayor_description(self):
        db = fx.new_session()
        variante = self._variante(db)
        turno = _turno_d8(db)
        db.commit()
        _seed_sale(db, turno, variante, total=Decimal("1000"), description="Cono A")
        _seed_sale(db, turno, variante, total=Decimal("1000"), description="Cono Z")
        db.commit()

        r = _periodo_del_dia(db)

        self.assertEqual([row["name"] for row in r["top_products"]], ["Cono Z"])

    def test_con_la_misma_hora_de_apertura_desempata_el_id_del_turno(self):
        db = fx.new_session()
        variante = self._variante(db)
        con_id_2 = _turno_d8(db, id=_id(2))
        con_id_1 = _turno_d8(db, id=_id(1))
        db.commit()
        _seed_sale(db, con_id_2, variante, total=Decimal("1000"), description="del turno 2")
        _seed_sale(db, con_id_1, variante, total=Decimal("1000"), description="del turno 1")
        db.commit()

        r = _periodo_del_dia(db)

        self.assertEqual([row["name"] for row in r["top_products"]], ["del turno 1"])
        self.assertEqual(r["shift_ids"], [_id(1), _id(2)])

    def test_el_nombre_de_la_categoria_es_el_del_turno_mas_antiguo(self):
        db = fx.new_session()
        category = fx.make_category(db, name="Helados")
        variante = fx.make_variant(db, product=fx.make_product(db, category=category, name="Cono"))
        viejo = fx.make_cash_shift(db, opened_at=APERTURA)
        db.commit()
        _seed_sale(db, viejo, variante, total=Decimal("1000"))
        db.commit()
        _cerrar(db, viejo)  # congela "Helados"
        category.name = "Postres"  # renombrada después
        nuevo = _turno_d8(db, opened_at=APERTURA + timedelta(hours=2))
        db.commit()
        _seed_sale(db, nuevo, variante, total=Decimal("1000"))
        db.commit()

        r = _periodo_del_dia(db)

        self.assertEqual([c["category_name"] for c in r["categories"]], ["Helados"])
        self.assertEqual(r["categories"][0]["units"], 2)


class TestOrdenEnEmpates(unittest.TestCase):
    """FR-022: ingreso descendente, nombre ascendente, identificador ascendente
    (y `category_id` nulo al final). El orden de inserción NO coincide con el
    esperado. Nombres con la misma capitalización (colación, research.md D11)."""

    def _sembrar_empates(self, db, turno):
        category = fx.make_category(db, name="Helados")
        # `Product.name` es único por categoría: el nombre que muestra el reporte es
        # el `description` de la línea de venta, que sí se repite ("Alfa" dos veces).
        mk = lambda nombre, n: fx.make_variant(  # noqa: E731
            db, product=fx.make_product(db, category=category, name=f"{nombre}-{n}"), id=_id(n)
        )
        beta, alfa_2, alfa_1, zeta = mk("Beta", 3), mk("Alfa", 2), mk("Alfa", 1), mk("Zeta", 4)
        db.commit()
        _seed_sale(db, turno, beta, total=Decimal("10000"), description="Beta")
        _seed_sale(db, turno, alfa_2, total=Decimal("10000"), description="Alfa")
        _seed_sale(db, turno, alfa_1, total=Decimal("10000"), description="Alfa")
        _seed_sale(db, turno, zeta, total=Decimal("20000"), description="Zeta")
        db.commit()

    def _esperado(self):
        return [
            ("Zeta", _id(4)), ("Alfa", _id(1)), ("Alfa", _id(2)), ("Beta", _id(3)),
        ]

    def test_productos_empatados_en_turno_en_vivo(self):
        db = fx.new_session()
        turno = _turno_d8(db)
        self._sembrar_empates(db, turno)

        r = _periodo_del_dia(db)

        self.assertEqual(
            [(row["name"], UUID(row["product_variant_id"])) for row in r["top_products"]],
            self._esperado(),
        )

    def test_productos_empatados_en_turno_congelado(self):
        db = fx.new_session()
        turno = fx.make_cash_shift(db, opened_at=APERTURA)
        self._sembrar_empates(db, turno)
        _cerrar(db, turno)

        r = _periodo_del_dia(db)

        self.assertEqual(
            [(row["name"], UUID(row["product_variant_id"])) for row in r["top_products"]],
            self._esperado(),
        )

    def test_categorias_empatadas_ordenan_por_nombre(self):
        db = fx.new_session()
        turno = _turno_d8(db)
        for nombre, ingreso in (("Beta", "10000"), ("Zeta", "20000"), ("Alfa", "10000")):
            category = fx.make_category(db, name=nombre)
            variante = fx.make_variant(db, product=fx.make_product(db, category=category, name=nombre))
            db.commit()
            _seed_sale(db, turno, variante, total=Decimal(ingreso), description=nombre)
        db.commit()

        r = _periodo_del_dia(db)

        self.assertEqual([c["category_name"] for c in r["categories"]], ["Zeta", "Alfa", "Beta"])

    def test_categorias_empatadas_con_id_nulo_al_final(self):
        db = fx.new_session()
        turno = fx.make_cash_shift(db, opened_at=APERTURA)
        db.commit()
        _cerrar(db, turno)  # cabecera con totales en cero
        # `Product.category_id` es NOT NULL: la fila "Sin categoría" solo se puede
        # sembrar directamente en la tabla del snapshot.
        for category_id in (_id(2), None, _id(1)):
            db.add(ShiftSalesReportCategory(
                cash_shift_id=turno.id, category_id=category_id, category_name="Mixta",
                units=1, revenue=Decimal("100"),
            ))
        db.commit()

        r = _periodo_del_dia(db)

        ids = [UUID(c["category_id"]) if c["category_id"] else None for c in r["categories"]]
        self.assertEqual(ids, [_id(1), _id(2), None])


class TestCasosBorde(unittest.TestCase):
    def test_filas_hijas_huerfanas_sin_cabecera_se_ignoran(self):
        db = fx.new_session()
        variante = fx.make_variant(db)
        turno = _turno_d8(db)
        db.commit()
        _seed_sale(db, turno, variante, total=Decimal("1000"), description="Real")
        db.add(ShiftSalesReportProduct(
            cash_shift_id=turno.id, product_variant_id=uuid4(), name="Huérfana",
            units=999, revenue=Decimal("999999"),
        ))
        db.commit()

        r = _periodo_del_dia(db)

        self.assertEqual([row["name"] for row in r["top_products"]], ["Real"])
        self.assertEqual(r["total_sales"], Decimal("1000"))
        self.assertTrue(r["is_live"])

    def test_ticket_promedio_usa_redondeo_half_even_de_decimal(self):
        """$0,02 + $0,03 = $0,05 en 2 órdenes -> 0,025: half-even da 0,02 y el
        `ROUND` de SQL (half-away-from-zero) daría 0,03 (research.md D11)."""
        db = fx.new_session()
        variante = fx.make_variant(db)
        turno = _turno_d8(db)
        db.commit()
        _seed_sale(db, turno, variante, total=Decimal("0.02"))
        _seed_sale(db, turno, variante, total=Decimal("0.03"))
        db.commit()

        r = _periodo_del_dia(db)

        self.assertEqual(r["total_sales"], Decimal("0.05"))
        self.assertEqual(r["order_count"], 2)
        self.assertEqual(r["avg_ticket"], Decimal("0.02"))

    def test_turno_cerrado_sin_ventas_conserva_la_escala_del_contrato(self):
        """US5/FR-014: el contrato devuelve `total_sales` y `avg_ticket` como
        "0.00" (no "0") para un turno cerrado sin ventas -- la escala de la
        cabecera del snapshot no se pierde al sumar."""
        db = fx.new_session()
        turno = fx.make_cash_shift(db, opened_at=APERTURA)
        db.commit()
        _cerrar(db, turno)

        r = router.shift_sales_report(turno.id, db)

        self.assertEqual(str(r["total_sales"]), "0.00")
        self.assertEqual(str(r["avg_ticket"]), "0.00")
        self.assertEqual(r["order_count"], 0)

    def test_periodo_sin_turnos_devuelve_ceros_y_listas_vacias(self):
        db = fx.new_session()

        r = _periodo_del_dia(db)

        self.assertEqual(r["shift_ids"], [])
        self.assertFalse(r["is_live"])
        self.assertEqual(r["total_sales"], Decimal("0"))
        self.assertEqual(r["order_count"], 0)
        self.assertEqual(r["avg_ticket"], Decimal("0.00"))
        self.assertEqual(r["top_products"], [])
        self.assertEqual(r["categories"], [])

    def test_el_filtro_por_caja_no_mezcla_turnos_de_otras_cajas(self):
        """SC-004: aislamiento por caja (el aislamiento entre tenants es el del
        esquema: cada sesión de este fixture es una base distinta)."""
        db = fx.new_session()
        variante = fx.make_variant(db)
        caja_a, caja_b = fx.make_cash_register(db, name="A"), fx.make_cash_register(db, name="B")
        turno_a = _turno_d8(db, register=caja_a)
        turno_b = _turno_d8(db, register=caja_b)
        db.commit()
        _seed_sale(db, turno_a, variante, total=Decimal("1000"))
        _seed_sale(db, turno_b, variante, total=Decimal("5000"))
        db.commit()

        solo_a = _periodo_del_dia(db, cash_register_id=caja_a.id)
        todas = _periodo_del_dia(db)
        ninguna = _periodo_del_dia(db, cash_register_id=uuid4())

        self.assertEqual(solo_a["shift_ids"], [turno_a.id])
        self.assertEqual(solo_a["total_sales"], Decimal("1000"))
        self.assertEqual(todas["total_sales"], Decimal("6000"))
        self.assertEqual(ninguna["shift_ids"], [])
        self.assertEqual(ninguna["total_sales"], Decimal("0"))


class TestIdentidadConElOraculo(unittest.TestCase):
    """Un período que mezcla snapshot + abierto + D8 devuelve lo MISMO que
    recalcular desde cero con una función de referencia escrita en Python puro
    sobre `Sale`/`SaleItem` (sin reutilizar código de `reports.py`)."""

    def _oraculo(self, db, shifts):
        por_turno = {s.id: s for s in shifts}
        ventas = db.execute(select(Sale).where(Sale.status == "paid")).scalars().all()
        ventas = [v for v in ventas if v.cash_shift_id in por_turno]
        total = sum((v.total for v in ventas), Decimal("0"))
        # Por (turno, variante): nombre = MAX(description) del turno; luego, el del turno más antiguo.
        items = db.execute(select(SaleItem)).scalars().all()
        ids_venta = {v.id: v.cash_shift_id for v in ventas}
        por_turno_variante: dict = {}
        for it in items:
            turno_id = ids_venta.get(it.sale_id)
            if turno_id is None:
                continue
            acc = por_turno_variante.setdefault((turno_id, it.product_variant_id), [it.description, 0, Decimal("0")])
            acc[0] = max(acc[0], it.description)
            acc[1] += it.quantity
            acc[2] += it.line_total
        productos: dict = {}
        for (turno_id, variante_id), (nombre, unidades, ingreso) in sorted(
            por_turno_variante.items(),
            key=lambda kv: (por_turno[kv[0][0]].opened_at, str(kv[0][0])),
        ):
            fila = productos.setdefault(variante_id, [nombre, 0, Decimal("0")])
            fila[1] += unidades
            fila[2] += ingreso
        top = sorted(productos.items(), key=lambda kv: (-kv[1][2], kv[1][0], str(kv[0])))[:10]
        return total, len(ventas), [(n, u, i) for _, (n, u, i) in top]

    def test_mezcla_de_snapshot_abierto_y_d8_coincide_con_el_oraculo(self):
        rng = random.Random(119)
        db = fx.new_session()
        category = fx.make_category(db, name="Helados")
        variantes = [
            fx.make_variant(db, product=fx.make_product(db, category=category, name=f"P{i}"))
            for i in range(5)
        ]
        shifts = []
        for i, tipo in enumerate(["snapshot", "open", "d8", "snapshot", "open", "d8"]):
            opened_at = APERTURA + timedelta(minutes=10 * i)
            shifts.append(
                _turno_d8(db, opened_at=opened_at) if tipo == "d8"
                else fx.make_cash_shift(db, opened_at=opened_at)
            )
        db.commit()
        for i, turno in enumerate(shifts):
            for _ in range(rng.randint(3, 5)):
                variante = rng.choice(variantes)
                cantidad = rng.randint(1, 4)
                _seed_sale(
                    db, turno, variante, total=Decimal(rng.randint(1, 50) * 100) * cantidad,
                    quantity=cantidad, description=f"{variante.id.hex[:4]} t{i}",
                    sold_at=turno.opened_at,
                )
        db.commit()
        for turno, tipo in zip(shifts, ["snapshot", "open", "d8", "snapshot", "open", "d8"]):
            if tipo == "snapshot":
                _cerrar(db, turno)

        r = _periodo_del_dia(db)
        total, ordenes, top = self._oraculo(db, shifts)

        self.assertEqual(r["shift_ids"], [s.id for s in sorted(shifts, key=lambda s: (s.opened_at, str(s.id)))])
        self.assertTrue(r["is_live"])
        self.assertEqual(r["total_sales"], total)
        self.assertEqual(r["order_count"], ordenes)
        self.assertEqual(_filas(r["top_products"]), top)

    def test_is_live_y_shift_ids_en_cada_composicion(self):
        for tipos, esperado_live in ((["snapshot"], False), (["snapshot", "open"], True),
                                      (["snapshot", "d8"], True), (["d8"], True), (["open"], True)):
            with self.subTest(tipos=tipos):
                db = fx.new_session()
                shifts = []
                for i, tipo in enumerate(tipos):
                    opened_at = APERTURA + timedelta(minutes=i)
                    shifts.append(
                        _turno_d8(db, opened_at=opened_at) if tipo == "d8"
                        else fx.make_cash_shift(db, opened_at=opened_at)
                    )
                db.commit()
                for turno, tipo in zip(shifts, tipos):
                    if tipo == "snapshot":
                        _cerrar(db, turno)

                r = _periodo_del_dia(db)

                self.assertEqual(r["is_live"], esperado_live)
                self.assertEqual(r["shift_ids"], [s.id for s in shifts])


if __name__ == "__main__":
    unittest.main()
