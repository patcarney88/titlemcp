# titlemcp-us-il-recorder

Illinois county recorder sources for TitleMCP. Each county contributes a
source connector (`us-il-<county>-recorder`, kind `county_recorder`) that
searches the county's public index and answers `mortgage_release_search`.

Counties are a config table, not code: see
`src/titlemcp_us_il_recorder/sites.py`. The protocol lives in
`titlemcp-platform-publicsearch`, and the release method in `title_mcp`.

## Covered counties

| County | Source id | Index |
| --- | --- | --- |
| Lake | `us-il-lake-recorder` | `https://lake.il.publicsearch.us` |

Lake's index links each release to the mortgage it releases, so a lookup by
the mortgage's document number is exact.

## Release tracking

`IllinoisReleaseTrackingAdapter` plans a `release_tracking` workflow for any
Illinois county: check the county's index with `mortgage_release_search`,
follow up with the lender as the state's deadline runs out, and have a reviewer
confirm the release discharges the mortgage.

Illinois gives the mortgagee 30 days after payment to release the mortgage
(765 ILCS 905/2 and 905/4).

## Install

```bash
python -m pip install -e packages/titlemcp
python -m pip install -e packages/platforms/publicsearch
python -m pip install -e packages/jurisdictions/us/il/recorder
```

## Tests

```bash
python -m unittest discover -s packages/jurisdictions/us/il/recorder/tests -v
```
> **Runtime activation:** after installing this package, set
> `TITLE_MCP_LOAD_ENTRY_POINT_PLUGINS=true` before starting TitleMCP.
> Plugins execute installed Python code; enable this only for trusted packages.
> Installation can expose a capability manifest without registering its county sources.
> Confirm the expected connector with `list_source_connectors` or
> `list_title_capabilities.registered_source_ids` before querying it.
