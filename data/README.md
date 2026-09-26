# Data

Collected daily by the workflow in `.github/workflows/deploy.yml`. Records are
never deleted; changes over time are kept in each record and in git history.

| Path | Contents | Source and license |
|---|---|---|
| `meetings/meetings.json` | One record per public meeting, with a `history` of changes the city made after posting | City of Gloucester calendar (public record) |
| `meetings/agendas/<id>.pdf` | Each agenda as the city posted it | City of Gloucester Archive Center (public record) |
| `meetings/minutes/<id>.pdf` | Each set of minutes as the city posted it | City of Gloucester Archive Center (public record) |
| `summaries/<sha256>.json` | Readable text and a plain-English summary of an agenda or minutes (`kind`), keyed by the PDF's SHA-256. AI-generated; see `model` and `generated_at` | Derived from the agenda it names in `source_url` |
| `311/requests.json` | One record per public SeeClickFix request: category, location, ward, status, and submitted/acknowledged/closed/reopened times. No descriptions, photos, or reporter details | [SeeClickFix](https://seeclickfix.com), [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/) |
| `311/scorecard.json` | Metrics shown on the 311 page, recomputed daily | Derived from `311/requests.json`; same license |
| `static/gloucester-precincts-2022.geojson` | Ward and precinct boundaries with 2020 population | [MassGIS, Wards and Precincts (2022)](https://gis.data.mass.gov/maps/aec5130790814ace94438d3bcf23cf9a) |

Data derived from SeeClickFix is shared under the same CC BY-NC-SA 3.0 license,
with attribution to seeclickfix.com.
