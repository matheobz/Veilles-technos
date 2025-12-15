import os
import re
from datetime import datetime, timezone

import feedparser
import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

# Notion a évolué vers "data sources" (version 2025-09-03).
# On met cette version par défaut pour être compatible avec le modèle actuel. :contentReference[oaicite:7]{index=7}
NOTION_VERSION = os.getenv("NOTION_VERSION", "2025-09-03")

# Noms des colonnes dans Notion
PROP_TITLE = os.getenv("PROP_TITLE", "Titre")
PROP_URL = os.getenv("PROP_URL", "URL")
PROP_SOURCE = os.getenv("PROP_SOURCE", "Source")
PROP_DATE = os.getenv("PROP_DATE", "Date")

FEEDS = [u.strip() for u in os.getenv("FEEDS", "").splitlines() if u.strip()]
if not FEEDS:
    FEEDS = ["https://rss.arxiv.org/rss/cs.AI"]

session = requests.Session()
session.headers.update(
    {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
)

def get_data_source_id(database_id: str) -> str:
    # Retrieve database -> renvoie la liste des data_sources (modèle Notion 2025). :contentReference[oaicite:8]{index=8}
    r = session.get(f"https://api.notion.com/v1/databases/{database_id}")
    r.raise_for_status()
    ds_list = (r.json() or {}).get("data_sources") or []
    if not ds_list:
        raise RuntimeError("Aucune data_source trouvée dans cette base Notion.")
    return ds_list[0]["id"]

DATA_SOURCE_ID = os.getenv("NOTION_DATA_SOURCE_ID") or get_data_source_id(NOTION_DATABASE_ID)

def source_name_from_url(feed_url: str) -> str:
    if "arxiv.org" in feed_url:
        return "arXiv"
    if "huggingface.co" in feed_url:
        return "Hugging Face"
    return re.sub(r"^https?://", "", feed_url).split("/")[0]

def item_date_iso(entry) -> str | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if not t:
        return None
    dt = datetime(*t[:6], tzinfo=timezone.utc)
    return dt.isoformat()

def already_exists(url: str) -> bool:
    # Query a data source + filter sur la propriété URL :contentReference[oaicite:9]{index=9}
    payload = {"filter": {"property": PROP_URL, "url": {"equals": url}}, "page_size": 1}
    r = session.post(f"https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}/query", json=payload)
    r.raise_for_status()
    return len((r.json() or {}).get("results", [])) > 0

def create_page(title: str, url: str, source: str, published_iso: str | None) -> None:
    props = {
        PROP_TITLE: {"title": [{"text": {"content": title[:2000]}}]},
        PROP_URL: {"url": url},
        PROP_SOURCE: {"select": {"name": source}},
    }
    if published_iso:
        props[PROP_DATE] = {"date": {"start": published_iso}}

    # Create page sous un parent data_source :contentReference[oaicite:10]{index=10}
    payload = {
        "parent": {"type": "data_source_id", "data_source_id": DATA_SOURCE_ID},
        "properties": props,
    }
    r = session.post("https://api.notion.com/v1/pages", json=payload)
    r.raise_for_status()

def main():
    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url)
        source = source_name_from_url(feed_url)
        for entry in (feed.entries or [])[:50]:
            title = (entry.get("title") or "(sans titre)").strip()
            url = (entry.get("link") or "").strip()
            if not url:
                continue
            if already_exists(url):
                continue
            create_page(title, url, source, item_date_iso(entry))

if __name__ == "__main__":
    main()
