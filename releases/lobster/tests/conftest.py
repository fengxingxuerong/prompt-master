import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
import pytest
from app import app

@pytest.fixture()
def client():
    return TestClient(app)
