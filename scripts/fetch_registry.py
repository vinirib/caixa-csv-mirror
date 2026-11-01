#!/usr/bin/env python3
"""Incremental, polite read of the Caixa property registration (matrícula) PDFs.

For each property in the mirrored CSV list, Caixa publishes the registration certificate at
`/editais/matricula/<UF>/<id>.pdf`. This script downloads it, runs OCR, splits the certificate into
its acts (R.n registros and AV.n averbações), classifies each one, and reports the liens (penhora,
indisponibilidade, hipoteca, usufruto, ação) that no later act has cancelled. It writes
registry_<UF>.json keyed by property id. The previous JSON is the cache.

It fetches only ids that are new, changed (price or sale modality) or stale, one request per second,
with a cap per run. Any answer that is not a PDF (HTTP error, bot-manager challenge) stops the run and
keeps what was collected: this script never tries to get past bot protection. The matrícula PDFs sit
behind the same Radware/ShieldSquare protection as the detail pages, so this does NOT run reliably from a
datacenter IP (GitHub runners get redirected at once). Run it locally, from a residential connection, now
and then; each run resumes from the cache.

Prerequisites: poppler (pdftoppm) and tesseract with the Portuguese language data (tesseract-lang).
    macOS:  brew install poppler tesseract tesseract-lang
    Debian: apt-get install poppler-utils tesseract-ocr tesseract-ocr-por

Usage:
    # varredura da lista inteira (novos/alterados/velhos primeiro)
    python3 scripts/fetch_registry.py <Lista_imoveis_UF.csv> <cache registry_UF.json> <out registry_UF.json>
    # sob demanda: checa só alguns imóveis e mescla no registry_<UF>.json
    python3 scripts/fetch_registry.py --uf SP --ids 1444421729441,1444408501866

Defaults are deliberately gentle so a run does not look like a burst and trip the bot manager: a small
batch, several seconds between requests, with jitter. Tune via environment: MAX_FETCH (default 40),
MAX_SECONDS (1800), REQUEST_DELAY (4.0, the floor of a jittered wait), STALE_DAYS (30).
"""
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://venda-imoveis.caixa.gov.br"
USER_AGENT = "caixa-csv-mirror/1.0 (github.com/vinirib/caixa-csv-mirror)"

# Cabeçalho de um ato: "R. 4-M. 24.726, em 23 de outubro de 2023." ou
# "AV. 6-M. 24.726, em 1º de abril de 2026.". O OCR às vezes perde o hífen ("R.3M.") ou troca a
# pontuação, então tudo entre o número e o "em <data>" é tolerante.
ACT_HEADER = re.compile(
    r"\b(R|AV)\.?\s*(\d+)\s*[-–]?\s*M\.?\s*,?\s*[\d.]+\s*,?\s*em\s+(.{4,40}?)\s*\.",
    re.IGNORECASE,
)

# Cada categoria: (nome, é ônus, padrões). 'é ônus' marca o que pesa contra o imóvel e precisa estar
# cancelado para o imóvel ser seguro.
CATEGORIES = [
    ("penhora",              True,  [r"penhora", r"arresto", r"sequestro"]),
    ("indisponibilidade",    True,  [r"indisponibilidade", r"\bcnib\b", r"ordem de bloqueio"]),
    ("hipoteca",             True,  [r"hipoteca"]),
    ("usufruto",             True,  [r"usufruto"]),
    ("acao_judicial",        True,  [r"\ba[çc][ãa]o\b", r"execu[çc][ãa]o",
                                     r"cita[çc][ãa]o", r"citat[óo]ri", r"protesto"]),
    ("servidao",             True,  [r"servid[ãa]o"]),
    ("alienacao_fiduciaria", False, [r"aliena[çc][ãa]o fiduci", r"fiduciariamente"]),
    ("cedula_credito",       False, [r"c[ée]dula de cr[ée]dito"]),
    ("consolidacao",         False, [r"consolida[çc][ãa]o da propriedade",
                                     r"fica consolidada a propriedade"]),
    ("cancelamento",         False, [r"cancel"]),
    ("compra_venda",         False, [r"compra e venda", r"da[çc][ãa]o em pagamento", r"adjudica"]),
    ("abertura",             False, [r"abertura de matr[íi]cula", r"regulariza[çc][ãa]o fundi"]),
]
ONUS_LABELS = {name for name, is_onus, _ in CATEGORIES if is_onus}

# "cancelar a AV. 5" / "cancelamento do R. 3" -> número do ato cancelado.
CANCELS_REF = re.compile(r"cancel\w*\s+(?:a|o|d[aeo])?\s*(?:AV|R)\.?\s*(\d+)", re.IGNORECASE)

MARGIN_NOISE = re.compile(r"(?m)^[\s|>«»ç9Q357EB()oul]{1,4}\s+")


def pause(delay):
    """Espera entre delay e delay*1.6, para não cair num ritmo robótico que o bot manager detecta."""
    time.sleep(random.uniform(delay, delay * 1.6))


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
        price_col = header.index("Preço")
        modality_col = header.index("Modalidade de venda")
        listing = {}
        for row in rows:
            if len(row) <= modality_col or not row[0].strip().isdigit():
                continue
            listing[row[0].strip()] = (row[price_col].strip(), row[modality_col].strip())
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect means the bot manager answered, so it is treated as a stop signal."""

    def redirect_request(self, *args, **kwargs):
        return None


def download_pdf(property_id, uf, dest):
    request = urllib.request.Request(
        f"{BASE}/editais/matricula/{uf}/{property_id}.pdf",
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf"})
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(request, timeout=30) as response:
        data = response.read()
    if data[:4] != b"%PDF":
        raise ValueError("resposta não é PDF")
    dest.write_bytes(data)


def ocr(pdf_path, workdir):
    """PDF -> texto, uma passada de OCR por página, concatenado na ordem."""
    subprocess.run(["pdftoppm", "-r", "200", "-png", str(pdf_path), str(workdir / "pg")],
                   check=True, capture_output=True)
    pages = sorted(workdir.glob("pg-*.png"))
    texts = []
    for page_png in pages:
        base = page_png.with_suffix("")
        subprocess.run(["tesseract", str(page_png), str(base), "-l", "por", "--psm", "6"],
                       check=True, capture_output=True)
        texts.append(base.with_suffix(".txt").read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(texts), len(pages)


def classify(body):
    low = body.lower()
    return [name for name, _is_onus, patterns in CATEGORIES
            if any(re.search(p, low) for p in patterns)]


def title_of(body):
    """Primeira frase em CAIXA ALTA do corpo, que costuma ser o tipo do ato."""
    m = re.search(r"([A-ZÀ-ÚÇ][A-ZÀ-ÚÇ \-]{6,60})\s*[-–]", body)
    return m.group(1).strip() if m else None


def parse(text):
    """Lista de atos a partir do texto OCR da certidão."""
    text = MARGIN_NOISE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    headers = list(ACT_HEADER.finditer(text))
    acts = []
    for i, h in enumerate(headers):
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end].strip()
        number = int(h.group(2))
        labels = classify(body)
        # Um ato de cancelamento extingue um ônus anterior, ele mesmo não é ônus, ainda que cite a
        # palavra ("baixada a hipoteca", "cancelo a penhora").
        is_onus = bool(ONUS_LABELS.intersection(labels)) and "cancelamento" not in labels
        acts.append({
            "ref": f"{h.group(1).upper()}.{number}",
            "number": number,
            "date": h.group(3).strip(),
            "title": title_of(body),
            "labels": labels,
            "cancels": [int(x) for x in CANCELS_REF.findall(body)],
            "isOnus": is_onus,
        })
    return acts


def assess(acts):
    """Ônus em aberto = ato de ônus que nenhum ato posterior cancela."""
    cancelled = {n for a in acts for n in a["cancels"]}
    open_onus = [a for a in acts if a["isOnus"] and a["number"] not in cancelled]
    consolidated = any("consolidacao" in a["labels"] for a in acts)
    flags = []
    for a in open_onus:
        flags.append(f"ÔNUS EM ABERTO {a['ref']}: {', '.join(a['labels'])} ({a['date']})")
    if consolidated:
        flags.append("Propriedade consolidada em nome da Caixa (padrão em leilão)")
    if not acts:
        flags.append("Nenhum ato lido: OCR fraco ou PDF fora do padrão, conferir manualmente")
    risk = "alerta" if open_onus else ("ok" if acts else "indeterminado")
    return {
        "risk": risk,
        "consolidated": consolidated,
        "openOnus": [{"ref": a["ref"], "labels": a["labels"], "date": a["date"]} for a in open_onus],
        "acts": len(acts),
        "flags": flags,
    }


def require_tools():
    missing = [t for t in ("pdftoppm", "tesseract") if shutil.which(t) is None]
    if missing:
        raise SystemExit(f"faltam ferramentas: {', '.join(missing)}. Veja o cabeçalho do script.")
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    if "por" not in langs.stdout.split():
        raise SystemExit("tesseract sem o idioma 'por'. Instale tesseract-lang / tesseract-ocr-por.")


def load_cache(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as source:
            return json.load(source).get("properties", {})
    return {}


def write_registry(out_path, state, properties):
    now = datetime.now(timezone.utc)
    with open(out_path, "w", encoding="utf-8") as target:
        json.dump({"state": state, "generatedAt": now.isoformat(timespec="seconds"), "properties": properties},
                  target, ensure_ascii=False, separators=(",", ":"))


def select_ids(ids, cache, now, stale_after):
    """No modo --ids, pula o que já está no cache e fresco. Ajuda a retomar quando o 302 corta a rodada."""
    limit = (now - stale_after).isoformat()
    todo = [i for i in ids if i not in cache or cache[i].get("fetchedAt", "") < limit]
    skipped = [i for i in ids if i not in todo]
    return todo, skipped


def process(todo, state, prices, cache, delay, max_fetch, max_seconds):
    """Lê cada id de `todo` (até max_fetch), atualizando `cache` no lugar. Devolve quantos leu.

    Para no primeiro 302/desafio guardando o que já leu; `prices` dá (preço, modalidade) por id, ou vazio.
    """
    started = time.monotonic()
    read = failures = 0
    for property_id in todo[:max_fetch]:
        if time.monotonic() - started > max_seconds:
            print("time budget reached, stopping")
            break
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "m.pdf"
            try:
                download_pdf(property_id, state, pdf)
            except urllib.error.HTTPError as error:
                print(f"HTTP {error.code} for {property_id}, stopping (not retrying around it)")
                break
            except ValueError:
                print(f"non-PDF answer for {property_id}, stopping (looks like a challenge page)")
                break
            except (urllib.error.URLError, TimeoutError) as error:
                failures += 1
                print(f"request failed for {property_id}: {error}")
                if failures >= 3:
                    print("3 failures, stopping")
                    break
                pause(delay)
                continue
            try:
                text, pages = ocr(pdf, Path(tmp))
            except subprocess.CalledProcessError as error:
                failures += 1
                print(f"OCR failed for {property_id}: {error}")
                if failures >= 3:
                    print("3 OCR failures, stopping")
                    break
                continue
        failures = 0
        price, modality = prices.get(property_id, (None, None))
        entry = assess(parse(text))
        entry.update(price=price, modality=modality, pages=pages,
                     fetchedAt=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        cache[property_id] = entry
        read += 1
        pause(delay)
    return read


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        description="Lê as matrículas da Caixa, aponta os ônus em aberto e escreve registry_<UF>.json.")
    ap.add_argument("csv", nargs="?", help="Lista_imoveis_<UF>.csv (modo varredura)")
    ap.add_argument("cache", nargs="?", help="registry_<UF>.json de entrada, no modo varredura")
    ap.add_argument("out", nargs="?", help="registry_<UF>.json de saída, no modo varredura")
    ap.add_argument("--ids", help="checa só estes ids (separados por vírgula ou espaço); exige --uf")
    ap.add_argument("--uf", help="UF, ex. SP (obrigatório com --ids)")
    ap.add_argument("--out", dest="out_file",
                    help="arquivo de saída/cache no modo --ids (default registry_<UF>.json)")
    args = ap.parse_args(argv[1:])

    max_fetch = int(os.environ.get("MAX_FETCH", "40"))
    max_seconds = int(os.environ.get("MAX_SECONDS", "1800"))
    delay = float(os.environ.get("REQUEST_DELAY", "4.0"))
    stale_after = timedelta(days=int(os.environ.get("STALE_DAYS", "30")))
    now = datetime.now(timezone.utc)
    require_tools()

    # Modo sob demanda: checa só os ids pedidos e mescla no registry, sem podar o resto.
    if args.ids:
        if not args.uf:
            raise SystemExit("--uf é obrigatório com --ids")
        state = args.uf.upper()
        ids = [i for i in re.split(r"[,\s]+", args.ids.strip()) if i]
        out_path = args.out_file or f"registry_{state}.json"
        cache = load_cache(out_path)
        todo, skipped = select_ids(ids, cache, now, stale_after)
        print(f"{state}: {len(ids)} ids requested, {len(cache)} cached, "
              f"{len(todo)} to read, {len(skipped)} already fresh")
        read = process(todo, state, {}, cache, delay, max_fetch, max_seconds)
        write_registry(out_path, state, cache)
        for i in ids:
            v = cache.get(i)
            print(f"  {i}: {v['risk']} | ônus {len(v['openOnus'])} | atos {v['acts']}"
                  if v else f"  {i}: não lido ainda")
        alerts = sum(1 for i in ids if cache.get(i, {}).get("risk") == "alerta")
        print(f"read {read}, registry now has {len(cache)}; requested with open liens: {alerts}")
        return

    # Modo varredura: lê a lista inteira, novos/alterados/velhos primeiro, e poda o que saiu da lista.
    if not (args.csv and args.cache and args.out):
        raise SystemExit("modo varredura precisa de <csv> <cache> <out>; ou use --ids --uf")
    state = os.path.basename(args.csv).removeprefix("Lista_imoveis_").removesuffix(".csv")
    listing = read_list(args.csv)
    cache = load_cache(args.cache)
    todo = plan(listing, cache, now, max_fetch, stale_after)
    print(f"{state}: {len(listing)} listed, {len(cache)} cached, {len(todo)} to read")
    read = process(todo, state, listing, cache, delay, max_fetch, max_seconds)
    kept = {i: d for i, d in cache.items() if i in listing}
    write_registry(args.out, state, kept)
    alerts = sum(1 for d in kept.values() if d.get("risk") == "alerta")
    print(f"read {read}, stored {len(kept)} of {len(listing)}, with open liens: {alerts}")


if __name__ == "__main__":
    main(sys.argv)
