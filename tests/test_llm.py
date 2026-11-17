import sys
import types

from immo_agent.core import llm
from immo_agent.core.config import get_settings
from immo_agent.sources.extract import RawListing


def fake_anthropic(text, stop="end_turn"):
    block = types.SimpleNamespace(type="text", text=text)
    resp = types.SimpleNamespace(stop_reason=stop, content=[block])
    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **kw: resp))
    return types.SimpleNamespace(Anthropic=lambda: client)


def test_llm_off_by_default():
    assert llm.complete_json("x", {}) is None and llm.complete_text("x") is None


def test_fill_missing_only_fills_empty_fields(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "anthropic")
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic('{"price": 250000, "municipality": "Lier", "surface": null}'))
    raw = RawListing(url="u", municipality="Mortsel")
    llm.fill_missing(raw, "<html><body>Huis te koop</body></html>")
    assert (raw.price, raw.municipality, raw.surface) == (250000, "Mortsel", None)
    assert llm.complete_text("hi") == '{"price": 250000, "municipality": "Lier", "surface": null}'
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic("", stop="refusal"))
    assert llm.complete_json("x", {}) is None
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic("not json"))
    assert llm.complete_json("x", {}) is None, "errors never break the pipeline"
