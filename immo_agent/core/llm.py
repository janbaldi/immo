"""LLM page reader and writing help. One interface, provider chosen by LLM_PROVIDER.

When configured, the LLM is the primary reader of agency listing pages (read_page); the rule-based parser
supplies photos, publication time and coordinates, and is the full fallback when the LLM is off ("none") or
a call fails. Also drafts visit-request text.
"""

import json
import logging
from typing import Any

from immo_agent.core.config import get_settings

log = logging.getLogger(__name__)

STATUSES = ["for_sale", "under_option", "sold"]
TYPES = ["house", "apartment", "land", "garage", "commercial"]


def _nullable(kind: str, enum: list[str] | None = None) -> dict[str, Any]:
    return {"type": [kind, "null"], **({"enum": [*enum, None]} if enum else {})}


FIELDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": _nullable("string"),
        "price": _nullable("integer"),
        "transaction": _nullable("string", ["sale", "rent"]),
        "status": _nullable("string", STATUSES),
        "property_type": _nullable("string", TYPES),
        "street": _nullable("string"),
        "postal_code": _nullable("string"),
        "municipality": _nullable("string"),
        "surface": _nullable("number"),
        "bedrooms": _nullable("integer"),
        "epc_label": _nullable("string", ["A+", "A", "B", "C", "D", "E", "F"]),
        "epc_score": _nullable("number"),
        "flood_p": _nullable("string", ["A", "B", "C", "D"]),
        "flood_g": _nullable("string", ["A", "B", "C", "D"]),
    },
    "additionalProperties": False,
}
FIELDS_SCHEMA["required"] = list(FIELDS_SCHEMA["properties"])

PROMPT = """You read one Belgian real-estate listing page (mostly Dutch) and return the facts of the property.
Rules:
- price: the asking price in euro as a whole number (for auctions: the starting price); null if "prijs op aanvraag".
- transaction: "rent" for te huur / à louer, else "sale".
- status: "sold" for verkocht, "under_option" for in optie / onder voorbehoud / compromis, else "for_sale".
- street: street and house number (and box) of the property itself, if shown.
- surface: habitable/living area (bewoonbare oppervlakte) in m², not the plot size.
- epc_score in kWh/m² per year; flood_p / flood_g are the P-score and G-score.
- Use null when the page does not say. Never guess.
- Never return names, phone numbers or e-mail addresses of anyone.

Page text:
"""


def complete_json(prompt: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    s = get_settings()
    if s.llm_provider != "anthropic":
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=s.llm_model,
            max_tokens=2000,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return None
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)
    except Exception as e:  # noqa: BLE001 - the LLM is optional, never break the pipeline
        log.warning("LLM call failed: %s", e)
        return None


def complete_text(prompt: str) -> str | None:
    s = get_settings()
    if s.llm_provider != "anthropic":
        return None
    try:
        import anthropic

        resp = anthropic.Anthropic().messages.create(
            model=s.llm_model,
            max_tokens=1000,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        return next((b.text for b in resp.content if b.type == "text"), None)
    except Exception as e:  # noqa: BLE001
        log.warning("LLM call failed: %s", e)
        return None


def _valid(field: str, value: Any) -> bool:
    """Trust boundary: model output is checked before it can overwrite parsed values."""
    if value in (None, ""):
        return False
    checks = {
        "price": lambda v: isinstance(v, int) and 100 <= v <= 50_000_000,
        "postal_code": lambda v: isinstance(v, str) and v.isdigit() and len(v) == 4,
        "surface": lambda v: isinstance(v, int | float) and 5 <= v <= 100_000,
        "bedrooms": lambda v: isinstance(v, int) and 0 <= v <= 50,
        "epc_score": lambda v: isinstance(v, int | float) and 0 <= v <= 2000,
    }
    enums = {k: (FIELDS_SCHEMA["properties"][k].get("enum") or []) for k in FIELDS_SCHEMA["properties"]}
    if enums[field]:
        return value in enums[field]
    return checks.get(field, lambda v: isinstance(v, str) and len(v) <= 300)(value)


def read_page(raw: Any, html: str) -> bool:
    """Primary reader: LLM values replace rule-based values. Returns False when the LLM was not used."""
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    from immo_agent.sources.extract import visible_text

    text = visible_text(HTMLParser(html))[:12000]  # about 3-4k tokens per page
    data = complete_json(PROMPT + text, FIELDS_SCHEMA) if text else None
    if not data:
        return False
    for field, value in data.items():
        if field in FIELDS_SCHEMA["properties"] and _valid(field, value):
            setattr(raw, field, value)
    return True
