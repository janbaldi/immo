"""Personal-data guard. Listings keep property fields only; contact details are removed at parse time."""

import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Belgian phone numbers: +32 / 0032 / 0 followed by 8-9 digits with optional separators.
PHONE_RE = re.compile(r"(?:\+|00)32[\s./-]?\(?0?\)?(?:[\s./-]?\d){8,9}|\b0\d(?:[\s./-]?\d){7,8}\b")
# "Contact: Jan Peeters" / "Contactpersoon Jan Peeters" style lines name a person: drop the name part.
CONTACT_RE = re.compile(r"(?i)\b(contact(?:persoon|eer)?|verkoper|eigenaar|makelaar)\s*:?\s+[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)*")


def scrub(text: str | None) -> str | None:
    if text is None:
        return None
    text = EMAIL_RE.sub("[verwijderd]", text)
    text = PHONE_RE.sub("[verwijderd]", text)
    return CONTACT_RE.sub(lambda m: m.group(1) + " [verwijderd]", text)


def find_pii(text: str | None) -> list[str]:
    """Used by the database scan in the acceptance report."""
    if not text:
        return []
    return EMAIL_RE.findall(text) + PHONE_RE.findall(text)
