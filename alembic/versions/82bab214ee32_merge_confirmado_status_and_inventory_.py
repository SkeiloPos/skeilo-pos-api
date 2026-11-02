"""merge confirmado status and inventory unify heads

Revision ID: 82bab214ee32
Revises: c23ca53d6387, 30cd5f4a8fd6
Create Date: 2026-11-02 14:20:08.330551

"""
from typing import Sequence, Union
from app.scripts.tenant import for_each_tenant_schema
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '82bab214ee32'
down_revision: Union[str, Sequence[str], None] = ('c23ca53d6387', '30cd5f4a8fd6')
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
