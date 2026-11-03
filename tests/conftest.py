from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO / "config" / "brands.toml"


@pytest.fixture
def real_config_path() -> Path:
    return REAL_CONFIG


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test.db"
