"""One Property per real home, many Listings (one per source).

Match on normalised address; otherwise on postal code/municipality + type + surface ±3 m² + price ±2%,
confirmed by a photo perceptual hash when both photos are available.
"""

import io
import re
import unicodedata

from PIL import Image
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from immo_agent.core.models import Property
from immo_agent.sources.extract import RawListing

PHASH_MAX_DISTANCE = 10


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def normalise_address(street: str | None, postal_code: str | None) -> str | None:
    """'Lange Nieuwstraat 12 bus 3' + '2000' -> 'lange nieuwstraat 12 2000'. Needs a house number.

    The box number is dropped (sites often omit it); units in one building are told apart by surface instead.
    """
    if not street or not postal_code or not re.search(r"\d", street):
        return None
    s = _plain(street)
    s = re.sub(r"\b(bus|bte|boite|box)\s*\w+", " ", s)
    s = re.sub(r"\bstr\b", "straat", s)
    return " ".join(s.split() + [postal_code.strip()])


def same_area(prop: Property, raw: RawListing) -> bool:
    if prop.postal_code and raw.postal_code:
        return prop.postal_code == raw.postal_code
    return bool(prop.municipality and raw.municipality and _plain(prop.municipality) == _plain(raw.municipality))


def fuzzy_match(prop: Property, raw: RawListing) -> bool:
    if prop.property_type != raw.property_type or not same_area(prop, raw):
        return False
    if not (prop.surface and raw.surface and prop.price and raw.price):
        return False
    return abs(prop.surface - raw.surface) <= 3 and abs(prop.price - raw.price) <= 0.02 * prop.price


def find_property(db: Session, raw: RawListing) -> tuple[Property | None, str]:
    """Returns (property, method) with method in {'address', 'fuzzy', 'none'}."""
    addr = normalise_address(raw.street, raw.postal_code)
    if addr:
        for prop in db.scalars(select(Property).where(Property.address_norm == addr)):
            same_type = prop.property_type in (None, raw.property_type) or raw.property_type is None
            same_size = not (prop.surface and raw.surface) or abs(prop.surface - raw.surface) <= max(3, 0.05 * prop.surface)
            if same_type and same_size:
                return prop, "address"
    if raw.property_type and raw.surface and raw.price:
        q = select(Property).where(Property.property_type == raw.property_type, Property.status != "removed")
        q = q.where(or_(Property.postal_code == raw.postal_code, Property.municipality == raw.municipality))
        for prop in db.scalars(q.limit(200)):
            if fuzzy_match(prop, raw) and (not addr or not prop.address_norm):
                return prop, "fuzzy"
    return None, "none"


def photo_hash(image_bytes: bytes) -> str:
    """64-bit difference hash (dHash) as 16 hex chars."""
    img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((9, 8))
    px = img.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")
