import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.main import app
from src.api.dependencies import get_db_session

from src.infrastructure.models import Base

DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5433/payment_recovery"

@pytest_asyncio.fixture
async def client():
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def override_get_db_session():
        async with Session() as session:
            yield session

    app.dependency_overrides[get_db_session] = override_get_db_session
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db_session, None)
    await engine.dispose()

@pytest.mark.asyncio
async def test_health_check_endpoint(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_health_check_db_failure():
    async def failing_get_db_session():
        class FailingSession:
            async def execute(self, stmt):
                raise Exception("Connection refused")
        yield FailingSession()

    app.dependency_overrides[get_db_session] = failing_get_db_session
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.get("/health")
    app.dependency_overrides.pop(get_db_session, None)
    assert response.status_code == 503
    assert "Database unreachable" in response.json()["detail"]

@pytest.mark.asyncio
async def test_executive_summary_endpoint(client):
    response = await client.get("/api/v1/summary")
    assert response.status_code == 200
    data = response.json()
    assert "total_gmv_minor_units" in data
    assert "total_protected_gmv_minor_units" in data
    assert "active_degradation_episodes" in data
    assert "total_successful_interventions" in data
    assert "overall_success_rate" in data
    assert "total_auths" in data
    assert "total_captures" in data

@pytest.mark.asyncio
async def test_episodes_endpoint(client):
    response = await client.get("/api/v1/episodes?limit=10")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

@pytest.mark.asyncio
async def test_episodes_endpoint_invalid_limit(client):
    response = await client.get("/api/v1/episodes?limit=0")
    assert response.status_code == 422  # Validation error (ge=1)

    response = client_resp = await client.get("/api/v1/episodes?limit=2000")
    assert client_resp.status_code == 422  # Validation error (le=1000)

@pytest.mark.asyncio
async def test_attributions_endpoint(client):
    response = await client.get("/api/v1/attributions?limit=25")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

@pytest.mark.asyncio
async def test_attributions_endpoint_invalid_limit(client):
    response = await client.get("/api/v1/attributions?limit=0")
    assert response.status_code == 422

@pytest.mark.asyncio
async def test_rca_not_found(client):
    response = await client.get("/api/v1/rca/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["detail"] == "RCA evaluation not found"

@pytest.mark.asyncio
async def test_timeline_not_found(client):
    response = await client.get("/api/v1/timeline/non_existent_payment_id")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]
