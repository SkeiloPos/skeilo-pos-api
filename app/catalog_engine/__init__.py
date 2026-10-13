"""Motor de catálogo: precio de línea + plan de consumo de inventario.

Extracción de `app/api/v1/catalog/line_pricing.py` y `consumption_plan.py`
(specs/014-extraccion-motor-catalogo). Reexporta los catorce símbolos públicos
del contrato — ver contracts/module-api.md Contrato A (spec 089 añade `compute_unit_price`,
`compute_addons_total` y `line_total`).
"""
from app.catalog_engine.core import (
    ChosenOption as ChosenOption,
    ConsumptionLine as ConsumptionLine,
    _exige_maximo as _exige_maximo,
    compute_addons_total as compute_addons_total,
    compute_line_price as compute_line_price,
    compute_unit_price as compute_unit_price,
    line_total as line_total,
)
from app.catalog_engine.consumption import (
    ensure_lines_consume_inventory as ensure_lines_consume_inventory,
    group_discounts as group_discounts,
    load_recipe as load_recipe,
    load_variant_groups as load_variant_groups,
    plan_line_consumption as plan_line_consumption,
    required_consumption as required_consumption,
    sold_out_detail as sold_out_detail,
    variant_label as variant_label,
)
from app.catalog_engine.pricing import (
    check_availability as check_availability,
    ensure_product_available as ensure_product_available,
    ensure_products_available as ensure_products_available,
    grupos_que_descuentan as grupos_que_descuentan,
    load_valid_options as load_valid_options,
    mark_per_line as mark_per_line,
    validate_option_selection as validate_option_selection,
)
