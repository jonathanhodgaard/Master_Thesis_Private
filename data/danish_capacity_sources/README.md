# Danish capacity source snapshots

These files were migrated byte-for-byte from `master_thesis/data` into this
repository. `manifest.json` records their sizes and SHA-256 checksums, plus the
original working versions' preparation-script checksums. Their release/download
dates were not recorded in the companion project and are not inferred here.

| File | Role |
| --- | --- |
| `ENS_power_plant_register.csv` | Danish conventional power, CHP and district-heating register; semicolon separator and decimal comma |
| `ENS_wind_&_solar_register.csv` | Danish wind/PV installations, capacities, operating dates and coordinates |
| `powerplants_PPM.csv` | Pinned original PPM snapshot used by the companion preparation recipe, before Danish replacement |
| `JRC-IDEES-2023_Residential_DK.xlsx` | `RES_hh_num` worksheet, 2023 household heating shares |
| `postcode_coordinates.csv` | Cached postcode centroids; no API credentials |

The PPM snapshot's upstream version is **unverified**. The former destination
folder `data/powerplants/archive/0.8.1/` was a model input location, not evidence
that this source snapshot was PPM 0.8.1. Do not replace this pinned input with a
fresh download as part of routine preparation.

Normal preparation reads these files without changing them or making online
postcode requests. For an intentional coordinate-cache refresh, set the private
`ADDR_KEY` environment variable and run the power preparation script with
`--refresh-postcodes --output-dir tmp/danish_capacity_review`. Recheck outputs and
update the manifest's cache checksum if the source cache changes intentionally.
ENS postcode `9999` is an unknown-location sentinel and is never queried online.

Prepared model inputs are written to `data/existing_infrastructure/`, separately
from both these source files and PyPSA-Eur's downloaded datasets. Historical
private input `powerplants_pre_migration.csv` is retained under
`data/existing_infrastructure/archive/`; it is not used by new thesis builds.

See [Preparation and execution workflow](../../doc/preparation_workflow.md) for
commands, assumptions, and the ordered path from sources to disruption results.
