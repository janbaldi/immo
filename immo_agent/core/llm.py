"""Optional LLM help. One interface, provider chosen by LLM_PROVIDER; with "none" every call is a no-op.

Used only where it clearly helps: filling critical fields the rule-based parser could not find,
and drafting visit-request text.
"""

import json
import logging
from typing import Any

from immo_agent.core.config import get_settings

log = logging.getLogger(__name__)

FIELDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "price": {"type": ["integer", "null"]},
        "property_type": {"type": ["string", "null"], "enum": ["house", "apartment", "land", "garage", "commercial", None]},
        "postal_code": {"type": ["string", "null"]},
        "municipality": {"type": ["string", "null"]},
        "surface": {"type": ["number", "null"]},
        "bedrooms": {"type": ["integer", "null"]},
    },
    "required": ["price", "property_type", "postal_code", "municipality", "surface", "bedrooms"],
    "additionalProperties": False,
}


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


def fill_missing(raw: Any, html: str) -> None:
    """Fill only fields that are still empty. Contact details are not requested and not stored."""
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    from immo_agent.sources.extract import visible_text

    text = visible_text(HTMLParser(html))[:12000]
    data = complete_json(
        "Extract these fields of the property for sale described on this Belgian real-estate page. "
        "Use null when unknown. Do not include any names, phone numbers or e-mail addresses.\n\n" + text,
        FIELDS_SCHEMA,
    )
    for k, v in (data or {}).items():
        if v not in (None, "") and getattr(raw, k, None) in (None, ""):
            setattr(raw, k, v)
