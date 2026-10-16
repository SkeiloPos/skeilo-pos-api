from decimal import Decimal

from sqlalchemy import CheckConstraint, Numeric, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.models import Base, UUIDPrimaryKeyMixin

# spec 121 (FR-009): catálogo por defecto de denominaciones del peso colombiano,
# de mayor a menor. Es la misma lista que siembra la revisión de Alembic
# `121_denomination_breakdown` (allí va escrita como literal a propósito: una
# migración no debe importar código de aplicación que luego cambie).
DEFAULT_COP_DENOMINATIONS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("100000"), "billete"),
    (Decimal("50000"), "billete"),
    (Decimal("20000"), "billete"),
    (Decimal("10000"), "billete"),
    (Decimal("5000"), "billete"),
    (Decimal("2000"), "billete"),
    (Decimal("1000"), "moneda"),
    (Decimal("500"), "moneda"),
    (Decimal("200"), "moneda"),
    (Decimal("100"), "moneda"),
    (Decimal("50"), "moneda"),
)


class DenominationCatalog(UUIDPrimaryKeyMixin, Base):
    """Catálogo de denominaciones permitidas en el arqueo de cierre, por tenant
    (spec 121, FR-009/FR-012). Solo lectura en esta spec: la administración del
    catálogo queda para una spec futura. No se llama `cash_denominations` para
    no confundirlo con `cash_count_denominations` (las líneas snapshot del
    conteo de cada turno)."""

    __tablename__ = "denomination_catalog"

    # Valor nominal. Único por tenant.
    denomination: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # 'billete' | 'moneda'.
    kind: Mapped[str] = mapped_column(String(10), nullable=False)

    __table_args__ = (
        # Sin nombre explícito: la convención de `app/core/models.py` produce
        # `uq__denomination_catalog__denomination`, el mismo de la migración.
        UniqueConstraint("denomination"),
        CheckConstraint("denomination > 0", name="ck_denomination_value_positive"),
        CheckConstraint("kind IN ('billete','moneda')", name="ck_denomination_kind"),
        {"schema": "tenant"},
    )


def seed_default_denominations(db: Session) -> None:
    """Siembra el catálogo COP por defecto. Idempotente: inserta solo los
    valores que falten. No hace `commit`: queda dentro de la transacción de quien
    la llama (p. ej. `tenant_create`)."""
    existentes = set(db.execute(select(DenominationCatalog.denomination)).scalars().all())
    for valor, tipo in DEFAULT_COP_DENOMINATIONS:
        if valor in existentes:
            continue
        db.add(DenominationCatalog(denomination=valor, kind=tipo))
    db.flush()
