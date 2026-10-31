# caixa-csv-mirror

Mirrors the public per-state property lists that Caixa Econômica Federal publishes for automated download
(`https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_<UF>.csv`) as GitHub release assets.

It exists because Caixa's bot protection blocks the datacenter address of the host that runs
[property-filter](https://github.com/vinirib/property-filter), while GitHub runners can download the files.
The workflow runs every 6 hours and replaces the assets of the `latest` release. A failed or suspicious download
never replaces the previous asset.

Files are served as downloaded (windows-1252, `;` separated):
`https://github.com/vinirib/caixa-csv-mirror/releases/download/latest/Lista_imoveis_SP.csv`
