import sys
import types

from pages import detail

from immo_agent.core import llm
from immo_agent.core.config import get_settings
from immo_agent.sources.extract import read_listing


def fake_anthropic(text, stop="end_turn", seen=None):
    block = types.SimpleNamespace(type="text", text=text)
    resp = types.SimpleNamespace(stop_reason=stop, content=[block])

    def create(**kw):
        if seen is not None:
            seen.append(kw)
        return resp

    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return types.SimpleNamespace(Anthropic=lambda: client)


def test_without_llm_rules_read_the_page():
    assert llm.complete_json("x", {}) is None and llm.complete_text("x") is None
    raw = read_listing(detail(), "https://a.test/huis/1")
    assert raw.price == 349000 and raw.municipality == "Antwerpen"


def test_llm_is_primary_and_validated(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "anthropic")
    seen = []
    answer = (
        '{"title": null, "price": 339000, "transaction": "sale", "status": "under_option", "property_type": "house",'
        ' "street": "Lange Nieuwstraat 12", "postal_code": "2000", "municipality": "Antwerpen", "surface": 118,'
        ' "bedrooms": 99999, "epc_label": "Z", "epc_score": null, "flood_p": "A", "flood_g": null}'
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(answer, seen=seen))
    raw = read_listing(detail(), "https://a.test/huis/1")
    assert (raw.price, raw.status, raw.surface) == (339000, "under_option", 118), "LLM values win"
    assert raw.bedrooms == 3 and raw.epc_label == "C", "invalid LLM values are ignored, rule values stay"
    assert raw.photos == ["https://cdn.agency.test/p1.jpg"], "photos still come from the rules"
    assert "Woning te koop" in seen[0]["messages"][0]["content"]
    assert seen[0]["output_config"]["format"]["schema"] is llm.FIELDS_SCHEMA


def test_llm_failure_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "anthropic")
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic("", stop="refusal"))
    assert read_listing(detail(), "https://a.test/huis/1").price == 349000
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic("not json"))
    assert read_listing(detail(), "https://a.test/huis/1").price == 349000, "errors never break the pipeline"
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic("Beste makelaar"))
    assert llm.complete_text("hi") == "Beste makelaar"
