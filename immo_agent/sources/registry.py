from sqlalchemy import select
from sqlalchemy.orm import Session

from immo_agent.core.models import Source
from immo_agent.sources.agency import GenericConnector, OmnicasaConnector, WhiseConnector, ZabunConnector
from immo_agent.sources.base import Connector
from immo_agent.sources.http import Fetcher
from immo_agent.sources.jsonapi import BidditConnector, NotaryConnector
from immo_agent.sources.portals import PortalConnector

CONNECTORS: dict[str, type[Connector]] = {
    "zabun": ZabunConnector,
    "whise": WhiseConnector,
    "omnicasa": OmnicasaConnector,
    "generic": GenericConnector,
    "biddit": BidditConnector,
    "notary": NotaryConnector,
    "immoweb": PortalConnector,
    "zimmo": PortalConnector,
}


def connector_for(source: Source, fetcher: Fetcher) -> Connector:
    return CONNECTORS[source.kind](source, fetcher)


def sync_sources(db: Session) -> int:
    """Upsert config/sources.yaml into the sources table. The admin on/off switch is preserved."""
    from immo_agent.core.config import get_settings, load_yaml

    count = 0
    for cfg in (load_yaml("sources.yaml") or {}).get("sources", []):
        src = db.scalars(select(Source).where(Source.base_url == cfg["base_url"])).first()
        if src is None:
            src = Source(base_url=cfg["base_url"], enabled=cfg.get("enabled", True))
            db.add(src)
        src.name, src.kind, src.list_url = cfg["name"], cfg["kind"], cfg["list_url"]
        src.province = cfg.get("province", get_settings().province)
        src.poll_minutes = cfg.get("poll_minutes", 10)
        src.is_portal = cfg.get("is_portal", False)
        src.use_browser = cfg.get("use_browser", False)
        src.options = cfg.get("options")
        src.review_status = "active"
        count += 1
    db.commit()
    return count
