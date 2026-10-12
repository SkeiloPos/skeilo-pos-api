"""merge idempotencia pedidos (111) e inventory import jobs (109)

Revision ID: 8c2efeb4956b
Revises: 30f5e7b0ef5f, 42dc063d8185
Create Date: 2026-10-11 14:30:34.053313

"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c2efeb4956b'
down_revision: Union[str, Sequence[str], None] = ('30f5e7b0ef5f', '42dc063d8185')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

@for_each_tenant_schema
def upgrade(schema: str) -> None:
    """Upgrade schema."""
    preparer = sa.sql.compiler.IdentifierPreparer(op.get_bind().dialect)
    schema_quoted = preparer.format_schema(schema)
    
    pass


@for_each_tenant_schema
def downgrade(schema: str) -> None:
    """Downgrade schema."""
    preparer = sa.sql.compiler.IdentifierPreparer(op.get_bind().dialect)
    schema_quoted = preparer.format_schema(schema)
    
    pass
