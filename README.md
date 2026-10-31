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

Run the tests with `python3 -m unittest discover -s tests`.
