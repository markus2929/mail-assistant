import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from mailassistant.db.models import Base


@pytest.fixture(scope="session")
def engine():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to an EMPTY throwaway database to run DB tests")
    eng = create_engine(url)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine):
    with engine.begin() as c:
        c.exec_driver_sql("TRUNCATE jobs RESTART IDENTITY")
    s = Session(engine)
    yield s
    s.rollback()
    s.close()
