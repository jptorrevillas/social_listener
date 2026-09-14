import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from social_listener.models import Base, Subreddit, Term


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, autoflush=False, future=True)
    with maker() as s:
        yield s


@pytest.fixture()
def seeded(session):
    session.add(
        Term(
            label="Brand",
            match_type="boolean",
            pattern='("northbridge college" OR nbc)',
            negative_pattern="northbridge road",
        )
    )
    session.add(Term(label="Billing", match_type="phrase", pattern="tuition"))
    session.add(Subreddit(name="testsub"))
    session.flush()
    return session
