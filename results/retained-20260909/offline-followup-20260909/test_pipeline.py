import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('pipeline_under_test', HERE / 'run_pipeline.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)


class PipelineTests(unittest.TestCase):
    def test_protected_partition_is_rejected_before_ledger_load(self):
        root = '/tmp/protected-offline-fixture'
        inventory = {'errors': [], 'cases': [{'case_root': root, 'instance_id': 'i',
                     'case_id': 'c', 'derived_partition': 'final_evaluation'}]}
        with self.assertRaisesRegex(ValueError, 'prohibits journal access'):
            P.select_cases({'cases': [{'case_root': root}]}, inventory)

    def test_duplicate_case_rejected(self):
        row = {'case_root': '/tmp/duplicate-fixture', 'instance_id': 'i',
               'case_id': 'c', 'derived_partition': 'train_calibration'}
        with self.assertRaisesRegex(ValueError, 'duplicate requested'):
            P.select_cases({'cases': [row, row]}, {'errors': [], 'cases': [row]})

    def test_invalid_ledger_stops_before_statistics_and_figures(self):
        root = '/tmp/invalid-ledger-fixture'
        inv = {'errors': [], 'cases': [{'case_root': root, 'instance_id': 'i',
               'case_id': 'c', 'derived_partition': 'train_calibration'}]}
        loaded = []
        def fake_load(name, path):
            loaded.append(name)
            if name == 'followup_calibration':
                return SimpleNamespace(inventory=lambda roots: inv)
            if name == 'followup_ledger':
                return SimpleNamespace(validate_case=lambda *args: {'validation': {'status': 'invalid'}})
            raise AssertionError('downstream stage ran after invalid ledger')
        with tempfile.TemporaryDirectory() as td, patch.object(P, 'load', fake_load):
            out = Path(td) / 'new'
            with self.assertRaisesRegex(ValueError, 'ledger validation failed'):
                P.run({'cases': [{'case_root': root}]}, out)
            self.assertTrue((out / 'pipeline_failure.json').is_file())
            self.assertFalse((out / 'pipeline_report.json').exists())
            self.assertEqual(loaded, ['followup_calibration', 'followup_ledger'])

    def test_existing_output_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, 'must be new'):
                P.run({}, Path(td))


if __name__ == '__main__':
    unittest.main()
