import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from social_listener.models import Base, Channel, WatchTerm


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
        WatchTerm(
            label="Brand",
            match_type="boolean",
            pattern='("northbridge college" OR nbc)',
            negative_pattern="northbridge road",
        )
    )
    session.add(WatchTerm(label="Billing", match_type="phrase", pattern="tuition"))
    session.add(
        Channel(
            youtube_id="UC_owned",
            title="Our Channel",
            uploads_playlist_id="UU_owned",
            is_owned=True,
        )
    )
    session.add(
        Channel(
            youtube_id="UC_peer",
            title="Peer Channel",
            uploads_playlist_id="UU_peer",
            is_owned=False,
        )
    )
    session.flush()
    return session
