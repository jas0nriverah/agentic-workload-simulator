"""Identity-only fixtures: no historical labels or raw source access."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.assignment.historical_analysis_scope import (
    ScopeError, build_scope, frozen_scope, main, validate_panel,
    ASSIGNMENT_ROOT, PANEL_PATH,
)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.v1 = {'schema_version': 'assignment.event-split-manifest.v1',
                   'calibration_run_ids': ['good'], 'holdout_run_ids': ['sealed-old', 'live']}
        self.old = self.bind('old.json', self.v1)
        self.v2 = {
            'schema_version': 'assignment.event-split-manifest.v2',
            'holdout_instance_id': 'repo__repo-1',
            'historical_split': {'path': str(self.old[0]), 'sha256': self.old[1],
                                 'preserved_holdout_run_ids': ['sealed-old']},
            'records': [dict(self.row('repo__repo-1', 'sealed-old'),
                             assignment='holdout', record_type='split_record',
                             schema_version='assignment.event-split-manifest.v2')],
        }
        partitions = ['sealed_holdout', 'final_evaluation', 'train_calibration',
                      'confirmation_development_excluded']
        clusters = [dict(self.row(f'repo__repo-{i}', None), partition=p)
                    for i, p in enumerate(partitions, 1)]
        self.production = {
            'schema_version': 'assignment-production-instance-cluster-split.v2',
            'clusters': clusters,
            'case_assignments': [dict(r, case_id=f'new-{i}', historical_template_case_id=f'old-{i}')
                                 for i, r in enumerate(clusters, 1)],
            'sealed_holdout': clusters[0],
            'partition_counts': {p: {'case_count': 1, 'cluster_count': 1} for p in partitions},
        }

    def row(self, instance, case):
        r = {'instance_id': instance, 'cluster_id': 'instance:' + instance}
        if case:
            r['case_id'] = case
        return r

    def bind(self, name, value):
        path = self.root / name
        raw = json.dumps(value).encode()
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def scope(self):
        return build_scope([self.old, self.bind('v2.json', self.v2),
                            self.bind('production.json', self.production)])

    def test_exact_schemas_union_and_aliases(self):
        scope = self.scope()
        self.assertEqual(scope.excluded_instance_ids, {'repo__repo-1', 'repo__repo-2'})
        for identity in [{'run_id': 'live'}, {'case_id': 'old-2'},
                         {'run_id': 'new-1'}, {'instance_id': 'repo__repo-2', 'run_id': 'fresh-attempt'}]:
            self.assertFalse(scope.is_eligible(identity))
        self.assertTrue(scope.is_eligible({'run_id': 'new-3'}))
        self.assertTrue(scope.is_eligible({'instance_id': 'repo__repo-4'}))
        with self.assertRaises(ScopeError):
            scope.is_eligible({'run_id': 'unknown'})
        with self.assertRaises(ScopeError):
            scope.is_eligible({'instance_id': 'repo__repo-4', 'run_id': 'new-3'})

    def test_filter_precedes_source_and_label_lookup(self):
        class Identity(dict):
            def __getitem__(self, key):
                if key in {'source_path', 'raw_label'}:
                    raise AssertionError('premature sensitive access')
                return super().__getitem__(key)
        denied = Identity(instance_id='repo__repo-2', source_path='never-open', raw_label='never-read')
        accepted = {'instance_id': 'repo__repo-3'}
        calls = []
        def loader(identity):
            calls.append(identity)
            return 'loaded'
        self.assertEqual(list(self.scope().load_eligible([denied, accepted], loader)), ['loaded'])
        self.assertEqual(calls, [accepted])

    def test_unknown_partitions_and_malformed_schemas(self):
        changes = [
            lambda: self.production['clusters'][1].update(partition='unknown'),
            lambda: self.production['case_assignments'][1].update(partition='unknown'),
            lambda: self.production.update(clusters=[]),
            lambda: self.production['case_assignments'][1].update(cluster_id='bad'),
            lambda: self.v2['records'][0].update(assignment='unknown'),
            lambda: self.v2.update(records={}),
            lambda: self.v2['records'][0].pop('case_id'),
            lambda: self.production.update(schema_version='unknown'),
            lambda: self.production['partition_counts'].update(unknown={}),
        ]
        for change in changes:
            with self.subTest(change=change):
                v2, production = copy.deepcopy(self.v2), copy.deepcopy(self.production)
                change()
                with self.assertRaises(ScopeError):
                    self.scope()
                self.v2, self.production = v2, production

    def test_hash_and_json_fail_closed(self):
        self.old[0].write_text('{}')
        with self.assertRaises(ScopeError):
            self.scope()
        for raw in [b'{', b'{"schema_version":"x","schema_version":"y"}', b'[]']:
            p = self.root / 'bad.json'
            p.write_bytes(raw)
            with self.assertRaises(ScopeError):
                build_scope([(p, hashlib.sha256(raw).hexdigest())])
        with self.assertRaises(ScopeError):
            build_scope([])

    def test_v1_malformed(self):
        for value in [None, 'live', [None], [{}]]:
            self.v1['holdout_run_ids'] = value
            with self.assertRaises(ScopeError):
                build_scope([self.bind('bad-v1.json', self.v1)])

    def test_panel_disjointness(self):
        instances = [{'instance_id': f'panel-{i}', 'pilot_case_id': None} for i in range(24)]
        panel = {'schema_version': 'assignment.configuration-confirmation-panel.v1',
                 'panel': {'instance_count': 24, 'instances': instances},
                 'candidate_cases': instances * 4}
        p, h = self.bind('panel.json', panel)
        self.assertTrue(validate_panel(self.scope(), p, h)['disjoint'])
        instances[0]['instance_id'] = 'repo__repo-2'
        p, h = self.bind('panel.json', panel)
        with self.assertRaises(ScopeError):
            validate_panel(self.scope(), p, h)

    @unittest.skipUnless((ASSIGNMENT_ROOT / PANEL_PATH).exists(), 'frozen identity manifests unavailable')
    def test_frozen_report_counts_panel_and_exclusive_output(self):
        scope = frozen_scope()
        self.assertEqual(len(scope.excluded_instance_ids), 137)
        self.assertEqual(len(scope.excluded_run_ids), 451)
        output = self.root / 'scope.json'
        self.assertEqual(main(['--output', str(output)]), 0)
        saved = output.read_bytes()
        artifact = json.loads(saved)
        self.assertTrue(artifact['panel_validation']['disjoint'])
        self.assertEqual(len(artifact['manifest_inputs']), 5)
        with self.assertRaises(SystemExit):
            main(['--output', str(output)])
        self.assertEqual(output.read_bytes(), saved)


if __name__ == '__main__':
    unittest.main()
