"""
eBay Gold- und Diamantschmuck-Alarm
------------------------------------
Durchsucht eBay ueber die offizielle Browse-API nach:
  - Gold / Goldschmuck ab 18kt (750er)
  - Edelstein-/Diamantschmuck: Zertifikat (GIA/HRD/IGI) ODER
    (Material Gold >=18kt oder Platin UND Haendler >=90% positiv UND EU-Standort)

Neue Treffer werden in state/latest_matches.json geschrieben. Diese Datei
liegt im (öffentlichen) Repo und ist damit unter einer festen
raw.githubusercontent.com-URL erreichbar - das Auktions-Logbuch-Tool liest
diese URL direkt aus dem Browser und fügt neue Treffer automatisch als
Zeilen hinzu. Kein E-Mail-Versand, keine manuelle Eingabe.

Bereits gesehene Artikel werden zusätzlich in state/seen_items.json
gemerkt, damit sie nicht doppelt in latest_matches.json auftauchen.

Alle Einstellungen stehen im Abschnitt "KONFIGURATION" unten - dort anpassen,
kein Grund die Logik weiter unten anzufassen.
"""

import os
import re
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# KONFIGURATION - hier anpassen
# ---------------------------------------------------------------------------

# Eine oder mehrere Suchanfragen. Jede wird einzeln an eBay geschickt.
SEARCH_QUERIES = [
    "18k gold ring",
    "750 gold ring",
    "18k gold kette",
    "gold armband 750",
    "diamant ring GIA",
    "diamant ring HRD",
    "diamant ring IGI",
    "platin diamant ring",
]

# eBay-Marktplatz: EBAY_DE (Deutschland), EBAY_NL (Niederlande), EBAY_AT,
# EBAY_GB, EBAY_US ... mehrere werden nacheinander abgefragt.
MARKETPLACES = ["EBAY_DE", "EBAY_NL"]

# Maximaler Preis pro Artikel in Euro (None = kein Limit)
MAX_PRICE_EUR = None

# Mindest-Haendlerbewertung in Prozent, wenn KEIN Zertifikat im Titel/Text steht
MIN_SELLER_FEEDBACK_PCT = 90.0

# Laender, die als "EU" gelten (ISO-2-Codes wie von eBay geliefert)
EU_COUNTRIES = {
    "DE", "FR", "IT", "ES", "NL", "BE", "AT", "PT", "IE", "FI", "SE", "DK",
    "PL", "CZ", "SK", "HU", "RO", "BG", "GR", "HR", "SI", "LT", "LV", "EE",
    "LU", "MT", "CY",
}

MAX_RESULTS_PER_QUERY = 25

# ---------------------------------------------------------------------------
# Ab hier normalerweise nichts aendern
# ---------------------------------------------------------------------------

EBAY_CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
EBAY_CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]

STATE_FILE = Path("state/seen_items.json")
MATCHES_FILE = Path("state/latest_matches.json")
MAX_MATCHES_IN_FEED = 200

CERT_PATTERN = re.compile(r"\b(GIA|HRD|IGI)\b", re.IGNORECASE)
GOLD_18K_PATTERN = re.compile(r"\b(18\s?k|18\s?kt|750)\b", re.IGNORECASE)
PLATINUM_PATTERN = re.compile(r"\bplatin(um)?\b", re.IGNORECASE)
GEMSTONE_PATTERN = re.compile(
    r"\b(diamant|diamond|edelstein|gemstone|saphir|sapphire|rubin|ruby|smaragd|emerald)\b",
    re.IGNORECASE,
)


def get_access_token():
    import base64
    creds = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}"
    b64creds = base64.b64encode(creds.encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {b64creds}",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def search_ebay(token, query, marketplace):
    resp = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
        },
        params={
            "q": query,
            "limit": str(MAX_RESULTS_PER_QUERY),
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  Warnung: eBay-Antwort {resp.status_code} fuer '{query}' auf {marketplace}")
        return []
    return resp.json().get("itemSummaries", [])


def item_matches_rules(item):
    text = (item.get("title", "") or "")
    if item.get("shortDescription"):
        text += " " + item["shortDescription"]

    has_cert = bool(CERT_PATTERN.search(text))
    is_18k_gold = bool(GOLD_18K_PATTERN.search(text))
    is_platinum = bool(PLATINUM_PATTERN.search(text))
    material_ok = is_18k_gold or is_platinum
    mentions_gemstone = bool(GEMSTONE_PATTERN.search(text))

    if not material_ok:
        return False, "kein 18kt-Gold/Platin erkannt"

    if not mentions_gemstone:
        # reines Goldstueck, kein Edelstein -> Material-Filter reicht aus
        return True, "Gold/Platin ohne Edelstein"

    if has_cert:
        return True, "Edelstein mit Zertifikat (GIA/HRD/IGI)"

    seller = item.get("seller", {})
    feedback_pct = float(seller.get("feedbackPercentage", 0) or 0)
    country = (item.get("itemLocation", {}) or {}).get("country", "")

    if feedback_pct >= MIN_SELLER_FEEDBACK_PCT and country in EU_COUNTRIES:
        return True, f"kein Zertifikat, aber Haendler {feedback_pct}% aus {country}"

    return False, f"kein Zertifikat, Haendler {feedback_pct}% aus {country} (Kriterien nicht erfuellt)"


def price_ok(item):
    if MAX_PRICE_EUR is None:
        return True
    price = item.get("price", {})
    try:
        value = float(price.get("value", 0))
    except (TypeError, ValueError):
        return True
    currency = price.get("currency", "EUR")
    if currency != "EUR":
        return True
    return value <= MAX_PRICE_EUR


def load_seen():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen_ids):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen_ids)))


def load_matches_feed():
    if MATCHES_FILE.exists():
        return json.loads(MATCHES_FILE.read_text())
    return []


def save_matches_feed(existing, new_matches):
    now = datetime.now(timezone.utc).isoformat()
    for item, reason in new_matches:
        price = item.get("price", {})
        existing.append({
            "itemId": item.get("itemId"),
            "objekt": item.get("title"),
            "gebot": price.get("value"),
            "waehrung": price.get("currency"),
            "link": item.get("itemWebUrl"),
            "haendlertyp": "gewerblich" if item.get("seller", {}).get("feedbackScore", 0) else "",
            "haendlerbewertung": item.get("seller", {}).get("feedbackPercentage"),
            "grund": reason,
            "gefundenAm": now,
        })
    # neueste zuerst, auf Maximalgroesse kappen
    existing.sort(key=lambda m: m["gefundenAm"], reverse=True)
    existing = existing[:MAX_MATCHES_IN_FEED]
    MATCHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    MATCHES_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


def main():
    token = get_access_token()
    seen = load_seen()
    new_matches = []

    for marketplace in MARKETPLACES:
        for query in SEARCH_QUERIES:
            print(f"Suche '{query}' auf {marketplace} ...")
            items = search_ebay(token, query, marketplace)
            time.sleep(0.3)  # eBay-Rate-Limit schonen

            for item in items:
                item_id = item.get("itemId")
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)

                if not price_ok(item):
                    continue

                ok, reason = item_matches_rules(item)
                if ok:
                    new_matches.append((item, reason))

    if new_matches:
        print(f"{len(new_matches)} neue Treffer gefunden, aktualisiere Feed ...")
        feed = load_matches_feed()
        save_matches_feed(feed, new_matches)
    else:
        print("Keine neuen Treffer in diesem Lauf.")

    save_seen(seen)


if __name__ == "__main__":
    main()
