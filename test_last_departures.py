import datetime as dt
import unittest
import csv
import io
import json
import tempfile
import zipfile
from pathlib import Path
from build_last_departures import active_on, service_instant, final_events, match_platforms, build_index

DAY = dt.date(2026, 10, 21)
UTC = dt.timezone.utc


def calendar(**changes):
    value = dict(start_date='20261001', end_date='20261212',
                 monday='1', tuesday='1', wednesday='1', thursday='1',
                 friday='1', saturday='0', sunday='0')
    return value | changes


class CalendarTests(unittest.TestCase):
    def test_weekday_and_validity(self):
        rules = {'regular': calendar()}
        self.assertTrue(active_on('regular', DAY, rules, {}))
        self.assertFalse(active_on('regular', dt.date(2026, 10, 24), rules, {}))
        self.assertFalse(active_on('regular', dt.date(2026, 12, 14), rules, {}))
        self.assertFalse(active_on('missing', DAY, rules, {}))

    def test_exceptions_override_weekly_rules_and_allow_exception_only_service(self):
        exceptions = {('regular', '20261021'): False, ('extra', '20261021'): True}
        self.assertFalse(active_on('regular', DAY, {'regular': calendar()}, exceptions))
        self.assertTrue(active_on('extra', DAY, {}, exceptions))
        self.assertFalse(active_on('extra', DAY + dt.timedelta(days=1), {}, exceptions))

    def test_24_plus_uses_original_service_date(self):
        self.assertEqual(service_instant(DAY, 24 * 3600 + 1800),
                         dt.datetime(2026, 10, 21, 22, 30, tzinfo=UTC).timestamp())

    def test_gtfs_noon_minus_twelve_hours_handles_dst(self):
        # GTFS seconds are measured from local noon minus 12 elapsed hours.
        self.assertEqual(service_instant(dt.date(2026, 10, 25), 2 * 3600 + 1800),
                         dt.datetime(2026, 10, 25, 1, 30, tzinfo=UTC).timestamp())
        self.assertEqual(service_instant(dt.date(2026, 3, 29), 3 * 3600 + 1800),
                         dt.datetime(2026, 3, 29, 1, 30, tzinfo=UTC).timestamp())

    def test_final_service_and_adjacent_overnight_continuity(self):
        rules = {'regular': calendar(saturday='1', sunday='1')}
        times = {'regular': {5 * 3600, 24 * 3600 + 1800}}
        final = service_instant(DAY, 24 * 3600 + 1800)
        self.assertIn(final, final_events(times, rules, {}, DAY, DAY + dt.timedelta(days=3)))
        times['night'] = {36 * 60}
        exceptions = {('night', '20261022'): True}
        self.assertNotIn(final, final_events(times, rules, exceptions, DAY, DAY + dt.timedelta(days=3)))

    def test_holiday_additions_and_removals_change_final(self):
        rules = {'regular': calendar()}
        times = {'regular': {5 * 3600, 24 * 3600 + 1800}, 'holiday': {24 * 3600 + 3600}}
        final = service_instant(DAY, 25 * 3600)
        exceptions = {('regular', '20261021'): False, ('holiday', '20261021'): True}
        events = final_events(times, rules, exceptions, DAY, DAY + dt.timedelta(days=3))
        self.assertIn(final, events)
        self.assertNotIn(service_instant(DAY, 24 * 3600 + 1800), events)

    def test_short_turn_after_full_route_suppresses_earlier_claim(self):
        rules = {'regular': calendar(), 'short': calendar()}
        times = {'regular': {5 * 3600, 24 * 3600}, 'short': {24 * 3600 + 1200}}
        events = final_events(times, rules, {}, DAY, DAY + dt.timedelta(days=3))
        self.assertNotIn(service_instant(DAY, 24 * 3600), events)
        self.assertIn(service_instant(DAY, 24 * 3600 + 1200), events)

    def test_end_of_coverage_does_not_imply_service_break(self):
        rules = {'regular': calendar()}
        times = {'regular': {23 * 3600 + 1800}}
        events = final_events(times, rules, {}, DAY, DAY + dt.timedelta(days=1))
        self.assertEqual(events, [])


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.points = {'r1': (48., 16.), 'r2': (48.01, 16.), 'r3': (48.02, 16.)}
        self.stops = {'g1': (48., 16.), 'g2': (48.01, 16.), 'g3': (48.02, 16.)}
        self.patterns = [('U4', '1', ('r1', 'r2', 'r3'))]
        self.paths = [('U4', ('g1', 'g2', 'g3'))]

    def test_matches_ordered_platforms_using_opaque_ids(self):
        result, rejected = match_platforms(self.patterns, self.paths, self.points, self.stops)
        self.assertEqual(result[('U4', '1', 'r2')], 'g2')
        self.assertFalse(rejected)

    def test_opposite_direction_and_other_line_cannot_match(self):
        for paths in [[('U4', ('g3', 'g2', 'g1'))], [('U2', ('g1', 'g2', 'g3'))]]:
            result, rejected = match_platforms(self.patterns, paths, self.points, self.stops)
            self.assertFalse(result)
            self.assertIn(('U4', '1', 'r2'), rejected)

    def test_ambiguous_platforms_are_excluded(self):
        self.stops['other'] = self.stops['g2']
        self.paths.append(('U4', ('g1', 'other', 'g3')))
        result, rejected = match_platforms(self.patterns, self.paths, self.points, self.stops)
        self.assertNotIn(('U4', '1', 'r2'), result)
        self.assertIn(('U4', '1', 'r2'), rejected)

    def test_unmatched_branch_cannot_invent_a_new_platform_identity(self):
        self.points['r4'] = (49., 16.)
        self.patterns.append(('U4', '1', ('r1', 'r2', 'r4')))
        result, rejected = match_platforms(self.patterns, self.paths, self.points, self.stops)
        self.assertEqual(result[('U4', '1', 'r2')], 'g2')
        self.assertNotIn(('U4', '1', 'r4'), result)
        self.assertIn(('U4', '1', 'r4'), rejected)

    def test_long_route_can_resolve_a_displaced_station_coordinate(self):
        points = {f'r{i}': (48. + i * .01, 16.) for i in range(10)}
        stops = {f'g{i}': points[f'r{i}'] for i in range(10)}
        stops['g5'] = (stops['g5'][0] + .001, 16.)
        result, _ = match_platforms([('U1', '1', tuple(points))], [('U1', tuple(stops))], points, stops)
        self.assertEqual(result[('U1', '1', 'r5')], 'g5')

    def test_short_or_displaced_patterns_do_not_prove_identity(self):
        for points, paths in [({'r1': (0., 0.), 'r2': (0., 0.), 'r3': (0., 0.)}, self.paths),
                              (self.points, [('U4', ('g1', 'g2'))])]:
            result, _ = match_platforms(self.patterns, paths, points, self.stops)
            self.assertFalse(result)


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write_csv('points.csv', ['StopID','DIVA','Latitude','Longitude'],
                       [[f'r{i}',f'd{i}',48+i*.01,16] for i in range(3)])
        self.write_csv('lines.csv', ['LineID','LineText','MeansOfTransport'], [['opaque-line','U4','ptMetro']])
        self.write_csv('patterns.csv', ['LineID','PatternID','StopSeqCount','StopID','Direction'],
                       [['opaque-line','path',i,f'r{i}','1'] for i in range(3)])
        catalog = {'data':[{'LineText':'U4','Direction':{'2':{'stops':[
            {'DIVA':f'd{i}','StopID':f'r{i}'} for i in range(3)]}}}]}
        self.catalog = self.root / 'catalogue.json'
        self.catalog.write_text(json.dumps(catalog))
        self.agency = '04'
        self.pickup = '0'
        self.frequency = False
        self.invalid_time = False
        self.bidirectional = False

    def write_csv(self, name, columns, values):
        path = self.root / name
        with path.open('w', newline='') as f:
            writer = csv.writer(f, delimiter=';'); writer.writerow(columns); writer.writerows(values)
        return path

    def compile(self, end=dt.date(2026,11,2)):
        archive = self.root / 'feed.zip'
        data = {
            'agency.txt': (['agency_id','agency_timezone'], [[self.agency,'Europe/Vienna']]),
            'routes.txt': (['route_id','route_short_name','route_type','agency_id'], [['route','U4','1',self.agency]]),
            'stops.txt': (['stop_id','stop_lat','stop_lon'], [[f'g{i}',48+i*.01,16] for i in range(3)]),
            'calendar.txt': (['service_id',*WEEKDAYS_FOR_TEST,'start_date','end_date'],
                             [['regular',*['1']*7,'20261001','20261212']]),
            'trips.txt': (['trip_id','route_id','service_id','direction_id'],
                          [['trip','route','regular','0']] + ([['opposite','route','regular','1']] if self.bidirectional else [])),
            'stop_times.txt': (['trip_id','stop_id','stop_sequence','departure_time','pickup_type'],
                [[trip,f'g{i}',i,'bad' if self.invalid_time else '24:30:00',self.pickup]
                 for trip in (['trip','opposite'] if self.bidirectional else ['trip']) for i in range(3)])}
        if self.frequency:
            data['frequencies.txt'] = (['trip_id','start_time','end_time','headway_secs'], [['trip','00:00:00','25:00:00','600']])
        with zipfile.ZipFile(archive,'w') as z:
            for name, (columns, values) in data.items():
                text = io.StringIO(); writer = csv.writer(text); writer.writerow(columns); writer.writerows(values)
                info = zipfile.ZipInfo(name, (2026,10,19,6,0,0)); z.writestr(info, text.getvalue())
        return build_index(archive,self.catalog,self.root/'points.csv',self.root/'lines.csv',
                           self.root/'patterns.csv',DAY,end)

    def test_full_pipeline_uses_current_platform_direction_not_old_catalogue_key(self):
        data = self.compile()
        route = next(r for r in data['routes'] if r['diva']=='d1')
        self.assertEqual(route['directionID'], '1')
        self.assertEqual(route['platforms'], {'r1':'g1'})
        self.assertIn(service_instant(DAY,24*3600+1800),route['lastDepartures'])
        self.assertEqual(data['provenance']['feedExport'],'2026-10-19')

    def test_other_agency_cannot_supply_the_same_line_label(self):
        self.agency = '03'
        self.assertEqual(self.compile()['routes'], [])

    def test_conditional_and_no_boarding_services_are_ineligible(self):
        for pickup in ['1','2','3']:
            self.pickup = pickup
            self.assertEqual(self.compile()['routes'], [], pickup)

    def test_missing_time_frequency_and_bidirectional_platforms_fail_closed(self):
        for flag in ['invalid_time','frequency','bidirectional']:
            setattr(self, flag, True)
            self.assertEqual(self.compile()['routes'], [], flag)
            setattr(self, flag, False)

    def test_expired_feed_cannot_be_extended_by_generator(self):
        with self.assertRaisesRegex(ValueError, 'Index dates'):
            self.compile(dt.date(2026,11,3))

    def test_unknown_added_platform_prevents_partial_saved_route_match(self):
        catalog=json.loads(self.catalog.read_text())
        catalog['data'][0]['Direction']['2']['stops'].append({'DIVA':'d1','StopID':'unknown'})
        self.catalog.write_text(json.dumps(catalog))
        self.assertFalse(any(r['diva']=='d1' for r in self.compile()['routes']))


WEEKDAYS_FOR_TEST = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']


if __name__ == '__main__':
    unittest.main()
