# Data

Collected daily by the workflow in `.github/workflows/deploy.yml`. Records are
never deleted; changes over time are kept in each record and in git history.

| Path | Contents | Source and license |
|---|---|---|
| `meetings/meetings.json` | One record per public meeting, with a `history` of changes the city made after posting | City of Gloucester calendar; School Committee meetings also from Gloucester Public Schools (public record) |
| `meetings/agendas/<id>.pdf` | Each agenda as the city or school district posted it. With a `[storage]` table in the town's config, these are kept in its documents bucket instead (Gloucester's: `files.publick.org/gloucester-ma/agendas/`) | City of Gloucester Archive Center; School Committee agendas from Gloucester Public Schools' Google Drive (public record) |
| `meetings/minutes/<id>.pdf` | Each set of minutes as the city or school district posted it. With a `[storage]` table, kept in the documents bucket instead (Gloucester's: `files.publick.org/gloucester-ma/minutes/`) | City of Gloucester Archive Center; School Committee minutes from Gloucester Public Schools' Google Drive (public record) |
| `summaries/<sha256>.json` | Readable text and a plain-English summary of an agenda or minutes (`kind`), keyed by the PDF's SHA-256. AI-generated; see `model` and `generated_at` | Derived from the agenda it names in `source_url` |
| `311/requests.json` | One record per public SeeClickFix request: category, location, ward, status, and submitted/acknowledged/closed/reopened times. No descriptions, photos, or reporter details | [SeeClickFix](https://seeclickfix.com), [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/) |
| `311/scorecard.json` | Metrics shown on the 311 page, recomputed daily | Derived from `311/requests.json`; same license |
| `finance/tax_bill.json` | Average single-family tax bill, recent fiscal years | [Mass. Division of Local Services, Municipal Databank](https://dls-gw.dor.state.ma.us/reports/rdPage.aspx?rdReport=AverageSingleTaxBill.SingleFamTaxBill_wRange) (public record) |
| `finance/budget.json` | General fund spending by function, revenue by source, levy and levy limit, free cash, stabilization fund, bond ratings, and spending per resident with the state median | [Mass. Division of Local Services, Municipal Databank](https://dls-gw.dor.state.ma.us/reports/rdPage.aspx?rdReport=ScheduleA.GenFund_MAIN) (public record) |
| `housing/housing.json` | New homes permitted by year; home value, rent, and rent burden with margins of error; Subsidized Housing Inventory share; residential parcels by type | [Census Building Permits Survey](https://www.census.gov/construction/bps/), [American Community Survey via Census Reporter](https://censusreporter.org), [EOHLC Subsidized Housing Inventory](https://www.mass.gov/info-details/subsidized-housing-inventory-shi), [Mass. DLS](https://dls-gw.dor.state.ma.us/reports/rdPage.aspx?rdReport=PropertyTaxInformation.LA4.Parcel_counts_vals) (public records) |
| `schools/schools.json` | Graduation rate, chronic absenteeism, and MCAS results (grades 3–8) for the district and the state, by year | [DESE, Education-to-Career data hub](https://educationtocareer.data.mass.gov) |
| `labor/unemployment.json` | Monthly unemployment rate for Gloucester city, with the statewide rate | [BLS Local Area Unemployment Statistics](https://www.bls.gov/lau/) (public domain) |
| `static/gloucester-precincts-2022.geojson` | Ward and precinct boundaries with 2020 population | [MassGIS, Wards and Precincts (2022)](https://gis.data.mass.gov/maps/aec5130790814ace94438d3bcf23cf9a) |

Data derived from SeeClickFix is shared under the same CC BY-NC-SA 3.0 license,
with attribution to seeclickfix.com.
