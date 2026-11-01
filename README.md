# caixa-csv-mirror

Mirrors the public per-state property lists that Caixa Econômica Federal publishes for automated download
(`https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_<UF>.csv`) as GitHub release assets.

It exists because Caixa's bot protection blocks the datacenter address of the host that runs
[property-filter](https://github.com/vinirib/property-filter), while GitHub runners can download the files.
The workflow runs every 6 hours and replaces the assets of the `latest` release. A failed or suspicious download
never replaces the previous asset.

Files are served as downloaded (windows-1252, `;` separated):
`https://github.com/vinirib/caixa-csv-mirror/releases/download/latest/Lista_imoveis_SP.csv`

## Property details

Besides the list, the workflow publishes `details_<UF>.json`
(`https://github.com/vinirib/caixa-csv-mirror/releases/download/latest/details_SP.json`), keyed by property id.
It holds what only the Caixa detail page has: registration (matrícula) number and PDF, county, registry office,
municipal registration (inscrição imobiliária), edital, auction dates, auctioneer, payment options and expense rules.

`scripts/fetch_details.py` fetches only new, changed (price or sale modality) or stale (7 days) ids, one request per
second, with a cap per run, and reuses the previous JSON as cache. It identifies itself with the same user agent as the
list download. Any redirect, HTTP error or non-detail page stops the run and keeps what was collected: it does not try
to get past bot protection. The step is `continue-on-error`, so the list is always mirrored.

## Registration (matrícula) due diligence — local only

For each property Caixa publishes the registration certificate at `/editais/matricula/<UF>/<id>.pdf`.
`scripts/fetch_registry.py` reads it, runs OCR, splits the certificate into its acts (R.n registros and AV.n
averbações), classifies each one and reports the liens (penhora, indisponibilidade/CNIB, hipoteca, usufruto, ação)
that no later act has cancelled. It writes `registry_<UF>.json` keyed by property id, with per-property
`risk` (`ok` / `alerta` / `indeterminado`), `consolidated` and the open liens. The previous JSON is the cache, and it
fetches new, changed or stale ids first, one request per second, with a cap per run.

Unlike the CSV list, the matrícula PDFs sit behind the same Radware/ShieldSquare protection as the detail pages, so
they are **not** reachable from a datacenter IP — a GitHub runner is redirected at once. This script is therefore
**local only, run by hand now and then** from a residential connection; each run resumes from the cache and stops the
moment it is challenged.

Prerequisites: poppler (`pdftoppm`) and tesseract with Portuguese data.

```sh
# one-time
brew install poppler tesseract tesseract-lang        # macOS
# apt-get install poppler-utils tesseract-ocr tesseract-ocr-por   # Debian

# fetch the current list, then read a batch of matrículas
curl -sSL -A "caixa-csv-mirror/1.0 (github.com/vinirib/caixa-csv-mirror)" \
  -o Lista_imoveis_SP.csv \
  https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_SP.csv
# or reuse the mirror: curl -L -o Lista_imoveis_SP.csv https://github.com/vinirib/caixa-csv-mirror/releases/download/latest/Lista_imoveis_SP.csv

python3 scripts/fetch_registry.py Lista_imoveis_SP.csv registry_SP.json registry_SP.json
```

The cache and output are the same file, so each run picks up where the last stopped. Tune a run with
`MAX_FETCH` (default 300), `MAX_SECONDS` (1800), `REQUEST_DELAY` (1.0) and `STALE_DAYS` (30).

Run the tests with `python3 -m unittest discover -s tests`.
