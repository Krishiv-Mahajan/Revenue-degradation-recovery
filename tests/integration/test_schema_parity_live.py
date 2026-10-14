"""
Live model/migration parity: the test database is built by `alembic upgrade head`
(tests/conftest.py), and Alembic's autogenerate comparison must find no differences
between that schema and the SQLAlchemy models.
"""
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from src.infrastructure.models import Base


@pytest.mark.asyncio
async def test_migrated_schema_matches_models(db_engine):
    def _diff(sync_conn):
        context = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    async with db_engine.connect() as conn:
        diff = await conn.run_sync(_diff)

    assert diff == []
