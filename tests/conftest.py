import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="module")
def _isolated_db():
    """Every test module gets its own database, so files never contaminate each other."""
    d = tempfile.mkdtemp()
    os.environ["SF_DB"] = os.path.join(d, "t.db")
    os.environ["SF_BACKUP"] = os.path.join(d, "bk")
    yield
