from sqlalchemy import create_engine
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.main import Artwork, Base, Episode, ImportIssue, Show, app, catalogue, db, report


def seeded_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
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


def test_editor_can_dismiss_seed_import_issue():
    session = seeded_session()
    issue = ImportIssue(message="duplicate seed variant")
    session.add(issue); session.commit()
    app.dependency_overrides[db] = lambda: session
    try:
        response = TestClient(app).delete(f"/admin/import-issues/{issue.id}", headers={"Authorization":"Bearer editor-demo"})
        assert response.status_code == 200
        assert report(session)["blocked"] is False
    finally:
        app.dependency_overrides.clear()
        session.close()
