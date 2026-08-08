"""
eBay Gold- und Diamantschmuck-Alarm
------------------------------------
Durchsucht eBay ueber die offizielle Browse-API nach FUENF Kategorien
(mindestens eine muss zutreffen):
  1. Anlagegold: Barren/Muenzen aus Feingold (999/999.9), unabhaengig vom Gewicht
  2. Goldschmuck: mindestens 18kt (750er) UND mindestens 10g Gesamtgewicht
  3. Markenschmuck: erkennbare Marke (Cartier, Tiffany, Bulgari, Van Cleef & Arpels,
     Chopard, Boucheron), unabhaengig von Material/Gewicht
  4. Lose Diamanten: mindestens 1,0 ct UND Zertifikat (GIA/HRD/IGI)
  5. (Alte Ausnahmeregel) Edelstein-/Diamantschmuck in Gold >=18kt oder Platin
     OHNE Zertifikat, aber nur wenn Haendler >=90% positiv UND EU-Standort

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
    # Anlagegold
    "goldbarren 999",
    "gold bar 999.9",
    "krugerrand gold",
    "maple leaf gold coin",
    "wiener philharmoniker gold",
    # Goldschmuck (Gewicht wird zusaetzlich im Text gefiltert)
    "18k gold kette gramm",
    "750 gold armband gramm",
    "18k gold halskette massiv",
    # Markenschmuck
    "Cartier ring gold",
    "Cartier armband",
    "Tiffany gold ring",
    "Bulgari ring gold",
    "Van Cleef Arpels ring",
    "Chopard ring gold",
    "Boucheron ring",
    # Lose Diamanten ab 1 ct
    "loose diamond 1 carat GIA",
    "loser diamant 1 ct GIA",
    "unset diamond 1ct HRD",
    "diamant lose 1 karat IGI",
    # Alte Ausnahmeregel: gefasste Edelsteine, ggf. ohne Zertifikat
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

# Mindestgewicht in Gramm fuer Goldschmuck (Kategorie 2)
MIN_GOLD_JEWELRY_WEIGHT_G = 10.0

# Mindestkaratgewicht fuer lose Diamanten (Kategorie 4)
MIN_LOOSE_DIAMOND_CT = 1.0

# Markennamen, die als "Markenschmuck" zaehlen (Kategorie 3) - hier ergaenzen
BRAND_NAMES = [
    "cartier", "tiffany", "bulgari", "bvlgari", "van cleef", "chopard", "boucheron",
]

# --- Alte Ausnahmeregel (Kategorie 5) ---
# Mindest-Haendlerbewertung in Prozent, wenn KEIN Zertifikat im Titel/Text steht
MIN_SELLER_FEEDBACK_PCT = 90.0

# Laender, die als "EU" gelten (ISO-2-Codes wie von eBay geliefert)
EU_COUNTRIES = {
    "DE", "FR", "IT", "ES", "NL", "BE", "AT", "PT", "IE", "FI", "SE", "DK",
    "PL", "CZ", "SK", "HU", "RO", "BG", "GR", "HR", "SI", "LT", "LV", "EE",
    "LU", "MT", "CY",
}

MAX_RESULTS_PER_QUERY = 25

# Aktueller Feingold-Spotpreis in Euro/Gramm - VON ZEIT ZU ZEIT MANUELL AKTUALISIEREN
# (z.B. von goldpreis.de oder finanzen.net abschauen)
GOLD_SPOT_EUR_PER_GRAM = 121.20

# Sicherheitspuffer fuer die automatische Kaufen/Beobachten/Nicht-kaufen-Bewertung
# bei reinen Gold-Funden (Kategorie 1+2, kein Edelstein)
MATERIAL_BUFFER_PCT = 10.0

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

INVESTMENT_GOLD_PATTERN = re.compile(
    r"\b(goldbarren|gold\s?bar|krugerrand|kr[üu]gerrand|maple\s?leaf|"
    r"philharmoniker|gold\s?coin|goldm[üu]nze|feingold\s?999)\b",
    re.IGNORECASE,
)

LOOSE_DIAMOND_PATTERN = re.compile(
    r"\b(loose\s?diamond|lose[rn]?\s?diamant|unset\s?diamond|diamant\s?lose)\b",
    re.IGNORECASE,
)

BRAND_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(b) for b in BRAND_NAMES) + r")\b",
    re.IGNORECASE,
)

# z.B. "6.22 grams", "10 g", "12,5 gramm"
WEIGHT_PATTERN = re.compile(r"(\d+[.,]?\d*)\s?(g|gr|gram|grams|gramm)\b", re.IGNORECASE)

# z.B. "1.20 ct", "1 carat", "1,5 karat" - bewusst getrennt von "kt" (Feingehalt)
CARAT_PATTERN = re.compile(r"(\d+[.,]?\d*)\s?(ct\.?|carat|karat)\b", re.IGNORECASE)


def extract_max_number(pattern, text):
    """Findet alle Treffer eines Zahlen-Musters und gibt die groesste Zahl zurueck."""
    matches = pattern.findall(text)
    numbers = []
    for m in matches:
        raw = m[0] if isinstance(m, tuple) else m
        try:
            numbers.append(float(raw.replace(",", ".")))
        except ValueError:
            continue
    return max(numbers) if numbers else None


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


def gold_verdict(bid, materialwert):
    """Leitet aus Gebot und Materialwert automatisch Kaufen/Beobachten/Nicht kaufen ab."""
    if bid is None:
        return "Prüfen:kein Angebotspreis erkannt"
    puffer_grenze = materialwert * (1 - MATERIAL_BUFFER_PCT / 100)
    if bid <= puffer_grenze:
        return f"Kaufen:{bid}€ unter Materialwert {round(materialwert)}€ (Puffer {MATERIAL_BUFFER_PCT:.0f}%)"
    if bid <= materialwert:
        return f"Beobachten:{bid}€ nahe Materialwert {round(materialwert)}€, aber kein Puffer mehr"
    return f"Nicht kaufen:{bid}€ über Materialwert {round(materialwert)}€"


def item_matches_rules(item):
    text = (item.get("title", "") or "")
    if item.get("shortDescription"):
        text += " " + item["shortDescription"]
    bid = None
    price = item.get("price", {})
    try:
        if price.get("currency") == "EUR":
            bid = float(price.get("value"))
    except (TypeError, ValueError):
        bid = None

    # --- Kategorie 1: Anlagegold (Barren/Muenzen) ---
    if INVESTMENT_GOLD_PATTERN.search(text):
        weight = extract_max_number(WEIGHT_PATTERN, text)
        extra = None
        if weight is not None:
            materialwert = weight * 0.999 * GOLD_SPOT_EUR_PER_GRAM
            extra = {
                "materialwert": round(materialwert, 2),
                "bewertung": gold_verdict(bid, materialwert),
                "material_text": f"{weight}g Feingold 999.9",
            }
        return True, "Anlagegold (Barren/Münze)", extra

    # --- Kategorie 3: Markenschmuck ---
    brand_match = BRAND_PATTERN.search(text)
    if brand_match:
        return True, f"Markenschmuck ({brand_match.group(0).title()})", None

    has_cert = bool(CERT_PATTERN.search(text))
    is_18k_gold = bool(GOLD_18K_PATTERN.search(text))
    is_platinum = bool(PLATINUM_PATTERN.search(text))
    material_ok = is_18k_gold or is_platinum
    mentions_gemstone = bool(GEMSTONE_PATTERN.search(text))
    is_loose = bool(LOOSE_DIAMOND_PATTERN.search(text))

    # --- Kategorie 4: Lose Diamanten ab 1 ct mit Zertifikat ---
    if is_loose and has_cert:
        carat = extract_max_number(CARAT_PATTERN, text)
        if carat is not None and carat >= MIN_LOOSE_DIAMOND_CT:
            return True, f"loser Diamant {carat} ct mit Zertifikat", None
        if carat is None:
            return False, "loser zertifizierter Diamant, aber Karatzahl nicht erkannt", None
        return False, f"loser Diamant nur {carat} ct (< {MIN_LOOSE_DIAMOND_CT} ct)", None

    # --- Kategorie 2: Goldschmuck >=18kt UND >=10g ---
    if material_ok and not mentions_gemstone:
        weight = extract_max_number(WEIGHT_PATTERN, text)
        if weight is not None and weight >= MIN_GOLD_JEWELRY_WEIGHT_G:
            extra = None
            if is_18k_gold:
                materialwert = weight * 0.75 * GOLD_SPOT_EUR_PER_GRAM
                extra = {
                    "materialwert": round(materialwert, 2),
                    "bewertung": gold_verdict(bid, materialwert),
                    "material_text": f"{weight}g 18kt Gold (kein Edelstein)",
                }
            return True, f"Goldschmuck {weight}g (>= {MIN_GOLD_JEWELRY_WEIGHT_G}g)", extra
        if weight is None:
            return False, "Gold/Platin ohne erkennbares Gewicht im Text", None
        return False, f"Goldschmuck nur {weight}g (< {MIN_GOLD_JEWELRY_WEIGHT_G}g)", None

    # --- Kategorie 5: alte Ausnahmeregel (gefasste Steine ohne Zertifikat) ---
    if material_ok and mentions_gemstone:
        if has_cert:
            return True, "Edelstein mit Zertifikat (GIA/HRD/IGI)", None

        seller = item.get("seller", {})
        feedback_pct = float(seller.get("feedbackPercentage", 0) or 0)
        country = (item.get("itemLocation", {}) or {}).get("country", "")

        if feedback_pct >= MIN_SELLER_FEEDBACK_PCT and country in EU_COUNTRIES:
            return True, f"kein Zertifikat, aber Händler {feedback_pct}% aus {country}", None

        return False, f"kein Zertifikat, Händler {feedback_pct}% aus {country} (Kriterien nicht erfüllt)", None

    return False, "keine der fünf Kategorien erfüllt", None


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
    for item, reason, extra in new_matches:
        price = item.get("price", {})
        entry = {
            "itemId": item.get("itemId"),
            "objekt": item.get("title"),
            "gebot": price.get("value"),
            "waehrung": price.get("currency"),
            "link": item.get("itemWebUrl"),
            "haendlertyp": "gewerblich" if item.get("seller", {}).get("feedbackScore", 0) else "",
            "haendlerbewertung": item.get("seller", {}).get("feedbackPercentage"),
            "grund": reason,
            "gefundenAm": now,
        }
        if extra:
            entry["materialwert"] = extra.get("materialwert")
            entry["bewertung"] = extra.get("bewertung")
            entry["material"] = extra.get("material_text")
        existing.append(entry)
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

                ok, reason, extra = item_matches_rules(item)
                if ok:
                    new_matches.append((item, reason, extra))

    if new_matches:
        print(f"{len(new_matches)} neue Treffer gefunden, aktualisiere Feed ...")
        feed = load_matches_feed()
        save_matches_feed(feed, new_matches)
    else:
        print("Keine neuen Treffer in diesem Lauf.")

    save_seen(seen)


if __name__ == "__main__":
    main()
