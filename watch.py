import os
import re
import sys
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import requests
import feedparser


# ----------------------------
# Config
# ----------------------------
TZ = ZoneInfo("Europe/Paris")

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "").strip()
NOTION_VERSION = os.getenv("NOTION_VERSION", "2025-09-03").strip()

PER_DAY = int(os.getenv("PER_DAY", "1"))  # 1 veille/jour
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "168"))  # on considère les 7 derniers jours
LOOKBACK_DAYS_DEDUP = int(os.getenv("LOOKBACK_DAYS_DEDUP", "180"))  # anti-doublons (url déjà postée)

# Noms de propriétés (tu peux adapter si besoin)
PROP_TITLE = os.getenv("PROP_TITLE", "Titre")
PROP_URL = os.getenv("PROP_URL", "URL")
PROP_SOURCE = os.getenv("PROP_SOURCE", "Source")
PROP_DATE = os.getenv("PROP_DATE", "Date")
PROP_SUMMARY = os.getenv("PROP_SUMMARY", "Résumé")  # optionnel
PROP_SCORE = os.getenv("PROP_SCORE", "Score")        # optionnel
PROP_TOPIC = os.getenv("PROP_TOPIC", "Sujet")        # optionnel
PROP_AUTO = os.getenv("PROP_AUTO", "Automatique")    # optionnel (checkbox)

TOPIC_LABEL = os.getenv("TOPIC_LABEL", "IA & emploi (métiers, automatisation)")
USER_AGENT = os.getenv(
    "USER_AGENT",
    "notion-ai-watch/1.0 (+https://github.com/; contact: you@example.com)"
)

FEEDS_RAW = os.getenv("FEEDS", "").strip()


# ----------------------------
# Helpers : texte, urls, scoring
# ----------------------------
def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)          # strip HTML
    s = re.sub(r"&nbsp;|&#160;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_url(url: str) -> str:
    """Supprime fragments + paramètres de tracking (utm, etc.) pour mieux dédupliquer."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    q = parse_qsl(parts.query, keep_blank_values=True)

    drop_prefixes = ("utm_",)
    drop_exact = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    q2 = [(k, v) for (k, v) in q if not (k.lower() in drop_exact or k.lower().startswith(drop_prefixes))]

    new_query = urlencode(q2, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, ""))  # drop fragment


AI_KEYWORDS = [
    "intelligence artificielle", "ia", "générative", "generative", "llm",
    "chatgpt", "copilot", "mistral", "openai", "deep learning", "machine learning",
    "agent", "agents", "automatisation", "automatiser"
]

WORK_KEYWORDS = [
    "emploi", "travail", "métier", "metier", "métiers", "metiers",
    "poste", "carrière", "carriere", "recrutement", "rh", "ressources humaines",
    "productivité", "productivite", "salaire", "chômage", "chomage",
    "reconversion", "formation", "compétence", "competence", "compétences", "competences",
    "automatisation", "automatiser", "remplacer", "remplacement", "substitution",
    "licenciement", "plan social"
]

NEGATIVE_HINTS = [
    # on évite des articles “IA” mais sans lien métier/emploi (ex: pure gaming, hardware...)
    "test", "smartphone", "console", "playstation", "xbox", "airfryer", "promo", "bon plan"
]


def contains_any(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(k in t for k in keywords)


def relevance_and_score(title: str, summary: str, source: str) -> tuple[bool, int]:
    t = f"{title}\n{summary}".lower()

    # pertinence minimale : au moins 1 mot IA ET 1 mot travail/emploi
    if not contains_any(t, AI_KEYWORDS):
        return False, 0
    if not contains_any(t, WORK_KEYWORDS):
        return False, 0

    # filtre “bruit” léger
    if contains_any(t, NEGATIVE_HINTS) and not ("emploi" in t or "travail" in t or "métier" in t or "metier" in t):
        return False, 0

    score = 0

    # bonus si titres contiennent les termes “durs”
    hard = ["remplacer", "remplacement", "substitution", "automatisation", "licenciement", "plan social"]
    for k in hard:
        if k in t:
            score += 6

    # mots travail/emploi
    for k in WORK_KEYWORDS:
        if k in t:
            score += 2

    # mots IA
    for k in AI_KEYWORDS:
        if k in t:
            score += 1

    # petites pondérations par source
    src = (source or "").lower()
    if "vie-publique" in src:
        score += 4
    if "journaldunet" in src or "jdn" in src:
        score += 2

    return True, score


def parse_entry_datetime(entry) -> datetime | None:
    # feedparser fournit souvent published_parsed / updated_parsed
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            return datetime(*tm[:6], tzinfo=timezone.utc)
    return None


# ----------------------------
# Notion client (compatible data_sources 2025-09-03)
# ----------------------------
class NotionClient:
    def __init__(self, token: str, notion_version: str):
        self.base = "https://api.notion.com/v1"
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": notion_version,
            "Content-Type": "application/json",
        })

    def _req(self, method: str, path: str, json=None, params=None):
        url = f"{self.base}{path}"
        r = self.s.request(method, url, json=json, params=params, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Notion API {r.status_code}: {r.text}")
        return r.json()

    def retrieve_database(self, db_id: str) -> dict:
        return self._req("GET", f"/databases/{db_id}")

    def retrieve_data_source(self, ds_id: str) -> dict:
        return self._req("GET", f"/data_sources/{ds_id}")

    def query_parent(self, mode: str, parent_id: str, body: dict) -> dict:
        if mode == "data_source":
            return self._req("POST", f"/data_sources/{parent_id}/query", json=body)
        return self._req("POST", f"/databases/{parent_id}/query", json=body)

    def create_page(self, parent_obj: dict, properties: dict) -> dict:
        payload = {"parent": parent_obj, "properties": properties}
        return self._req("POST", "/pages", json=payload)


def pick_prop(schema_props: dict, wanted_name: str, wanted_type: str) -> str | None:
    # 1) si l'utilisateur a le bon nom + type
    if wanted_name in schema_props and schema_props[wanted_name].get("type") == wanted_type:
        return wanted_name
    # 2) sinon, première propriété du bon type
    for name, obj in schema_props.items():
        if obj.get("type") == wanted_type:
            return name
    return None


def build_prop_value(prop_type: str, value):
    if prop_type == "title":
        return {"title": [{"type": "text", "text": {"content": str(value)[:2000]}}]}
    if prop_type == "rich_text":
        return {"rich_text": [{"type": "text", "text": {"content": str(value)[:2000]}}]}
    if prop_type == "url":
        return {"url": str(value)}
    if prop_type == "date":
        # value attendu: "YYYY-MM-DD" (date-only)
        return {"date": {"start": str(value)}}
    if prop_type == "select":
        return {"select": {"name": str(value)[:100]}}
    if prop_type == "multi_select":
        if not isinstance(value, list):
            value = [value]
        return {"multi_select": [{"name": str(v)[:100]} for v in value if v]}
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    if prop_type == "number":
        try:
            return {"number": float(value)}
        except Exception:
            return None
    return None


def extract_page_prop_value(page: dict, prop_name: str) -> str | None:
    p = page.get("properties", {}).get(prop_name)
    if not p:
        return None
    t = p.get("type")
    if t == "url":
        return p.get("url")
    if t == "rich_text":
        arr = p.get("rich_text", [])
        return "".join(x.get("plain_text", "") for x in arr) if arr else ""
    if t == "title":
        arr = p.get("title", [])
        return "".join(x.get("plain_text", "") for x in arr) if arr else ""
    return None


# ----------------------------
# RSS fetching
# ----------------------------
@dataclass
class Candidate:
    title: str
    url: str
    source: str
    summary: str
    published_utc: datetime
    score: int


def get_feed_urls() -> list[str]:
    if not FEEDS_RAW:
        return []
    # accepte séparateurs : \n, ;, ,
    parts = re.split(r"[\n;,]+", FEEDS_RAW)
    urls = [p.strip() for p in parts if p.strip()]
    return urls


def fetch_feed(session: requests.Session, url: str):
    r = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return feedparser.parse(r.content)


def candidates_from_feeds(feed_urls: list[str]) -> list[Candidate]:
    sess = requests.Session()
    now_utc = datetime.now(timezone.utc)
    min_dt = now_utc - timedelta(hours=MAX_AGE_HOURS)

    out: list[Candidate] = []
    seen_norm: set[str] = set()

    for f in feed_urls:
        try:
            parsed = fetch_feed(sess, f)
        except Exception as e:
            print(f"[WARN] Feed fetch failed: {f} -> {e}", file=sys.stderr)
            continue

        feed_title = clean_text(getattr(parsed.feed, "title", "")) or urlsplit(f).netloc

        for entry in parsed.entries[:100]:
            title = clean_text(entry.get("title", ""))[:300]
            link = entry.get("link", "") or ""
            link = link.strip()
            if not title or not link:
                continue

            summary = clean_text(entry.get("summary", "") or entry.get("description", ""))[:1500]

            dt = parse_entry_datetime(entry)
            if dt is None:
                # fallback : on garde mais on le considère récent
                dt = now_utc

            if dt < min_dt:
                continue

            src = clean_text(entry.get("source", {}).get("title", "")) if isinstance(entry.get("source"), dict) else ""
            source = src or feed_title

            ok, score = relevance_and_score(title, summary, source)
            if not ok:
                continue

            nurl = normalize_url(link)
            if nurl in seen_norm:
                continue
            seen_norm.add(nurl)

            out.append(Candidate(
                title=title,
                url=nurl,  # on stocke la version normalisée pour éviter les doublons
                source=source,
                summary=summary,
                published_utc=dt,
                score=score
            ))

    # tri : score desc, puis plus récent
    out.sort(key=lambda c: (c.score, c.published_utc), reverse=True)
    return out


# ----------------------------
# Main logic
# ----------------------------
def main():
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        raise SystemExit("Missing NOTION_TOKEN or NOTION_DATABASE_ID")
    feed_urls = get_feed_urls()
    if not feed_urls:
        raise SystemExit("Missing FEEDS (secret)")

    notion = NotionClient(NOTION_TOKEN, NOTION_VERSION)

    db = notion.retrieve_database(NOTION_DATABASE_ID)

    # mode data_source si présent
    ds_list = db.get("data_sources") or []
    if ds_list:
        mode = "data_source"
        data_source_id = ds_list[0].get("id")
        parent_id = data_source_id
        ds = notion.retrieve_data_source(data_source_id)
        schema_props = ds.get("properties", {})
        parent_obj = {"type": "data_source_id", "data_source_id": data_source_id}
    else:
        mode = "database"
        parent_id = NOTION_DATABASE_ID
        schema_props = db.get("properties", {})
        parent_obj = {"database_id": NOTION_DATABASE_ID}

    # auto-detect properties
    title_prop = pick_prop(schema_props, PROP_TITLE, "title")
    url_prop = pick_prop(schema_props, PROP_URL, "url") or PROP_URL  # si absent, on tentera quand même
    date_prop = pick_prop(schema_props, PROP_DATE, "date")
    source_prop = PROP_SOURCE if PROP_SOURCE in schema_props else None
    summary_prop = PROP_SUMMARY if PROP_SUMMARY in schema_props else None
    score_prop = PROP_SCORE if PROP_SCORE in schema_props else None
    topic_prop = PROP_TOPIC if PROP_TOPIC in schema_props else None
    auto_prop = PROP_AUTO if PROP_AUTO in schema_props and schema_props[PROP_AUTO].get("type") == "checkbox" else None

    if not title_prop:
        raise SystemExit("Impossible de trouver une propriété Title (type 'title') dans ta base Notion.")
    if not date_prop:
        raise SystemExit("Impossible de trouver une propriété Date (type 'date') dans ta base Notion.")

    # date du jour (Paris)
    now_paris = datetime.now(TZ)
    today = now_paris.date()
    tomorrow = (now_paris + timedelta(days=1)).date()

    # Mode forcé : on crée toujours PER_DAY veille(s), quoi qu'il arrive
    remaining = PER_DAY
    print(f"[INFO] Mode forcé : création de {remaining} veille(s) aujourd'hui ({today})")

    # set d’URLs existantes (lookback) pour éviter les doublons d’un jour à l’autre
    lookback_start = (today - timedelta(days=LOOKBACK_DAYS_DEDUP)).isoformat()
    body_old = {
        "page_size": 100,
        "filter": {"property": date_prop, "date": {"on_or_after": lookback_start}}
    }

    existing_urls: set[str] = set()
    cursor = None
    while True:
        b = dict(body_old)
        if cursor:
            b["start_cursor"] = cursor
        resp = notion.query_parent(mode, parent_id, b)
        for page in resp.get("results", []):
            u = extract_page_prop_value(page, url_prop)
            if u:
                existing_urls.add(normalize_url(u))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")

    # récupère candidats RSS
    cands = candidates_from_feeds(feed_urls)

    # crée jusqu’à remaining
    created = 0
    for c in cands:
        if created >= remaining:
            break
        if c.url in existing_urls:
            continue

        props = {}

        # title
        props[title_prop] = build_prop_value("title", c.title)

        # url (si la propriété existe et type url)
        if url_prop in schema_props and schema_props[url_prop].get("type") == "url":
            props[url_prop] = build_prop_value("url", c.url)

        # date : date-only du jour (ton “1 veille par jour” devient “2 veilles par jour”)
        props[date_prop] = build_prop_value("date", today.isoformat())

        # source
        if source_prop:
            st = schema_props[source_prop].get("type")
            pv = build_prop_value(st, c.source)
            if pv:
                props[source_prop] = pv

        # résumé (optionnel)
        if summary_prop:
            st = schema_props[summary_prop].get("type")
            pv = build_prop_value(st, c.summary)
            if pv:
                props[summary_prop] = pv

        # score (optionnel)
        if score_prop:
            pv = build_prop_value("number", c.score)
            if pv:
                props[score_prop] = pv

        # sujet (optionnel)
        if topic_prop:
            st = schema_props[topic_prop].get("type")
            pv = build_prop_value(st, TOPIC_LABEL)
            if pv:
                props[topic_prop] = pv

        # automatique checkbox (optionnel)
        if auto_prop:
            props[auto_prop] = build_prop_value("checkbox", True)

        notion.create_page(parent_obj, props)
        print(f"[OK] Created: {c.title} ({c.source}) score={c.score}")
        existing_urls.add(c.url)
        created += 1

        # petit sleep pour éviter rate-limit
        time.sleep(0.2)

    print(f"[DONE] created={created}")


if __name__ == "__main__":
    main()
