"""`app/api/v1/orders/service.py`: `create_order` (CONGELA comportamiento
actual, specs/017-caracterizacion-orders, Historia 5) más `order_has_sale`/
`paid_order_ids`, agregadas por spec 029 — estas dos últimas no son
characterization tests (no existía comportamiento previo que congelar), son
la verificación de la señal "pedido ya pagado" introducida por esa spec.

`create_order` es la comanda directa de mostrador/mesero que nace ya
`abierta` (confirmada) y descuenta inventario al crearse, porque no vuelve a
pasar por `confirm_order`.

El caso de contraste directo con **A-04** (`create_order` sí pasa `variant` a
`load_valid_options`, a diferencia de `consolidation.add_item_to_table`) ya
vive en `test_orders_consolidation.py::test_create_order_contraste_a04_...`
(tasks.md T013/T051: se escribe una sola vez).

Ejecutar solo este módulo:

    python -m unittest app.characterization_tests.test_orders_service -v
"""
from decimal import Decimal
import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.characterization_tests import orders_fixtures as fx
from app.api.v1.orders import service
from app.api.v1.orders.schemas import OrderChannel, OrderCreate, OrderItemIn, OrderType
from app.api.v1.catalog.schemas import OptionSelectionIn
from app.models.inventory_movement import InventoryMovement
from app.models.sale import Sale

PRECIO = Decimal("10000")


class TestService(unittest.TestCase):
    def _seed_variant_con_receta_y_opciones(self, db):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("2"))
        group = fx.make_option_group(db, min_select=1, max_select=1)
        option = fx.make_option(db, group=group, extra_price=Decimal("500"))
        fx.link_variant_group(db, variant, group, min_select=1, max_select=1)
        return variant, insumo, option

    # ------------------------------------------------------------ create_order (T048)

    def test_create_order_nace_abierta_y_descuenta_inventario(self):
        """CONGELA comportamiento actual (`service.py:37+`, spec.md Historia
        5, escenario 1): variante con receta y opciones válidas →
        `create_order` nace en status 'abierta' (no 'recibida') con el
        inventario ya descontado — no vuelve a pasar por `confirm_order`."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        order = service.create_order(db, data, uuid4())

        self.assertEqual(order.status, "abierta")
        movimientos = db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == order.id)
        ).scalars().all()
        self.assertEqual(len(movimientos), 1)
        db.refresh(insumo)
        self.assertEqual(Decimal(insumo.current_stock), Decimal("998"))

    # ----------------------------------------------- create_order sin receta (T049)

    def test_create_order_variante_sin_receta_rechaza(self):
        """CONGELA comportamiento actual: variante sin receta asociada → la
        guarda de `deduct_order_items` rechaza la creación con 409, migrando
        el caso de `test_receta_obligatoria.py` correspondiente a este
        camino (research.md §5, SC-007)."""
        db = fx.new_session()
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant_sin_receta = fx.make_variant(db, product=product, price=PRECIO)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            items=[OrderItemIn(product_variant_id=variant_sin_receta.id, quantity=1)],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 409)

    # ------------------------------------------- create_order abre sesión (T050)

    def test_create_order_abre_sesion_de_mesa_si_no_existe(self):
        """CONGELA comportamiento actual (spec.md Historia 5, escenario 3):
        mesa sin sesión de mesa activa → `create_order` con
        `dining_table_id` crea la sesión de mesa vía
        `consolidation.get_or_create_table_session_id` antes de crear la
        orden — congelando la dependencia real entre `service.py` y
        `consolidation.py`."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="libre")
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("1"))
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            dining_table_id=table.id,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1)],
        )
        order = service.create_order(db, data, uuid4())

        self.assertIsNotNone(order.table_session_id)
        db.refresh(table)
        self.assertEqual(table.status, "ocupada")

    # ------------------------------------- create_order hold_for_payment (T019)

    def test_create_order_hold_for_payment_nace_recibida_sin_descontar(self):
        """Comportamiento nuevo (spec 028, T013): con `hold_for_payment=True`
        la comanda nace 'recibida' (no 'abierta') y no descuenta inventario —
        el descuento se mueve a `checkout.checkout_and_send`, al cobrar."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            hold_for_payment=True,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        order = service.create_order(db, data, uuid4())

        self.assertEqual(order.status, "recibida")
        movimientos = db.execute(
            select(InventoryMovement).where(InventoryMovement.reference_id == order.id)
        ).scalars().all()
        self.assertEqual(movimientos, [])
        db.refresh(insumo)
        self.assertEqual(Decimal(insumo.current_stock), Decimal("1000"))

    def test_create_order_hold_for_payment_con_channel_qr_400(self):
        """spec 028, T013: `hold_for_payment` es exclusivo de mostrador/mesero
        — combinado con `channel='QR_MENU'` es 400 (ese canal ya tiene su
        propio flujo 'recibida' vía `/cart/submit`)."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.QR_MENU,
            hold_for_payment=True,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 400)

    # ------------------------ mezcla de orígenes QR/mostrador (spec 028, T014)

    def test_create_order_bloqueado_por_pedido_qr_activo_en_la_mesa_409(self):
        """spec 028, FR-013 (T014): una mesa no mezcla orígenes de pedido a la
        vez. Si ya hay un pedido QR activo (no terminal) en la sesión de
        mesa, una comanda de mostrador/mesero no puede abrirse encima —
        simetría directa de `cart.service.submit_cart` (T015)."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)
        fx.make_customer_order(db, ts, channel="QR_MENU", status="recibida")
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            dining_table_id=table.id,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 409)

    def test_create_order_permite_si_el_pedido_qr_ya_es_terminal(self):
        """Contraste del test anterior: un pedido QR 'pagada' ya no es
        'activo', así que no bloquea una comanda nueva de mostrador/mesero en
        la misma mesa."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="ocupada")
        ts = fx.make_table_session(db, table=table)
        fx.make_customer_order(db, ts, channel="QR_MENU", status="pagada")
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Cliente de prueba",
            dining_table_id=table.id,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        order = service.create_order(db, data, uuid4())
        self.assertEqual(order.status, "abierta")

    # ---------------------------------------------- spec 055: "Para Llevar"

    def test_create_order_takeaway_con_mesa_rechaza_422(self):
        """spec 055, research.md Decisión 5: un pedido TAKEAWAY o DELIVERY
        nunca lleva mesa asociada."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="libre")
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.TAKEAWAY,
            dining_table_id=table.id,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 422)

    def test_create_order_delivery_con_mesa_rechaza_422(self):
        db = fx.new_session()
        table = fx.make_dining_table(db, status="libre")
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DELIVERY,
            dining_table_id=table.id,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 422)

    def test_create_order_takeaway_sin_mesa_crea_correctamente(self):
        """spec 055, FR-011: el pedido "Para Llevar" queda con order_type
        TAKEAWAY, canal POS y sin mesa asociada."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.TAKEAWAY,
            customer_name="Consumidor final",
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
            hold_for_payment=True,
        )
        order = service.create_order(db, data, uuid4())

        self.assertEqual(order.order_type, "TAKEAWAY")
        self.assertEqual(order.channel, "POS")
        self.assertIsNone(order.dining_table_id)
        self.assertEqual(order.customer_name, "Consumidor final")
        self.assertEqual(order.status, "recibida")

    # ---------------------- spec 055, FR-006/FR-007: combinaciones canal/tipo

    def test_create_order_combinacion_invalida_whatsapp_dine_in_rechaza_400(self):
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.WHATSAPP,
            order_type=OrderType.DINE_IN,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_create_order_combinacion_invalida_api_dine_in_rechaza_400(self):
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.API,
            order_type=OrderType.DINE_IN,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_create_order_combinacion_invalida_qr_menu_takeaway_rechaza_400(self):
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.QR_MENU,
            order_type=OrderType.TAKEAWAY,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_create_order_combinacion_invalida_qr_menu_delivery_rechaza_400(self):
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.QR_MENU,
            order_type=OrderType.DELIVERY,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_create_order_combinaciones_validas_no_rechaza(self):
        """data-model.md, tabla de combinaciones: POS admite los tres tipos;
        WHATSAPP/API admiten TAKEAWAY y DELIVERY."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        combinaciones = [
            (OrderChannel.POS, OrderType.DINE_IN),
            (OrderChannel.POS, OrderType.TAKEAWAY),
            (OrderChannel.POS, OrderType.DELIVERY),
            (OrderChannel.WHATSAPP, OrderType.TAKEAWAY),
            (OrderChannel.WHATSAPP, OrderType.DELIVERY),
            (OrderChannel.API, OrderType.TAKEAWAY),
            (OrderChannel.API, OrderType.DELIVERY),
        ]
        for channel, order_type in combinaciones:
            with self.subTest(channel=channel, order_type=order_type):
                # spec 056, FR-007: DELIVERY exige además dirección/valor del
                # domicilio -- se completan aquí para que este test siga
                # verificando únicamente la combinación canal×tipo de orden
                # (spec 055), no la obligatoriedad de esos campos nuevos
                # (cubierta aparte en TestCreateOrderDelivery). spec 087
                # (FR-005, A-88): customer_name ahora es obligatorio para los
                # tres tipos, no solo DELIVERY -- se incluye siempre.
                extra = (
                    dict(
                        delivery_address="Cra 45 #12-30",
                        delivery_fee=Decimal("6000"),
                    )
                    if order_type is OrderType.DELIVERY else {}
                )
                data = OrderCreate(
                    channel=channel,
                    order_type=order_type,
                    customer_name="Ana Torres",
                    items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
                    **extra,
                )
                order = service.create_order(db, data, uuid4())
                self.assertEqual(order.channel, channel.value)
                self.assertEqual(order.order_type, order_type.value)

    # -------------------------- customer_name obligatorio salvo Terminal (spec 087 A-88,
    # reabierta por spec 091 A-99 solo para channel=POS)

    def test_create_order_dine_in_sin_nombre_cliente_en_pos_usa_consumidor_final(self):
        """spec 091 (A-99): reabre A-88 solo para la Terminal -- un DINE_IN de channel=POS
        sin nombre ya no se rechaza, se guarda con "Consumidor final"."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DINE_IN,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        order = service.create_order(db, data, uuid4())
        self.assertEqual(order.customer_name, "Consumidor final")

    def test_create_order_takeaway_sin_nombre_cliente_en_pos_usa_consumidor_final(self):
        """spec 091 (A-99): igual que DINE_IN, para TAKEAWAY de channel=POS."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.TAKEAWAY,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        order = service.create_order(db, data, uuid4())
        self.assertEqual(order.customer_name, "Consumidor final")

    def test_create_order_takeaway_sin_nombre_cliente_en_whatsapp_sigue_rechazando_422(self):
        """spec 091 (A-99, research.md D6): el valor por defecto solo aplica al canal `POS`
        (Terminal) -- WHATSAPP/API + TAKEAWAY sin nombre siguen rechazándose exactamente como
        antes de esta spec (A-88, spec 087 sin cambios para estos canales)."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        for channel in (OrderChannel.WHATSAPP, OrderChannel.API):
            with self.subTest(channel=channel):
                data = OrderCreate(
                    channel=channel,
                    order_type=OrderType.TAKEAWAY,
                    items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
                )
                with self.assertRaises(HTTPException) as ctx:
                    service.create_order(db, data, uuid4())
                self.assertEqual(ctx.exception.status_code, 422)

    def test_create_order_dine_in_con_participant_sin_nombre_explicito_se_autocompleta(self):
        """Con `participant_id`, `customer_name` se completa desde
        `participant.display_label`/`display_name` (líneas ya existentes,
        antes de la validación nueva) — un pedido QR con comensal identificado
        sigue funcionando sin exigirle el campo explícito."""
        db = fx.new_session()
        ts = fx.make_table_session(db)
        participante = fx.make_participant(db, table_session=ts, display_name="Camila")
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.QR_MENU,
            order_type=OrderType.DINE_IN,
            participant_id=participante.id,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        order = service.create_order(db, data, uuid4())
        self.assertEqual(order.customer_name, "Camila")

    def test_create_order_dos_pedidos_en_la_misma_mesa_no_se_rechazan(self):
        """spec 087 (FR-007): comportamiento intencional -- `POST /orders`
        permite dos `CustomerOrder` con el mismo `dining_table_id` sin ningún
        rechazo (confirmado en investigación: ninguna constraint lo impide
        hoy). Hasta esta spec era "silencioso", sin ningún test que lo
        declarara; se documenta aquí como comportamiento nuevo autorizado."""
        db = fx.new_session()
        table = fx.make_dining_table(db, status="libre")
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Ana",
            dining_table_id=table.id,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        order_1 = service.create_order(db, data, uuid4())

        data_2 = OrderCreate(
            channel=OrderChannel.POS,
            customer_name="Beto",
            dining_table_id=table.id,
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        order_2 = service.create_order(db, data_2, uuid4())

        self.assertNotEqual(order_1.id, order_2.id)
        self.assertEqual(order_1.dining_table_id, table.id)
        self.assertEqual(order_2.dining_table_id, table.id)

    def test_create_order_delivery_sigue_validado_una_sola_vez(self):
        """DELIVERY ya exigía `customer_name` desde spec 056 (FR-007, arriba)
        -- confirma que la validación nueva de DINE_IN/TAKEAWAY no la duplica
        ni cambia su mensaje/status para este tipo."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DELIVERY,
            delivery_address="Cra 45 #12-30",
            delivery_fee=Decimal("6000"),
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(
            ctx.exception.detail,
            "Un pedido a domicilio requiere nombre del cliente, dirección y "
            "valor del domicilio.",
        )


class TestCreateOrderDelivery(unittest.TestCase):
    """spec 056: habilitación del tipo de orden DELIVERY en la creación
    manual — campos de entrega, obligatoriedad, y persistencia."""

    def _seed_variant_con_receta_y_opciones(self, db):
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        insumo = fx.make_inventory_item(db, current_stock=Decimal("1000"))
        fx.make_recipe_item(db, variant, insumo, quantity=Decimal("2"))
        group = fx.make_option_group(db, min_select=1, max_select=1)
        option = fx.make_option(db, group=group, extra_price=Decimal("500"))
        fx.link_variant_group(db, variant, group, min_select=1, max_select=1)
        return variant, insumo, option

    def test_create_order_delivery_completo_crea_correctamente(self):
        """FR-010: la orden queda con order_type DELIVERY, canal POS, sin
        mesa, y los cuatro datos de entrega guardados tal cual se enviaron."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DELIVERY,
            customer_name="Ana Torres",
            delivery_address="Cra 45 #12-30, apto 301",
            delivery_phone="3011234567",
            delivery_fee=Decimal("6000"),
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
            hold_for_payment=True,
        )
        order = service.create_order(db, data, uuid4())

        self.assertEqual(order.order_type, "DELIVERY")
        self.assertEqual(order.channel, "POS")
        self.assertIsNone(order.dining_table_id)
        self.assertEqual(order.customer_name, "Ana Torres")
        self.assertEqual(order.delivery_address, "Cra 45 #12-30, apto 301")
        self.assertEqual(order.delivery_phone, "3011234567")
        self.assertEqual(order.delivery_fee, Decimal("6000"))

    def test_create_order_delivery_sin_telefono_crea_correctamente(self):
        """FR-008: el teléfono nunca es obligatorio, ni siquiera para DELIVERY."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DELIVERY,
            customer_name="Ana Torres",
            delivery_address="Cra 45 #12-30",
            delivery_fee=Decimal("0"),
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        order = service.create_order(db, data, uuid4())

        self.assertIsNone(order.delivery_phone)
        self.assertEqual(order.delivery_fee, Decimal("0"))

    def test_create_order_delivery_sin_nombre_cliente_rechaza_422(self):
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DELIVERY,
            delivery_address="Cra 45 #12-30",
            delivery_fee=Decimal("6000"),
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 422)

    def test_create_order_delivery_sin_direccion_rechaza_422(self):
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DELIVERY,
            customer_name="Ana Torres",
            delivery_fee=Decimal("6000"),
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 422)

    def test_create_order_delivery_sin_valor_domicilio_rechaza_422(self):
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DELIVERY,
            customer_name="Ana Torres",
            delivery_address="Cra 45 #12-30",
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 422)

    def test_create_order_delivery_nombre_cliente_solo_espacios_rechaza_422(self):
        """FR-007: un nombre de solo espacios no cuenta como diligenciado."""
        db = fx.new_session()
        variant, insumo, option = self._seed_variant_con_receta_y_opciones(db)
        db.commit()

        data = OrderCreate(
            channel=OrderChannel.POS,
            order_type=OrderType.DELIVERY,
            customer_name="   ",
            delivery_address="Cra 45 #12-30",
            delivery_fee=Decimal("6000"),
            items=[OrderItemIn(product_variant_id=variant.id, quantity=1, options=[OptionSelectionIn(option_id=option.id)])],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(db, data, uuid4())
        self.assertEqual(ctx.exception.status_code, 422)

    def test_create_order_delivery_valor_negativo_rechaza_422(self):
        """FR-006: el valor del domicilio no admite negativos (validado ya en
        el schema Pydantic, Field(ge=0) — 422 de FastAPI/Pydantic)."""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            OrderCreate(
                channel=OrderChannel.POS,
                order_type=OrderType.DELIVERY,
                customer_name="Ana Torres",
                delivery_address="Cra 45 #12-30",
                delivery_fee=Decimal("-1"),
                items=[OrderItemIn(product_variant_id=uuid4(), quantity=1)],
            )


class TestOrderHasSale(unittest.TestCase):
    """Spec 029 (D2/D3 de research.md): `order_has_sale`/`paid_order_ids` son
    la señal real de "este pedido ya está pagado" — no `CustomerOrder.status`,
    que nunca llega a `"pagada"` en los caminos QR/mostrador vigentes
    (`checkout_and_send`/`_confirm_order_impl` dejan la orden en `"abierta"`
    a propósito, con la `Sale` ya emitida)."""

    def _seed_order_con_sale(self, *, order_status: str) -> tuple:
        db = fx.new_session()
        ts = fx.make_table_session(db)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status=order_status, channel="POS")
        shift = fx.make_cash_shift(db)
        cashier = fx.make_user_double()
        db.add(Sale(
            cash_shift_id=shift.id,
            customer_order_id=order.id,
            table_session_id=ts.id,
            user_id=cashier.id,
            status="paid",
        ))
        db.commit()
        return db, order

    def test_order_has_sale_true_sobre_orden_abierta_con_sale_camino_qr_mostrador(self):
        """El caso real que motivó la spec: `checkout_and_send`/
        `_confirm_order_impl` dejan la orden en 'abierta' con la `Sale` ya
        emitida — `order_has_sale` debe reconocerlo como pagado aunque
        `status` nunca haya llegado a 'pagada'."""
        db, order = self._seed_order_con_sale(order_status="abierta")

        self.assertTrue(service.order_has_sale(db, order.id))

    def test_order_has_sale_false_sobre_orden_abierta_sin_sale(self):
        db = fx.new_session()
        ts = fx.make_table_session(db)
        order = fx.make_customer_order(db, ts, status="abierta", channel="POS")
        db.commit()

        self.assertFalse(service.order_has_sale(db, order.id))

    def test_order_has_sale_false_en_delivery_sin_sale_sin_importar_el_avance_de_cocina(self):
        """Spec 112, US5/FR-017: el eje de cobro (`order_has_sale`) es
        independiente del eje operativo (`OrderItem.estado_cocina`) -- un
        pedido de Domicilio con todos sus ítems ya 'listo' (lo más avanzado
        que existe en ese eje; no hay un estado 'despachado' en el modelo,
        ver research.md D6) sigue sin pago mientras no exista una `Sale`."""
        db = fx.new_session()
        ts = fx.make_table_session(db)
        category = fx.make_category(db)
        product = fx.make_product(db, category=category)
        variant = fx.make_variant(db, product=product, price=PRECIO)
        order = fx.make_customer_order(db, ts, status="abierta", channel="POS", order_type="DELIVERY")
        fx.make_order_item(db, order, variant, estado_cocina="listo")
        db.commit()

        self.assertFalse(service.order_has_sale(db, order.id))

    def test_paid_order_ids_resuelve_en_bloque_para_una_lista_de_pedidos(self):
        """Versión en bloque: una sola consulta para todo un listado, en vez
        de N — el pedido sin `Sale` no aparece en el resultado."""
        db, pagado = self._seed_order_con_sale(order_status="abierta")
        ts = fx.make_table_session(db)
        sin_pagar = fx.make_customer_order(db, ts, status="abierta", channel="POS")
        db.commit()

        resultado = service.paid_order_ids(db, [pagado.id, sin_pagar.id])

        self.assertEqual(resultado, {pagado.id})

    def test_paid_order_ids_lista_vacia_no_consulta(self):
        db = fx.new_session()
        self.assertEqual(service.paid_order_ids(db, []), set())


class TestListOrdersActiveSessionsOnly(unittest.TestCase):
    """Spec 029, hotfix: `list_orders(active_sessions_only=True)` — un pedido
    ya pagado de una `TableSession` ya `'closed'` (visita anterior, mesa ya
    liberada y reabierta por QR) no debe reaparecer mezclado con la sesión
    activa de la misma mesa física. El caso real reportado en producción:
    liberar una mesa, volver a escanear su QR y ver los pedidos de la visita
    vieja junto al pedido nuevo."""

    def _seed(self, *, session_status: str, order_status: str, con_sale: bool):
        db = fx.new_session()
        ts = fx.make_table_session(db, status=session_status)
        order = fx.make_customer_order(db, ts, status=order_status, channel="POS")
        if con_sale:
            shift = fx.make_cash_shift(db)
            cashier = fx.make_user_double()
            db.add(Sale(
                cash_shift_id=shift.id, customer_order_id=order.id,
                table_session_id=ts.id, user_id=cashier.id, status="paid",
            ))
        db.commit()
        return db, ts, order

    def test_excluye_pedido_pagado_de_sesion_ya_cerrada(self):
        db, _, order = self._seed(session_status="closed", order_status="abierta", con_sale=True)

        resultado = service.list_orders(db, active_sessions_only=True)

        self.assertNotIn(order.id, [o.id for o in resultado])

    def test_conserva_pedido_sin_pagar_de_sesion_ya_cerrada_caso_huerfano(self):
        db, _, order = self._seed(session_status="closed", order_status="abierta", con_sale=False)

        resultado = service.list_orders(db, active_sessions_only=True)

        self.assertIn(order.id, [o.id for o in resultado])

    def test_conserva_pedido_pagado_de_sesion_activa(self):
        db, _, order = self._seed(session_status="active", order_status="abierta", con_sale=True)

        resultado = service.list_orders(db, active_sessions_only=True)

        self.assertIn(order.id, [o.id for o in resultado])

    def test_conserva_pedido_sin_table_session_id_mostrador_puro(self):
        db = fx.new_session()
        ts = fx.make_table_session(db)  # solo para satisfacer la firma del fixture
        order = fx.make_customer_order(
            db, ts, table_session_id=None, dining_table_id=None,
            status="abierta", channel="POS",
        )
        db.commit()

        resultado = service.list_orders(db, active_sessions_only=True)

        self.assertIn(order.id, [o.id for o in resultado])

    def test_sin_el_parametro_el_resultado_no_cambia_respecto_a_hoy(self):
        db, _, order = self._seed(session_status="closed", order_status="abierta", con_sale=True)

        resultado = service.list_orders(db)

        self.assertIn(order.id, [o.id for o in resultado])


class TestOrderLinesWithAddonsPerLine(unittest.TestCase):
    """spec 089 (A-94): las líneas con adicionales cobrados una vez por línea suman lo
    mismo en el cobro (`order_sale_lines`/`compute_bill`), en la respuesta HTTP del pedido y
    en la base del descuento; las de la terminal POS (`per_line=False`) no cambian."""

    def setUp(self):
        from app.models.order_item import OrderItemOption
        self.db = fx.new_session()
        self.table = fx.make_dining_table(self.db)
        self.ts = fx.make_table_session(self.db, table=self.table)
        self.order = fx.make_customer_order(self.db, self.ts, dining_table_id=self.table.id)
        self.variant = fx.make_variant(self.db, price=Decimal("15000"))
        self.group = fx.make_option_group(self.db)
        self.tocino = fx.make_option(self.db, group=self.group, extra_price=Decimal("3000"))
        self.OrderItemOption = OrderItemOption

    def _item(self, *, quantity, unit_price, addons, per_line):
        item = fx.make_order_item(
            self.db, self.order, self.variant, quantity=quantity,
            unit_price=unit_price, addons_total=addons,
        )
        self.db.add(self.OrderItemOption(
            order_item_id=item.id, option_id=self.tocino.id, quantity=1, per_line=per_line,
        ))
        self.db.flush()
        self.db.refresh(item)
        return item

    def test_order_sale_lines_suma_el_adicional_una_vez_y_no_lo_descuenta(self):
        from app.api.v1.orders import checkout
        self._item(quantity=2, unit_price=Decimal("15000"), addons=Decimal("3000"), per_line=True)
        [line] = checkout.order_sale_lines(self.db, self.order.id)
        self.assertEqual(line.unit_price, Decimal("15000"))
        self.assertEqual(line.addons_total, Decimal("3000"))
        self.assertEqual(line.line_total, Decimal("33000"))
        # La promoción nunca descuenta el adicional: la base es el propio unit_price (D3).
        self.assertEqual(line.base_unit_price, Decimal("15000"))
        self.assertEqual(line.options[0]["per_line"], True)

    def test_linea_por_unidad_no_cambia_de_total_ni_de_base(self):
        from app.api.v1.orders import checkout
        self._item(quantity=2, unit_price=Decimal("18000"), addons=Decimal("0"), per_line=False)
        [line] = checkout.order_sale_lines(self.db, self.order.id)
        self.assertEqual(line.line_total, Decimal("36000"))
        self.assertEqual(line.base_unit_price, Decimal("15000"))
        self.assertNotIn("per_line", line.options[0])

    def test_compute_bill_usa_line_total_incluidos_los_adicionales(self):
        from app.api.v1.orders import checkout
        self._item(quantity=2, unit_price=Decimal("15000"), addons=Decimal("3000"), per_line=True)
        bill = checkout.compute_bill(self.db, self.table.id)
        self.assertEqual(bill.total, Decimal("33000"))
        self.assertEqual(bill.orders[0].items[0].line_total, Decimal("33000"))

    def test_order_item_response_expone_addons_total_line_total_y_la_cantidad_del_adicional(self):
        from app.api.v1.orders.schemas import OrderItemResponse
        item = self._item(
            quantity=2, unit_price=Decimal("15000"), addons=Decimal("3000"), per_line=True
        )
        out = OrderItemResponse.model_validate(item)
        self.assertEqual(out.addons_total, Decimal("3000"))
        self.assertEqual(out.line_total, Decimal("33000"))
        # La comanda conserva la cantidad ELEGIDA del adicional: x1, no x2 (FR-006).
        self.assertEqual(out.options[0].quantity, 1)
        self.assertTrue(out.options[0].per_line)

    def test_order_item_response_de_una_linea_historica(self):
        from app.api.v1.orders.schemas import OrderItemResponse
        item = self._item(
            quantity=2, unit_price=Decimal("18000"), addons=Decimal("0"), per_line=False
        )
        out = OrderItemResponse.model_validate(item)
        self.assertEqual(out.addons_total, Decimal("0"))
        self.assertEqual(out.line_total, Decimal("36000"))

    def test_create_order_de_la_terminal_pos_sigue_por_unidad(self):
        """La terminal POS/mostrador no cambia (decisión de negocio "solo Menú QR")."""
        insumo = fx.make_inventory_item(self.db, current_stock=Decimal("100"))
        fx.make_recipe_item(self.db, self.variant, insumo, quantity=Decimal("1"))
        fx.link_variant_group(
            self.db, self.variant, self.group, min_select=0, max_select=1,
        )
        data = OrderCreate(
            channel=OrderChannel.POS, customer_name="Cliente de prueba",
            items=[OrderItemIn(
                product_variant_id=self.variant.id, quantity=2,
                options=[OptionSelectionIn(option_id=self.tocino.id)],
            )],
        )
        order = service.create_order(self.db, data, uuid4())
        [item] = order.items
        self.assertEqual(item.unit_price, Decimal("18000"))
        self.assertEqual(item.addons_total, Decimal("0"))
        self.assertEqual(item.line_total, Decimal("36000"))
        self.assertFalse(item.options[0].per_line)


class TestSoldOutNamesTheProduct(unittest.TestCase):
    """spec 089 (A-96, FR-029, research D18): al agregar una línea cuyo insumo se agota, la
    respuesta nombra el PRODUCTO, no solo el insumo, y no queda nada a medias. El status sigue
    siendo el de `InsufficientStockError` (400)."""

    def setUp(self):
        self.db = fx.new_session()
        category = fx.make_category(self.db)
        self.product = fx.make_product(self.db, category=category, name="Hamburguesa")
        self.variant = fx.make_variant(self.db, product=self.product, price=PRECIO)
        self.queso = fx.make_inventory_item(
            self.db, name="Queso", current_stock=Decimal("1")
        )
        fx.make_recipe_item(self.db, self.variant, self.queso, quantity=Decimal("5"))
        self.db.commit()

    def _assert_named(self, exc: HTTPException):
        self.assertEqual(exc.status_code, 400)
        self.assertIsInstance(exc.detail, dict)
        self.assertIn("«", exc.detail["error"])
        self.assertIn("Hamburguesa", exc.detail["error"])
        self.assertIn("está agotado", exc.detail["error"])
        self.assertIn("falta Queso", exc.detail["error"])
        self.assertIn("Hamburguesa", exc.detail["producto"])
        self.assertEqual(exc.detail["insumo"], "Queso")

    def _assert_nothing_left(self):
        from app.models.order_item import OrderItem
        self.db.expire_all()
        self.assertEqual(self.db.execute(select(OrderItem)).scalars().all(), [])
        self.assertEqual(Decimal(self.queso.current_stock), Decimal("1"))
        self.assertEqual(self.db.execute(select(InventoryMovement)).scalars().all(), [])

    def test_create_order_nombra_el_producto_agotado_y_no_deja_lineas_a_medias(self):
        data = OrderCreate(
            channel=OrderChannel.POS, customer_name="Cliente de prueba",
            items=[OrderItemIn(product_variant_id=self.variant.id, quantity=1)],
        )
        with self.assertRaises(HTTPException) as ctx:
            service.create_order(self.db, data, uuid4())
        self._assert_named(ctx.exception)
        self._assert_nothing_left()

    def test_add_item_to_order_nombra_el_producto_agotado(self):
        from app.api.v1.orders import consolidation
        ts = fx.make_table_session(self.db)
        order = fx.make_customer_order(self.db, ts, status="abierta", channel="POS")
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            consolidation.add_item_to_order(
                self.db, order.id,
                OrderItemIn(product_variant_id=self.variant.id, quantity=1),
                fx.make_user_double(),
            )
        self._assert_named(ctx.exception)
        self._assert_nothing_left()

    def test_reemplazo_de_linea_nombra_el_producto_agotado_y_conserva_la_original(self):
        from app.api.v1.orders import kitchen
        from app.api.v1.orders.schemas import VoidItemIn
        ts = fx.make_table_session(self.db)
        order = fx.make_customer_order(self.db, ts, status="abierta", channel="POS")
        otro = fx.make_variant(self.db, product=self.product, price=PRECIO)
        fx.make_recipe_item(self.db, otro, self.queso, quantity=Decimal("0.1"))
        original = fx.make_order_item(
            self.db, order, otro, estado_cocina="pendiente", quantity=1
        )
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            kitchen.void_item(
                self.db, original.id,
                VoidItemIn(
                    motivo="cambio",
                    replacement=OrderItemIn(product_variant_id=self.variant.id, quantity=1),
                ),
                fx.make_user_double(),
            )
        self._assert_named(ctx.exception)
        self.db.expire_all()
        # Rollback total: la línea original sigue viva y sin reversa.
        self.assertNotEqual(self.db.get(type(original), original.id).estado_cocina, "anulado")


if __name__ == "__main__":
    unittest.main()
