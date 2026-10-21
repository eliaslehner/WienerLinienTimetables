#!/usr/bin/env python3
"""Publish a bounded rolling index from fresh official inputs (Python 3.11+)."""
import argparse
import csv
import datetime as dt
import io
import json
import math
import os
from pathlib import Path
import tempfile
import urllib.request
import zipfile

from build_last_departures import build_index, SOURCE, VIENNA, MAX_FEED_AGE_DAYS, midnight

BASELINE_ROUTES = 3701
METRO = {'U1', 'U2', 'U3', 'U4', 'U6'}
MAX_BYTES = 8 * 1024 * 1024
INPUTS = dict(gtfs='gtfs/gtfs.zip', stops='wienerlinien-ogd-haltepunkte.csv',
              lines='wienerlinien-ogd-linien.csv', patterns='wienerlinien-ogd-fahrwegverlaeufe.csv')
VERSION = SOURCE + 'wienerlinien-ogd-version.csv'


def publication_window(export, calendar_end, now):
    today = now.astimezone(VIENNA).date()
    end = min(export + dt.timedelta(days=MAX_FEED_AGE_DAYS), calendar_end)
    if export > today or midnight(end) - now.timestamp() < 86400:
        raise ValueError('Official export is future-dated, stale, or has less than 24 hours of runway')
    return today, end


def archive_window(archive, now):
    with zipfile.ZipFile(archive) as feed:
        export = dt.date(*feed.getinfo('stop_times.txt').date_time[:3])
        def rows(name):
            if name not in feed.namelist():
                return []
            with feed.open(name) as stream:
                return list(csv.DictReader(io.TextIOWrapper(stream, encoding='utf-8-sig', newline='')))
        ends = [r['end_date'] for r in rows('calendar.txt')] + [r['date'] for r in rows('calendar_dates.txt')]
        if not ends:
            raise ValueError('No official calendar coverage')
        end = dt.datetime.strptime(max(ends), '%Y%m%d').date() + dt.timedelta(days=1)
        return publication_window(export, end, now)


def download(url, target):
    request = urllib.request.Request(url, headers={'User-Agent': 'WienerLinienTimetables/1.0'})
    limit = 256 * 1024 * 1024 if url.endswith('.zip') else 32 * 1024 * 1024
    with urllib.request.urlopen(request, timeout=90) as response, target.open('wb') as stream:
        if response.status != 200 or not response.url.startswith('https://'):
            raise ValueError('Invalid official source response')
        total = 0
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > limit:
                raise ValueError('Official source exceeded size limit')
            stream.write(block)


def download_inputs(directory, fetch=download):
    before = directory / 'version-before.csv'
    after = directory / 'version-after.csv'
    fetch(VERSION, before)
    inputs = {}
    for name, path in INPUTS.items():
        inputs[name] = directory / Path(path).name
        fetch(SOURCE + path, inputs[name])
    fetch(VERSION, after)
    if before.read_bytes() != after.read_bytes():
        raise ValueError('OGD generation changed during download; retry with a consistent input set')
    return inputs


def build_publication(inputs, catalogue, previous, now):
    start, end = archive_window(inputs['gtfs'], now)
    value = build_index(inputs['gtfs'], catalogue, inputs['stops'], inputs['lines'], inputs['patterns'], start, end)
    value['generatedAt'] = int(now.timestamp())
    validate_publication(value, previous, now)
    return value


def validate_publication(value, previous, now, baseline_routes=BASELINE_ROUTES):
    export = dt.date.fromisoformat(value['provenance']['feedExport'])
    start, maximum_end = publication_window(export, export + dt.timedelta(days=MAX_FEED_AGE_DAYS), now)
    if (value['schemaVersion'] != 1 or value['timezone'] != 'Europe/Vienna'
            or value['minimumBreakSeconds'] < 10800 or value['validFrom'] != midnight(start)
            or not now.timestamp() + 86400 <= value['validUntil'] <= midnight(maximum_end)
            or not value['validFrom'] <= value['generatedAt'] <= now.timestamp()):
        raise ValueError('Invalid publication validity or schema')
    routes = value['routes']
    reference_count = max(baseline_routes, len(previous['routes']) if previous else 0)
    if len(routes) < math.ceil(reference_count * .75) or not METRO <= {r['line'] for r in routes}:
        raise ValueError('Verified coverage collapsed or a metro line is missing')
    if previous and (export < dt.date.fromisoformat(previous['provenance']['feedExport'])
                     or value['generatedAt'] < previous.get('generatedAt', 0)):
        raise ValueError('Publication would roll back source or generation')


def publish(value, output, previous, now, baseline_routes=BASELINE_ROUTES):
    validate_publication(value, previous, now, baseline_routes)
    data = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
    if len(data) > MAX_BYTES:
        raise ValueError('Compiled feed exceeds app size limit')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.timetable-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalogue', type=Path, default=Path('catalogue.json'))
    parser.add_argument('--output', type=Path, default=Path('last-departures.json'))
    for key in INPUTS:
        parser.add_argument('--' + key, type=Path, help='Use a reviewed local official input set (all four required)')
    args = parser.parse_args()
    local = {key: getattr(args, key) for key in INPUTS}
    if any(local.values()) and not all(local.values()):
        parser.error('Provide all four input files, or omit them to download a consistent official set')
    previous = json.loads(args.output.read_text()) if args.output.exists() else None
    with tempfile.TemporaryDirectory(prefix='wl-timetable-') as directory:
        inputs = local if all(local.values()) else download_inputs(Path(directory))
        now = dt.datetime.now(dt.timezone.utc)
        value = build_publication(inputs, args.catalogue, previous, now)
        publish(value, args.output, previous, now)
    print(json.dumps({key: value[key] for key in ('generatedAt', 'validFrom', 'validUntil', 'coverage')}, indent=2))


if __name__ == '__main__':
    main()
