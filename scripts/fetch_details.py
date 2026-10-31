#!/usr/bin/env python3
"""Incremental, polite fetch of the Caixa property detail pages.

Reads the mirrored CSV list, fetches only ids that are new, changed (price or sale modality) or stale,
at a low rate and with caps per run, and writes details_<UF>.json keyed by property id. The previous
JSON is the cache. Any answer that is not a normal detail page (HTTP error, challenge page) stops the run
and keeps what was collected so far: this script never tries to get past bot protection.
"""
import csv
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://venda-imoveis.caixa.gov.br"
USER_AGENT = "caixa-csv-mirror/1.0 (github.com/vinirib/caixa-csv-mirror)"
MARKER = 'id="dadosImovel"'
UNAVAILABLE = "não está mais disponível"

LABEL = re.compile(r"<span>([^<:=]+?):\s*<strong>(.*?)</strong></span>", re.S)
AREA = re.compile(r"<span>(Área [^<=]+?) = <strong>([^<]+)</strong>")
AUCTION_DATE = re.compile(r"<span>(?:<i[^>]*></i>\s*)?(Data d[^<]+?-\s*\d{2}/\d{2}/\d{4}[^<]*)</span>")
DOC = re.compile(r"ExibeDoc\('([^']+)'\)")
TRAILING_DOTS = re.compile(r"(?:\s*\.)+$")
TAG = re.compile(r"<[^>]+>")
SPACES = re.compile(r"\s+")
SCRIPT = re.compile(r"<script.*?</script>|<style.*?</style>", re.S | re.I)
BULLET = re.compile(r"&nbsp;([^<]+?)<br", re.S)
OCCUPIED = re.compile(r"\bocupad[oa]s?\b", re.I)
VACANT = re.compile(r"\bdesocupad[oa]s?\b", re.I)
NOT_OCCUPIED = re.compile(r"\bn[ãa]o (?:est[áa]|se encontra) ocupad[oa]\b", re.I)


def clean(text):
    return SPACES.sub(" ", html.unescape(TAG.sub(" ", text))).strip()


def first(pattern, text, flags=re.S):
    match = re.search(pattern, text, flags)
    return clean(match.group(1)) if match else None


def occupancy(visible_text):
    """DESOCUPADO or OCUPADO only when the page says so in words. Caixa usually does not."""
    if NOT_OCCUPIED.search(visible_text) or VACANT.search(visible_text):
        return "DESOCUPADO"
    if OCCUPIED.search(visible_text):
        return "OCUPADO"
    return None


def parse_detail(page):
    """Returns the details of one property, or None when the page is not a detail page."""
    if MARKER not in page:
        return None
    if UNAVAILABLE in page:
        return {"unavailable": True}
    labels = {clean(k): clean(v) for k, v in LABEL.findall(page)}
    areas = {clean(k): clean(v) for k, v in AREA.findall(page)}
    docs = DOC.findall(page)
    registration = next((d for d in docs if "/matricula/" in d), None)
    edital = next((d for d in docs if "/matricula/" not in d), None)
    payment_block = re.search(r"FORMAS DE PAGAMENTO ACEITAS:(.*?)(?:REGRAS PARA PAGAMENTO DAS DESPESAS|</p>)", page, re.S)
    expense_block = re.search(r"REGRAS PARA PAGAMENTO DAS DESPESAS \(caso existam\):(.*?)</p>", page, re.S)
    payments = [clean(m) for m in BULLET.findall(payment_block.group(1))] if payment_block else []
    if payment_block and not payments:
        text = clean(payment_block.group(1))
        payments = [text] if text else []
    description = first(r"<strong>Descrição:</strong><br>(.*?)</p>", page)
    if description:
        description = TRAILING_DOTS.sub(".", description)
    details = {
        "propertyNumber": labels.get("Número do imóvel"),
        "registration": labels.get("Matrícula(s)"),
        "county": labels.get("Comarca"),
        "registryOffice": labels.get("Ofício"),
        "municipalRegistration": labels.get("Inscrição imobiliária"),
        "auctionAnnotation": labels.get("Averbação dos leilões negativos"),
        "bedrooms": labels.get("Quartos"),
        "parking": labels.get("Garagem"),
        "totalArea": areas.get("Área total"),
        "privateArea": areas.get("Área privativa"),
        "landArea": areas.get("Área do terreno"),
        "address": first(r"<strong>Endereço:</strong><br>(.*?)</p>", page),
        "description": description if description and description != "." else None,
        "payments": payments,
        "expenses": [clean(m) for m in BULLET.findall(expense_block.group(1))] if expense_block else [],
        "registrationUrl": registration,
        "editalUrl": edital,
        "edital": first(r"<span>Edital:&nbsp;([^<]+)</span>", page),
        "editalPublishedAt": first(r"Edital publicado em: ([^)<]+)\)", page),
        "itemNumber": first(r"<span>Número do item: (\d+)</span>", page),
        "auctioneer": first(r"<span>Leiloeiro\(a\): ([^<]+)</span>", page),
        "auctioneerSite": first(r'SiteLeiloeiro\("([^"]+)"\)', page),
        "auctionDates": [clean(m) for m in AUCTION_DATE.findall(page)],
        "occupancy": occupancy(clean(SCRIPT.sub(" ", page))),
    }
    return {key: value for key, value in details.items() if value not in (None, "", [])}


def read_list(path):
    """Maps property id to (price, modality) from the windows-1252 CSV."""
    with open(path, encoding="cp1252", newline="") as source:
        rows = csv.reader(source, delimiter=";")
        header = None
        for row in rows:
            if row and "Link de acesso" in row:
                header = [h.strip() for h in row]
                break
        if header is None:
            raise SystemExit("CSV has no header row")
        id_col = 0
        price_col = header.index("Preço")
        modality_col = header.index("Modalidade de venda")
        listing = {}
        for row in rows:
            if len(row) <= modality_col or not row[id_col].strip().isdigit():
                continue
            listing[row[id_col].strip()] = (row[price_col].strip(), row[modality_col].strip())
        return listing


def plan(listing, cache, now, max_fetch, stale_after):
    """New ids first, then changed ones, then the stalest, up to max_fetch."""
    new = [i for i in listing if i not in cache]
    changed = [i for i in listing if i in cache
               and (cache[i].get("price"), cache[i].get("modality")) != listing[i]]
    limit = (now - stale_after).isoformat()
    stale = sorted((i for i in listing if i in cache and i not in changed
                    and cache[i].get("fetchedAt", "") < limit),
                   key=lambda i: cache[i].get("fetchedAt", ""))
    return (new + changed + stale)[:max_fetch]


def fetch(property_id):
    request = urllib.request.Request(
        f"{BASE}/sistema/detalhe-imovel.asp?hdnimovel={property_id}",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect means the bot manager answered, so it is treated as a stop signal."""

    def redirect_request(self, *args, **kwargs):
        return None


def main(argv):
    csv_path, cache_path, out_path = argv[1:4]
    state = os.path.basename(csv_path).removeprefix("Lista_imoveis_").removesuffix(".csv")
    max_fetch = int(os.environ.get("MAX_FETCH", "1500"))
    max_seconds = int(os.environ.get("MAX_SECONDS", "1800"))
    delay = float(os.environ.get("REQUEST_DELAY", "1.0"))
    stale_after = timedelta(days=int(os.environ.get("STALE_DAYS", "7")))

    listing = read_list(csv_path)
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as source:
            cache = json.load(source).get("properties", {})
    now = datetime.now(timezone.utc)
    todo = plan(listing, cache, now, max_fetch, stale_after)
    print(f"{state}: {len(listing)} listed, {len(cache)} cached, {len(todo)} to fetch")

    started = time.monotonic()
    fetched = failures = 0
    for property_id in todo:
        if time.monotonic() - started > max_seconds:
            print("time budget reached, stopping")
            break
        try:
            page = fetch(property_id)
        except urllib.error.HTTPError as error:
            print(f"HTTP {error.code} for {property_id}, stopping (not retrying around it)")
            break
        except (urllib.error.URLError, TimeoutError) as error:
            failures += 1
            print(f"request failed for {property_id}: {error}")
            if failures >= 3:
                print("3 failures, stopping")
                break
            time.sleep(delay)
            continue
        details = parse_detail(page)
        if details is None:
            print(f"unexpected page for {property_id}, stopping")
            break
        failures = 0
        price, modality = listing[property_id]
        details.update(price=price, modality=modality, fetchedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        cache[property_id] = details
        fetched += 1
        time.sleep(delay)

    kept = {i: d for i, d in cache.items() if i in listing}
    with open(out_path, "w", encoding="utf-8") as target:
        json.dump({"state": state, "generatedAt": now.isoformat(timespec="seconds"), "properties": kept},
                  target, ensure_ascii=False, separators=(",", ":"))
    with_occupancy = sum(1 for d in kept.values() if d.get("occupancy"))
    print(f"fetched {fetched}, stored {len(kept)} of {len(listing)}, with occupancy text: {with_occupancy}")


if __name__ == "__main__":
    main(sys.argv)
