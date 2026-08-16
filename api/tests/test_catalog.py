from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import Artwork, Base, Episode, Show, catalogue, report


def seeded_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    show = Show(title="Test Show", slug="test-show", section="series", categories=["stories"], synopsis="A test", status="published")
    session.add(show); session.flush()
    for kind, dimensions in {"poster": (600, 900), "banner": (1280, 720), "thumbnail": (640, 360)}.items():
        session.add(Artwork(show_id=show.id, kind=kind, path=f"{kind}.png", width=dimensions[0], height=dimensions[1], size_bytes=10))
    session.add_all([
        Episode(external_id="one", show_id=show.id, season_number=1, episode_number=1, title="One", duration_seconds=60, language="en", content_group="s01e01", status="published"),
        Episode(external_id="two", show_id=show.id, season_number=1, episode_number=1, title="One", duration_seconds=65, language="hi", content_group="s01e01", status="published"),
    ])
    session.commit()
    return session


def test_catalogue_groups_language_variants():
    session = seeded_session()
    episode = catalogue(session)["sections"][0]["shows"][0]["episodes"][0]
    assert episode["languages"] == ["en", "hi"]


def test_validation_is_clear_for_valid_catalogue():
    assert report(seeded_session())["blocked"] is False
