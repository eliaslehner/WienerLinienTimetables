import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from refresh_feed import publication_window, validate_publication, publish, download_inputs
from build_last_departures import midnight

NOW = dt.datetime(2026, 10, 21, 12, tzinfo=dt.timezone.utc)


def document():
    return dict(schemaVersion=1, timezone='Europe/Vienna', minimumBreakSeconds=10800,
                generatedAt=int(NOW.timestamp()), validFrom=midnight(dt.date(2026, 10, 21)),
                validUntil=midnight(dt.date(2026, 11, 2)),
                provenance=dict(feedExport='2026-10-19'),
                routes=[dict(line=line) for line in ('U1', 'U2', 'U3', 'U4', 'U6')])


class RefreshTests(unittest.TestCase):
    def test_window_uses_vienna_date_and_export_age_across_dst(self):
        now = dt.datetime(2026, 10, 24, 23, 30, tzinfo=dt.timezone.utc)
        self.assertEqual(publication_window(dt.date(2026, 10, 19), dt.date(2026, 12, 1), now),
                         (dt.date(2026, 10, 25), dt.date(2026, 11, 2)))

    def test_calendar_coverage_caps_window(self):
        self.assertEqual(publication_window(dt.date(2026, 10, 19), dt.date(2026, 10, 24), NOW)[1],
                         dt.date(2026, 10, 24))

    def test_rejects_stale_future_export_and_less_than_day_runway(self):
        for export, end in [(dt.date(2026, 10, 1), dt.date(2026, 12, 1)),
                            (dt.date(2026, 10, 22), dt.date(2026, 12, 1)),
                            (dt.date(2026, 10, 19), dt.date(2026, 10, 22))]:
            with self.assertRaises(ValueError):
                publication_window(export, end, NOW)

    def test_coverage_collapse_missing_metro_and_empty_fail_closed(self):
        value = document()
        validate_publication(value, None, NOW, baseline_routes=5)
        with self.assertRaises(ValueError):
            validate_publication(value, {'routes': [{}] * 10}, NOW, baseline_routes=5)
        for routes in [[], value['routes'][:-1]]:
            with self.assertRaises(ValueError):
                validate_publication(value | {'routes': routes}, None, NOW, baseline_routes=5)

    def test_no_rollback_or_expiry_extension_from_download_time(self):
        value = document()
        with self.assertRaises(ValueError):
            validate_publication(value | {'validUntil': midnight(dt.date(2026, 11, 3))}, None, NOW, 5)
        with self.assertRaises(ValueError):
            validate_publication(value, value | {'generatedAt': NOW.timestamp() + 100}, NOW, 5)

    def test_atomic_failure_and_validation_preserve_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'last-departures.json'
            output.write_text('previous')
            with self.assertRaises(ValueError):
                publish(document() | {'routes': []}, output, None, NOW, baseline_routes=5)
            self.assertEqual(output.read_text(), 'previous')
            with patch('refresh_feed.os.replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    publish(document(), output, None, NOW, baseline_routes=5)
            self.assertEqual(output.read_text(), 'previous')
            publish(document(), output, None, NOW, baseline_routes=5)
            self.assertEqual(json.loads(output.read_text()), document())

    def test_inconsistent_ogd_generation_and_network_error_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            counter = [0]
            def inconsistent(url, target):
                counter[0] += 1
                target.write_bytes(str(counter[0]).encode() if 'version' in url else b'input')
            with self.assertRaises(ValueError):
                download_inputs(Path(directory), fetch=inconsistent)
            def failure(url, target):
                raise OSError('offline')
            with self.assertRaises(OSError):
                download_inputs(Path(directory), fetch=failure)


if __name__ == '__main__':
    unittest.main()
