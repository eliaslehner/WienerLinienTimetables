#!/usr/bin/env python3
"""Compile official GTFS + OGD route identities into a small, expiring app index.

Datenquelle: Wiener Linien (CC BY 4.0). No fuzzy names or numeric ID conversion.
The input archive is streamed; its large stop_times.txt is never extracted.
"""
import argparse
import bisect
import collections
import csv
import datetime as dt
import hashlib
import io
import itertools
import json
import math
from pathlib import Path
import sys
import zipfile
from zoneinfo import ZoneInfo

VIENNA = ZoneInfo('Europe/Vienna')
WEEKDAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
BREAK_SECONDS = 3 * 3600
MAX_FEED_AGE_DAYS = 14
SOURCE = 'https://www.wienerlinien.at/ogd_realtime/doku/ogd/'


def active_on(service, day, calendars, exceptions):
    stamp = day.strftime('%Y%m%d')
    if (service, stamp) in exceptions:
        return exceptions[service, stamp]
    rule = calendars.get(service)
    return bool(rule and rule['start_date'] <= stamp <= rule['end_date']
                and rule[WEEKDAYS[day.weekday()]] == '1')


def service_instant(day, seconds):
    # The GTFS definition is local noon minus 12 *elapsed* hours, not local midnight.
    noon = dt.datetime.combine(day, dt.time(12), VIENNA).timestamp()
    return int(noon - 12 * 3600 + seconds)


def midnight(day):
    return int(dt.datetime.combine(day, dt.time(), VIENNA).timestamp())


def final_events(times, calendars, exceptions, valid_from, valid_until):
    lower, upper = midnight(valid_from), midnight(valid_until)
    days = [valid_from + dt.timedelta(days=i)
            for i in range(-2, (valid_until - valid_from).days + 2)]
    by_day = []
    for day in days:
        seconds = set().union(*(values for service, values in times.items()
                               if active_on(service, day, calendars, exceptions)))
        by_day.append(sorted(service_instant(day, value) for value in seconds))
    all_events = sorted(set(itertools.chain.from_iterable(by_day)))
    result = set()
    for events in by_day:
        if not events:
            continue
        last = events[-1]
        if not lower <= last or last + BREAK_SECONDS >= upper:
            continue
        next_index = bisect.bisect_right(all_events, last)
        if next_index == len(all_events) or all_events[next_index] - last >= BREAK_SECONDS:
            result.add(last)
    return sorted(result)


def distance(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    return math.hypot((lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2)),
                      lat2 - lat1) * 111_320


def match_platforms(patterns, paths, points, stops):
    """Join *complete ordered routes*, accepting only one GTFS identity per RBL.

    Nearby opposite platforms are not resolved by choosing the nearest point.
    Line identity + full route order disambiguate them. Any unmatched OGD branch
    cannot invent a binding. Every RBL still requires positive full-route evidence.
    """
    candidates = collections.defaultdict(set)
    for line, path in paths:
        candidates[line, len(path)].add(path)
    matches = collections.defaultdict(set)
    rejected = set()
    for line, direction, pattern in patterns:
        keys = [(line, direction, rbl) for rbl in pattern]
        coords = [points.get(rbl) for rbl in pattern]
        found = []
        if len(pattern) >= 3 and all(coords) and max(distance(coords[0], c) for c in coords) >= 250:
            for path in candidates[line, len(pattern)]:
                if all(s in stops for s in path):
                    distances = [distance(c, stops[s]) for c, s in zip(coords, path)]
                    # Different exports sometimes place a station at opposite ends
                    # of its platform. A whole route needs >=80% close anchors.
                    if max(distances) <= 150 and sum(d <= 35 for d in distances) >= max(3, math.ceil(.8 * len(path))):
                        found.append(path)
        if not found:
            rejected.update(keys)
        for path in found:
            for key, stop in zip(keys, path):
                matches[key].add(stop)
    rejected.difference_update(matches)
    rejected.update(key for key, values in matches.items() if len(values) != 1)
    return ({key: next(iter(values)) for key, values in matches.items() if key not in rejected}, rejected)


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def csv_file(path):
    with open(path, encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream, delimiter=';'))


def seconds(value):
    hour, minute, second = map(int, value.split(':'))
    if not 0 <= hour < 48 or not 0 <= minute < 60 or not 0 <= second < 60:
        raise ValueError('Unsupported GTFS departure time: ' + value)
    return hour * 3600 + minute * 60 + second


def route_type(means):
    if means == 'ptMetro':
        return '1'
    if means in ('ptTram', 'ptTramWLB'):
        return '0'
    if means in ('ptBusCity', 'ptBusNight'):
        return '3'
    return None


def build_index(archive, catalogue, stops_csv, lines_csv, patterns_csv, valid_from, valid_until):
    point_rows = {r['StopID']: r for r in csv_file(stops_csv)}
    points = {rbl: (float(r['Latitude']), float(r['Longitude'])) for rbl, r in point_rows.items()}
    ogd_lines = {r['LineID']: r for r in csv_file(lines_csv)}
    by_line = {r['LineText']: r for r in ogd_lines.values()}
    patterns = collections.defaultdict(list)
    for row in csv_file(patterns_csv):
        line = ogd_lines.get(row['LineID'])
        if line and route_type(line['MeansOfTransport']) and row['Direction'] in ('1', '2'):
            patterns[line['LineText'], row['Direction'], row['PatternID']].append(
                (int(row['StopSeqCount']), row['StopID']))
    ogd_paths = [(line, direction, tuple(rbl for _, rbl in sorted(rows)))
                 for (line, direction, _), rows in patterns.items()]
    times = collections.defaultdict(lambda: collections.defaultdict(set))
    directions = collections.defaultdict(set)
    blocked = set()
    paths = set()
    with zipfile.ZipFile(archive) as feed:
        def rows(name):
            return csv.DictReader(io.TextIOWrapper(feed.open(name), encoding='utf-8-sig', newline=''))
        calendars = {r['service_id']: r for r in rows('calendar.txt')} if 'calendar.txt' in feed.namelist() else {}
        exceptions = {(r['service_id'], r['date']): r['exception_type'] == '1'
                      for r in rows('calendar_dates.txt')} if 'calendar_dates.txt' in feed.namelist() else {}
        export = dt.date(*feed.getinfo('stop_times.txt').date_time[:3])
        bounds = [r['end_date'] for r in calendars.values()] + [date for _, date in exceptions]
        if not bounds:
            raise ValueError('No service calendar coverage')
        coverage_end = dt.datetime.strptime(max(bounds), '%Y%m%d').date() + dt.timedelta(days=1)
        expiry = min(export + dt.timedelta(days=MAX_FEED_AGE_DAYS), coverage_end)
        if valid_from < export or not valid_from < valid_until <= expiry:
            raise ValueError(f'Index dates must be within {export} .. {expiry} (exclusive end)')
        agencies = {r['agency_id'] for r in rows('agency.txt') if r['agency_timezone'] == 'Europe/Vienna'}
        routes = {}
        for r in rows('routes.txt'):
            line = by_line.get(r['route_short_name'])
            if (line and r['route_type'] == route_type(line['MeansOfTransport'])
                    and r['agency_id'] in agencies
                    and r['agency_id'] == ('03' if line['MeansOfTransport'] == 'ptTramWLB' else '04')):
                routes[r['route_id']] = r['route_short_name']
        trip_rows = {r['trip_id']: r for r in rows('trips.txt') if r['route_id'] in routes}
        frequency_trips = {r['trip_id'] for r in rows('frequencies.txt')} if 'frequencies.txt' in feed.namelist() else set()
        gtfs_stops = {r['stop_id']: (float(r['stop_lat']), float(r['stop_lon'])) for r in rows('stops.txt')}
        window = [valid_from + dt.timedelta(days=i)
                  for i in range(-2, (valid_until - valid_from).days + 2)]
        services = {r['service_id'] for r in trip_rows.values()}
        active_services = {service for service in services
                           if any(active_on(service, day, calendars, exceptions) for day in window)}
        # Current Wiener Linien exports group stop_times by trip. Validate this
        # precondition rather than silently accepting an incomplete route path.
        seen = set()
        for trip_id, group in itertools.groupby(rows('stop_times.txt'), key=lambda r: r['trip_id']):
            if trip_id in seen:
                raise ValueError('stop_times must be grouped by trip_id; sort the input before compiling')
            seen.add(trip_id)
            trip = trip_rows.get(trip_id)
            entries = list(group)
            if not trip:
                continue
            line = routes[trip['route_id']]
            entries.sort(key=lambda r: int(r['stop_sequence']))
            paths.add((line, tuple(r['stop_id'] for r in entries)))
            if trip['service_id'] not in active_services:
                continue
            for entry in entries:
                key = (line, entry['stop_id'])
                pickup = entry.get('pickup_type', '') or '0'
                if pickup == '1':
                    continue  # Alighting-only terminal is not a passenger departure.
                if pickup != '0' or trip_id in frequency_trips or not trip.get('direction_id'):
                    blocked.add(key)
                    continue
                try:
                    value = seconds(entry['departure_time'])
                except (ValueError, KeyError):
                    blocked.add(key)
                    continue
                directions[key].add(trip['direction_id'])
                times[key][trip['service_id']].add(value)
    crosswalk, rejected = match_platforms(ogd_paths, paths, points, gtfs_stops)
    by_rbl = collections.defaultdict(list)
    for (line, direction, rbl), gtfs in crosswalk.items():
        by_rbl[line, rbl].append((direction, gtfs))
    with open(catalogue, encoding='utf-8') as stream:
        catalog = json.load(stream)
    result = []
    skipped = collections.Counter()
    for line in catalog['data']:
        for direction, route in line['Direction'].items():
            if direction not in ('1', '2'):
                continue
            by_diva = collections.defaultdict(set)
            for stop in route['stops']:
                by_diva[stop['DIVA']].add(stop['StopID'])
            for diva, rbls in sorted(by_diva.items()):
                identities = {rbl: by_rbl[line['LineText'], rbl] for rbl in sorted(rbls)}
                if any(len(v) != 1 for v in identities.values()) or len({v[0][0] for v in identities.values() if v}) != 1:
                    skipped['ambiguousOrMissingDirection'] += 1
                    continue
                current_direction = next(iter(identities.values()))[0][0]
                platforms = {rbl: values[0][1] for rbl, values in identities.items()}
                if any(not gtfs or point_rows.get(rbl, {}).get('DIVA') != diva for rbl, gtfs in platforms.items()):
                    skipped['unverifiedPlatformOrBranch'] += 1
                    continue
                keys = [(line['LineText'], gtfs) for gtfs in platforms.values()]
                if any(key in blocked or len(directions[key]) != 1 or not times[key] for key in keys):
                    skipped['conditionalMissingOrBidirectionalService'] += 1
                    continue
                combined = collections.defaultdict(set)
                for key in keys:
                    for service, values in times[key].items():
                        combined[service].update(values)
                events = final_events(combined, calendars, exceptions, valid_from, valid_until)
                result.append(dict(diva=diva, line=line['LineText'], directionID=current_direction,
                                   stopIDs=sorted(rbls), platforms=platforms, lastDepartures=events))
    result = list({(r['diva'], r['line'], tuple(r['stopIDs'])): r for r in result}.values())
    result.sort(key=lambda r: (r['line'], r['diva'], r['stopIDs']))
    return dict(schemaVersion=1, timezone='Europe/Vienna', validFrom=midnight(valid_from),
                validUntil=midnight(valid_until), minimumBreakSeconds=BREAK_SECONDS,
                provenance=dict(attribution='Datenquelle: Wiener Linien', license='CC BY 4.0',
                                gtfsURL=SOURCE + 'gtfs/gtfs.zip', feedExport=str(export),
                                gtfsSHA256=digest(archive),
                                inputs={Path(p).name: digest(p)
                                        for p in (stops_csv, lines_csv, patterns_csv, catalogue)}),
                coverage=dict(verifiedRoutes=len(result), routesWithLastDepartures=sum(bool(r['lastDepartures']) for r in result),
                              skipped=dict(skipped), verifiedPlatformBindings=len(crosswalk),
                              rejectedPlatformBindings=len(rejected)), routes=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gtfs', required=True)
    parser.add_argument('--catalogue', required=True)
    parser.add_argument('--stops', required=True)
    parser.add_argument('--lines', required=True)
    parser.add_argument('--patterns', required=True)
    parser.add_argument('--from-date', required=True, type=dt.date.fromisoformat)
    parser.add_argument('--until-date', required=True, type=dt.date.fromisoformat)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    value = build_index(args.gtfs, args.catalogue, args.stops, args.lines, args.patterns,
                        args.from_date, args.until_date)
    Path(args.output).write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(json.dumps(value['coverage'], indent=2), file=sys.stderr)


if __name__ == '__main__':
    main()
