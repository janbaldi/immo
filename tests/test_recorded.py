"""Checks every recorded real page in tests/fixtures/recorded/ (create them with `immo record-fixture URL NAME`).

Each recorded detail page must yield the critical fields. Skipped when nothing has been recorded yet.
"""

import re
from pathlib import Path

import pytest

from immo_agent.sources.extract import extract_listing

RECORDED = sorted((Path(__file__).parent / "fixtures" / "recorded").glob("*.html"))


@pytest.mark.skipif(not RECORDED, reason="no recorded pages yet: run `immo record-fixture <url> <name>`")
@pytest.mark.parametrize("path", RECORDED, ids=lambda p: p.stem)
def test_recorded_detail_page(path):
    html = path.read_text(encoding="utf-8")
    url = re.search(r"<!-- recorded from (\S+)", html).group(1)
    raw = extract_listing(html, url).clean()
    if path.stem.startswith("list-"):
        pytest.skip("listing pages are covered by the connector tests")
    assert raw.missing_critical() == [], f"{path.name}: missing {raw.missing_critical()}"
