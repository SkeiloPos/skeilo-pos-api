"""spec 129: adiciones (toppings) configuradas a nivel de producto

Revision ID: 43e57b9da5d9
Revises: cfa621f5b865
Create Date: 2026-10-19 00:00:00.000000

Por cada esquema de tenant, todo aditivo:

  1. `products` gana `is_topping BOOLEAN NOT NULL DEFAULT false` y
     `visible_en_qr BOOLEAN NOT NULL DEFAULT true` -- todo producto existente queda
     como producto regular visible en QR (el único estado posible antes de esta
     spec), sin backfill de datos (research.md D7/data-model.md de spec 129).
  2. `product_toppings`: qué adición ofrece un producto, a nivel de producto (no de
     variante) -- reemplaza, para adiciones, al enlace variante->grupo de opciones.
  3. `cart_item_toppings` / `order_item_toppings`: adición adjunta a una línea de
     carrito/pedido -- espejo de `cart_item_options`/`order_item_options`, pero
     referenciando la variante de un producto-adición en vez de una `Option`.

Ningún paso reescribe, recalcula ni cambia el significado de una fila existente
(Principio VII). `OptionGroup`/`Option`/`VariantOptionGroup` no se tocan.
"""
from typing import Sequence, Union

from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = '43e57b9da5d9'
down_revision: Union[str, Sequence[str], None] = 'cfa621f5b865'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(schema: str, table: str) -> bool:
    return op.get_bind().execute(
        text("SELECT to_regclass(:q)"), {"q": f"{schema}.{table}"}
    ).scalar() is not None


@for_each_tenant_schema
def upgrade(schema: str) -> None:
    if not _has_table(schema, "products"):
        return

    op.add_column(
        "products",
        sa.Column("is_topping", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=schema,
    )
    op.add_column(
        "products",
        sa.Column("visible_en_qr", sa.Boolean(), nullable=False, server_default=sa.true()),
        schema=schema,
    )

    op.create_table(
        "product_toppings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("product_id", sa.UUID(), nullable=False),
        sa.Column("topping_product_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "product_id <> topping_product_id",
            name=op.f("ck__product_toppings__ck_product_toppings_no_self_association"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], [f"{schema}.products.id"],
            name=op.f("fk__product_toppings__product_id__products"), ondelete="CASCADE",
        ),
        # Sin ondelete: una adición referenciada no puede borrarse físicamente -- se
        # retira por soft-delete (`active=false`), igual que `option_group_id` en
        # `variant_option_groups`.
        sa.ForeignKeyConstraint(
            ["topping_product_id"], [f"{schema}.products.id"],
            name=op.f("fk__product_toppings__topping_product_id__products"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__product_toppings")),
        sa.UniqueConstraint(
            "product_id", "topping_product_id",
            name="uq__product_toppings__product_id__topping_product_id",
        ),
        schema=schema,
    )
    op.create_index(
        op.f("ix__product_toppings__product_id"), "product_toppings",
        ["product_id"], schema=schema,
    )
    op.create_index(
        op.f("ix__product_toppings__topping_product_id"), "product_toppings",
        ["topping_product_id"], schema=schema,
    )

    op.create_table(
        "cart_item_toppings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("cart_item_id", sa.UUID(), nullable=False),
        sa.Column("topping_product_variant_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.CheckConstraint(
            "quantity > 0",
            name=op.f("ck__cart_item_toppings__ck_cart_item_toppings_quantity_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["cart_item_id"], [f"{schema}.cart_items.id"],
            name=op.f("fk__cart_item_toppings__cart_item_id__cart_items"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topping_product_variant_id"], [f"{schema}.product_variants.id"],
            name=op.f("fk__cart_item_toppings__topping_product_variant_id__product_variants"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__cart_item_toppings")),
        sa.UniqueConstraint(
            "cart_item_id", "topping_product_variant_id",
            name="uq__cart_item_toppings__cart_item_id__topping_variant_id",
        ),
        schema=schema,
    )
    op.create_index(
        op.f("ix__cart_item_toppings__cart_item_id"), "cart_item_toppings",
        ["cart_item_id"], schema=schema,
    )
    op.create_index(
        op.f("ix__cart_item_toppings__topping_product_variant_id"), "cart_item_toppings",
        ["topping_product_variant_id"], schema=schema,
    )

    op.create_table(
        "order_item_toppings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("order_item_id", sa.UUID(), nullable=False),
        sa.Column("topping_product_variant_id", sa.UUID(), nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("added_after_sale", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("complementary_sale_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "quantity > 0",
            name=op.f("ck__order_item_toppings__ck_order_item_toppings_quantity_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["order_item_id"], [f"{schema}.order_items.id"],
            name=op.f("fk__order_item_toppings__order_item_id__order_items"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topping_product_variant_id"], [f"{schema}.product_variants.id"],
            name=op.f("fk__order_item_toppings__topping_product_variant_id__product_variants"),
        ),
        sa.ForeignKeyConstraint(
            ["complementary_sale_id"], [f"{schema}.sales.id"],
            name=op.f("fk__order_item_toppings__complementary_sale_id__sales"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk__order_item_toppings")),
        sa.UniqueConstraint(
            "order_item_id", "topping_product_variant_id",
            name="uq__order_item_toppings__order_item_id__topping_variant_id",
        ),
        schema=schema,
    )
    op.create_index(
        op.f("ix__order_item_toppings__order_item_id"), "order_item_toppings",
        ["order_item_id"], schema=schema,
    )
    op.create_index(
        op.f("ix__order_item_toppings__topping_product_variant_id"), "order_item_toppings",
        ["topping_product_variant_id"], schema=schema,
    )


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    if not _has_table(schema, "products"):
        return

    if _has_table(schema, "order_item_toppings"):
        op.drop_index(op.f("ix__order_item_toppings__topping_product_variant_id"),
                      table_name="order_item_toppings", schema=schema)
        op.drop_index(op.f("ix__order_item_toppings__order_item_id"),
                      table_name="order_item_toppings", schema=schema)
        op.drop_table("order_item_toppings", schema=schema)

    if _has_table(schema, "cart_item_toppings"):
        op.drop_index(op.f("ix__cart_item_toppings__topping_product_variant_id"),
                      table_name="cart_item_toppings", schema=schema)
        op.drop_index(op.f("ix__cart_item_toppings__cart_item_id"),
                      table_name="cart_item_toppings", schema=schema)
        op.drop_table("cart_item_toppings", schema=schema)

    if _has_table(schema, "product_toppings"):
        op.drop_index(op.f("ix__product_toppings__topping_product_id"),
                      table_name="product_toppings", schema=schema)
        op.drop_index(op.f("ix__product_toppings__product_id"),
                      table_name="product_toppings", schema=schema)
        op.drop_table("product_toppings", schema=schema)

    op.drop_column("products", "visible_en_qr", schema=schema)
    op.drop_column("products", "is_topping", schema=schema)
