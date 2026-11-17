import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import site
from pages import detail
from sqlalchemy import select

from immo_agent import cli, report, worker
from immo_agent.agents import analyst
from immo_agent.core.db import utcnow
from immo_agent.core.models import Alert, DedupReview, Listing, ListingEvent, Property, Source, SourceRun, User, Valuation


def seed(db, days_live=15, minutes=6):
    now = utcnow()
    db.add(Source(id=1, name="A", kind="zabun", base_url="https://a.test", list_url="https://a.test/x"))
    db.add(SourceRun(source_id=1, started_at=now - timedelta(days=days_live)))
    if db.get(User, 1) is None:
        db.add(User(id=1, email="u@example.com", password_hash="x"))
    p = Property(id=1, first_seen_anywhere_at=now - timedelta(hours=1), price=300000)
    db.add(p)
    db.add(Listing(id=1, property_id=1, source_id=1, url="https://a.test/x/1", description="Mooi huis"))
    db.add(
        Alert(
            user_id=1,
            profile_id=1,
            property_id=1,
            listing_id=1,
            event_type="new",
            channel="email",
            token="t",
            sent_at=p.first_seen_anywhere_at + timedelta(minutes=minutes),
        )
    )
    db.add(Valuation(property_id=1, at=now - timedelta(days=3), asking_price=320000, fair_value=300000, low=280000, high=330000))
    db.add(ListingEvent(listing_id=1, property_id=1, type="price_drop", old_value="320000", new_value="300000", at=now))
    db.add_all([DedupReview(property_id=1, correct=True)])
    db.commit()


def test_report_flags_missing_acceptance_items(db):
    seed(db)
    text, passed = report.build(db, 14)
    assert "| Median time to alert < 15 min | median 6.0 min (n=1) | PASS |" in text
    assert "| Zero personal-data fields stored | 0 findings | PASS |" in text
    assert "1 alerts, 0 without source URL" in text
    assert "Dedup precision" in text and "100.0% on 1 labels | FAIL" in text, "200 labels are required"
    assert "100% (n=1)" in text  # later price inside the earlier interval
    assert passed is False


def test_report_detects_pii_and_slow_alerts(db):
    seed(db, minutes=40)
    db.get(Listing, 1).description = "Bel 0477 12 34 56"
    db.commit()
    text, _ = report.build(db, 14, dead_links=["https://a.test/x/1"])
    assert "| median 40.0 min (n=1) | FAIL |" in text
    assert "1 findings" in text and "1 dead links" in text


def test_cli_admin_and_report(db, tmp_path, monkeypatch, capsys):
    db.add(User(email="founder@example.com", password_hash="x"))
    db.commit()
    cli.main(["admin", "create", "Founder@example.com"])
    db.expire_all()
    assert db.scalars(select(User)).one().is_admin
    with pytest.raises(SystemExit):
        cli.main(["admin", "create", "nobody@example.com"])
    seed(db)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_:
        cli.main(["report"])
    assert exit_.value.code == 1 and list(Path("reports").glob("acceptance-*.md"))
    assert "acceptance report" in capsys.readouterr().out


def test_cli_init_runs_migrations(tmp_path, monkeypatch, capsys):
    from immo_agent.core import db as dbm
    from immo_agent.core.config import get_settings

    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    monkeypatch.setattr(get_settings(), "database_url", url)
    dbm.configure(url)
    cli.main(["init"])
    assert "configured sources loaded" in capsys.readouterr().out


def test_cli_run_and_check_live(db, monkeypatch, capsys):
    from immo_agent.agents import scout
    from immo_agent.sources import http

    pages = {"https://www.biddit.be/robots.txt": (404, ""), "https://immo.notaris.be/robots.txt": (404, "")}
    real = http.Fetcher
    monkeypatch.setattr(scout, "Fetcher", lambda: real(transport=site(pages)))
    with pytest.raises(SystemExit) as exit_:
        cli.main(["check-live", "--min-ok", "1"])
    out = capsys.readouterr().out
    assert exit_.value.code == 1 and "Biddit" in out and "0/2 sources OK" in out and "Telegram alert to admin: FAIL" in out
    cli.main(["run"])
    assert "source" in capsys.readouterr().out


def test_cli_parse_and_record(monkeypatch, capsys, tmp_path):
    from immo_agent.sources import http

    real = http.Fetcher
    monkeypatch.setattr(http, "Fetcher", lambda: real(transport=site({"https://a.test/huis/1": detail()})))
    cli.main(["parse", "https://a.test/huis/1"])
    assert '"price": 349000' in capsys.readouterr().out
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    cli.main(["record-fixture", "https://a.test/huis/1", "sample"])
    assert (tmp_path / "tests/fixtures/recorded/sample.html").read_text().startswith("<!-- recorded from https://a.test/huis/1")


def test_worker_jobs_and_telegram_link(db, monkeypatch):
    s = worker.build()
    assert {j.id for j in s.get_jobs()} >= {
        "scout",
        "matcher",
        "analyst",
        "ops_qa",
        "retrain",
        "weekly",
        "discover",
        "full_crawl",
    }
    db.add(User(email="u@example.com", password_hash="x", telegram_link_code="abc123"))
    db.commit()
    monkeypatch.setattr(
        "immo_agent.notify.telegram_updates",
        lambda offset: [
            {"update_id": 7, "message": {"text": "/start abc123", "chat": {"id": 555}}},
            {"update_id": 8, "message": {"text": "hello", "chat": {"id": 1}}},
        ],
    )
    monkeypatch.setattr("immo_agent.notify.telegram_send", lambda *a: True)
    assert worker.link_telegram() == 1
    db.expire_all()
    u = db.scalars(select(User)).one()
    assert u.telegram_chat_id == "555" and u.telegram_link_code is None
    boom = worker.safe(lambda: 1 / 0)
    boom()  # logged, not raised


def test_analyst_jobs(db, monkeypatch):
    db.add(Property(street="Kerkstraat 1", municipality="Antwerpen", price=1))
    db.commit()
    assert analyst.enrich_new() == 1
    db.expire_all()
    assert db.scalars(select(Property)).one().enriched_at is not None
    analyst.nightly()
    assert db.scalars(select(Valuation)).all()
    assert asyncio.run(asyncio.sleep(0, result=True))
