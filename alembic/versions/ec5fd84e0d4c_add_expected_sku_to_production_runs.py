"""add expected_sku to production_runs

Revision ID: ec5fd84e0d4c
Revises: 137e813427da
Create Date: 2026-09-30 00:10:27.674602
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'ec5fd84e0d4c'
down_revision: str | Sequence[str] | None = '137e813427da'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Agregar la columna como nullable
    op.add_column('production_runs', sa.Column('expected_sku', sa.String(), nullable=True))

    # 2. Backfill de filas existentes.
    #    Sabemos que las corridas que ya existen fueron todas para BLI-AMOXI-500
    #    (las de tus pruebas manuales contra dev). Ajusta si tienes otros casos.
    op.execute("UPDATE production_runs SET expected_sku = 'BLI-AMOXI-500' WHERE expected_sku IS NULL")

    # 3. Ahora sí, aplicar la restricción NOT NULL
    op.alter_column('production_runs', 'expected_sku', nullable=False)

def downgrade() -> None:
    op.drop_column('production_runs', 'expected_sku')
