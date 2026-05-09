from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test.db"
    settings = Settings(database_url=f"sqlite:///{db_path}", api_token="test-token", default_languages="en")
    app = create_app(settings)

    with TestClient(app) as test_client:
        yield test_client
