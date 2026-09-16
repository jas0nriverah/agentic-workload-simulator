import copy
import struct
import unittest

from scripts.validation.analyze_cpu_capture_overhead import binary_counts, paired_spans


class OverheadExtractionTests(unittest.TestCase):
    def rows(self):
        start = {'span_id': 's', 'event_id': 'pre', 'event_kind': 'tool_event_start',
                 'terminal': False, 'clock': {'clock_id': 'CLOCK_MONOTONIC_RAW', 'boot_id': 'b'},
                 'start_mono_ns': 10_000_000, 'end_mono_ns': None, 'duration_ms': None}
        end = {**start, 'event_id': 'post', 'event_kind': 'tool_event', 'terminal': True,
               'end_mono_ns': 15_000_000, 'duration_ms': 5.0}
        return [start, end]

    def test_pair_preserves_exact_pre_event_identity_and_excludes_intent(self):
        rows = self.rows() + [{'event_kind': 'tool_intent'}]
        self.assertEqual(paired_spans(rows)[0]['start_event_id'], 'pre')

    def test_missing_duplicate_and_wrong_duration_rejected(self):
        rows = self.rows()
        for bad in [rows[:1], rows+[rows[1]], [rows[0], {**rows[1], 'duration_ms': 7.0}]]:
            with self.subTest(rows=bad), self.assertRaises(ValueError):
                paired_spans(bad)

    def test_clock_identity_change_rejected(self):
        rows = copy.deepcopy(self.rows())
        rows[1]['clock'] = {**rows[1]['clock'], 'clock_id': 'CLOCK_MONOTONIC'}
        with self.assertRaisesRegex(ValueError, 'clock mismatch'):
            paired_spans(rows)

    def test_native_count_and_envelope_without_summing_overlapping_events(self):
        packets = [struct.pack('<4Q', 42, seq, start, end)+bytes(320)
                   for seq,start,end in [(1, 100, 180), (2, 110, 140)]]
        self.assertEqual(binary_counts(b''.join(packets), 352),
                         {42: {'records': 2, 'kernel_start_ns': 100, 'kernel_end_ns': 180}})
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            binary_counts(packets[0]*2, 352)
        with self.assertRaisesRegex(ValueError, 'ABI/length'):
            binary_counts(packets[0][:-1], 352)


if __name__ == '__main__':
    unittest.main()
