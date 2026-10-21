# Wiener Linien timetables

A small public timetable feed for verifying the final scheduled departure of a
saved platform, line and direction. It contains data and a standalone compiler;
it has no connection to private app source or history.

**Datenquelle: Wiener Linien.** Source data and the derived timetable are
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). This is an independent
project, not an official Wiener Linien service. The compiler scripts are MIT
licensed; see LICENSE.

[Download the JSON feed](https://raw.githubusercontent.com/eliaslehner/WienerLinienTimetables/main/last-departures.json)
· [Refresh runs](https://github.com/eliaslehner/WienerLinienTimetables/actions/workflows/refresh.yml)

The workflow requests fresh official GTFS and route CSVs daily at 04:23 UTC and
can also be run manually. GitHub may delay scheduled jobs. It validates source
age, calendar coverage, consistent CSV generations, all five metro lines and a
75% route coverage floor before replacing the publication. Failures leave the
previous file unchanged. Watch failed workflow runs in GitHub; they need operator
attention if the official export stops advancing or its format changes.

Each index is valid only until the earlier of its calendar coverage or 14 days
after the official GTFS export date. Download time never extends that window.
Clients must reject expired data and use ordinary departure labels if evidence
is missing. A successful daily publication changes the generation/window and
creates repository activity; GitHub can disable public scheduled workflows after
60 days without repository activity, so prolonged failures require attention.

Matching uses complete ordered routes, official line/operator identity and
platform coordinates. Ambiguous identities are excluded. Weekly calendars,
exception dates, services beyond 24:00, Vienna DST and adjacent service dates are
resolved before publishing dated final departures. A final service requires a
verified break of at least three hours. The fixed official catalogue describes
the saved selections supported by the app; new selections require a reviewed
catalogue update. No personal data or live user requests are collected.

## Run locally (Python 3.11 or newer)

```sh
python -m unittest discover -p 'test_*.py'
python refresh_feed.py
```

The first build downloads roughly 91 MB of GTFS data to a temporary directory.
Phones download only the compiled JSON, currently around 0.9 MB, using ETags.
Source URLs and SHA-256 hashes are recorded in each publication.

Official inputs:
- [GTFS](https://www.wienerlinien.at/ogd_realtime/doku/ogd/gtfs/gtfs.zip)
- [Stops](https://www.wienerlinien.at/ogd_realtime/doku/ogd/wienerlinien-ogd-haltepunkte.csv)
- [Lines](https://www.wienerlinien.at/ogd_realtime/doku/ogd/wienerlinien-ogd-linien.csv)
- [Ordered routes](https://www.wienerlinien.at/ogd_realtime/doku/ogd/wienerlinien-ogd-fahrwegverlaeufe.csv)
- [CSV version](https://www.wienerlinien.at/ogd_realtime/doku/ogd/wienerlinien-ogd-version.csv)
