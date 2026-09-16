"""Frozen historical mining boundary; never use this scope as a training split.

Pass identity-only metadata to ``HistoricalScope.load_eligible`` and defer ALL
source-path resolution and label IO to its loader. Filtering already-loaded
labels cannot restore blindness. No API here opens historical raw sources.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, TypeVar

ASSIGNMENT_ROOT = Path('/home/riverahernandezjason/h100-assignment-work-20260905/assignment')
FROZEN_MANIFESTS = (
    ('submission/20260908T010000Z/d9/split_manifest.json', '960dddcb260c738afa5ba17152e1b472a60613517fa751e4116e42c944ade5e7'),
    ('submission/20260908T010000Z/d9-retry-knn-20260908T012000Z/split_manifest.json', '05aef81c19c51dc03720aa657350d391738638c48c9fcd76a8aa05e20a06f619'),
    ('submission/20260908T030000Z/d9-live/split_manifest.json', 'a3d525041344bc98ecdc42ac9c8ce3df9d39c8bce13f5446c2a658cc8d041ffe'),
    ('submission/20260908T140000Z-offline-v2/live-plan/production_split_manifest.v2.json', '0b0c37147b45ec824e2af45d82ba20b3ed57ac56b64f58ea07004e0c13bcc99f'),
    ('submission/20260908T140000Z-offline-v2/live-plan/split_manifest.json', '7b1e65b4737c9f7985e3aa82ed056f1f5a5ba388e074fbd6adce518d07200ddc'),
)
PANEL_PATH = 'submission/20260908T140000Z-offline-v2/configuration-analysis/CONFIGURATION_CONFIRMATION_PANEL.json'
PANEL_SHA256 = '1e7ed714a4d910b5bb17ab5fe596fde57736ea882b8b41cead9e666ebc04e398'
DENIED = frozenset({'sealed_holdout', 'final_evaluation', 'holdout'})
PARTITIONS = frozenset({'sealed_holdout', 'final_evaluation', 'train_calibration', 'confirmation_development_excluded'})
ID_KEYS = ('run_id', 'case_id', 'historical_template_case_id', 'resume_key', 'candidate_case_id', 'panel_case_id', 'pilot_case_id')
T = TypeVar('T')


class ScopeError(ValueError):
    """Invalid or excluded scope: callers must stop, never fall back."""


def _require(ok, message):
    if not ok:
        raise ScopeError(message)


def _string(value):
    _require(isinstance(value, str) and bool(value) and value == value.strip(), 'invalid identity/string')
    return value


def _list(value):
    _require(isinstance(value, list), 'expected list')
    return value


def _object(value):
    _require(isinstance(value, dict), 'expected object')
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _read_bound(path, expected):
    _require(isinstance(expected, str) and len(expected) == 64 and all(c in '0123456789abcdef' for c in expected), 'invalid SHA-256')
    raw = Path(path).read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == expected, f'manifest hash mismatch: {path}')
    try:
        return _object(json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda s: (_ for _ in ()).throw(ScopeError(f'invalid JSON constant: {s}'))))
    except (ValueError, TypeError) as exc:
        raise ScopeError(f'malformed JSON: {path}: {exc}') from exc


def _identity(row):
    _require(isinstance(row, Mapping), 'identity must be a mapping')
    instance = _string(row['instance_id']) if 'instance_id' in row else None
    if 'cluster_id' in row:
        cluster = _string(row['cluster_id'])
        _require(cluster.startswith('instance:') and len(cluster) > 9, 'invalid cluster_id')
        _require(instance is None or cluster == 'instance:' + instance, 'cluster/instance mismatch')
        instance = cluster[9:]
    ids = tuple(_string(row[k]) for k in ID_KEYS if k in row and not (k == 'pilot_case_id' and row[k] is None))
    _require(instance is not None or ids, 'missing identity')
    return instance, ids


@dataclass(frozen=True)
class HistoricalScope:
    excluded_instance_ids: frozenset[str]
    excluded_run_ids: frozenset[str]
    identity_by_run_id: Mapping[str, str]
    manifest_inputs: tuple[tuple[str, str], ...]

    def is_eligible(self, identity: Mapping) -> bool:
        """Check identity fields only; unknown run-only identities fail closed."""
        instance, ids = _identity(identity)
        if instance in self.excluded_instance_ids or any(i in self.excluded_run_ids for i in ids):
            return False
        resolved = {self.identity_by_run_id[i] for i in ids if i in self.identity_by_run_id}
        if instance is not None:
            resolved.add(instance)
        _require(len(resolved) == 1, 'unknown or conflicting identity')
        return next(iter(resolved)) not in self.excluded_instance_ids

    def assert_eligible(self, identity: Mapping) -> None:
        _require(self.is_eligible(identity), 'excluded historical identity')

    def load_eligible(self, identities: Iterable[Mapping], loader: Callable[[Mapping], T]) -> Iterable[T]:
        """Invoke loader only after approval; loader owns path lookup and raw IO."""
        for identity in identities:
            if self.is_eligible(identity):
                yield loader(identity)

    def artifact(self):
        inputs = [{'path': p, 'sha256': h} for p, h in self.manifest_inputs]
        return {
            'schema_version': 'assignment.historical-analysis-scope.v1',
            'manifest_inputs': inputs,
            'manifest_list_sha256': hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'excluded_instance_ids': sorted(self.excluded_instance_ids),
            'excluded_run_ids': sorted(self.excluded_run_ids),
            'excluded_instance_count': len(self.excluded_instance_ids),
            'excluded_run_count': len(self.excluded_run_ids),
            'purpose': 'historical development mining only; not training authorization',
            'prior_access_disclosure': 'Initial forensic pass accessed evaluation-cluster historical rows; filtering does not restore blindness.',
        }


def build_scope(manifest_inputs: Iterable[tuple[Path | str, str]]) -> HistoricalScope:
    """Validate caller-pinned manifests, then union cluster and explicit ID denials.

    Use ``frozen_scope`` for the complete report boundary. This lower-level API
    accepts fixtures or an explicitly reviewed replacement set of bindings.
    """
    inputs = tuple((str(Path(p).resolve()), h) for p, h in manifest_inputs)
    _require(bool(inputs) and len({p for p, _ in inputs}) == len(inputs), 'empty or duplicate manifest list')
    instances, denied_ids, aliases = set(), set(), {}
    rows = []
    for path, digest in inputs:
        d = _read_bound(path, digest)
        schema = d.get('schema_version')
        if schema == 'assignment.event-split-manifest.v1':
            _require(set(d) == {'schema_version', 'calibration_run_ids', 'holdout_run_ids'}, 'invalid v1 fields')
            calibration = {_string(i) for i in _list(d['calibration_run_ids'])}
            holdout = {_string(i) for i in _list(d['holdout_run_ids'])}
            _require(not calibration & holdout, 'overlapping v1 partitions')
            denied_ids.update(holdout)
        elif schema == 'assignment.event-split-manifest.v2':
            instances.add(_string(d.get('holdout_instance_id')))
            records = _list(d.get('records'))
            _require(bool(records), 'empty records')
            for row in records:
                row = _object(row)
                _require(row.get('schema_version') == schema and row.get('record_type') == 'split_record', 'invalid split record')
                _require(row.get('assignment') in {'development', 'holdout'}, 'unknown assignment')
                _string(row.get('case_id'))
                rows.append((row, row['assignment']))
            historical = _object(d.get('historical_split'))
            denied_ids.update(_string(i) for i in _list(historical.get('preserved_holdout_run_ids')))
            _require((str(Path(_string(historical.get('path'))).resolve()), historical.get('sha256')) in inputs, 'unbound historical split')
        elif schema == 'assignment-production-instance-cluster-split.v2':
            clusters = _list(d.get('clusters'))
            cases = _list(d.get('case_assignments'))
            _require(bool(clusters) and bool(cases), 'empty production assignments')
            partitions = {}
            for row in clusters:
                row = _object(row)
                instance, _ = _identity(row)
                _require(instance is not None and instance not in partitions, 'duplicate/missing cluster')
                _require(row.get('partition') in PARTITIONS, 'unknown partition')
                partitions[instance] = row['partition']
                rows.append((row, row['partition']))
            for row in cases:
                row = _object(row)
                instance, _ = _identity(row)
                _string(row.get('case_id'))
                _string(row.get('historical_template_case_id'))
                _require(row.get('partition') in PARTITIONS and partitions.get(instance) == row['partition'], 'unknown/conflicting case partition')
                rows.append((row, row['partition']))
            sealed = _object(d.get('sealed_holdout'))
            _require(sealed.get('partition') == 'sealed_holdout' and partitions.get(sealed.get('instance_id')) == 'sealed_holdout', 'invalid sealed holdout')
            rows.append((sealed, 'sealed_holdout'))
            counts = _object(d.get('partition_counts'))
            _require(set(counts) == PARTITIONS, 'unknown/missing partition counts')
            for partition, count in counts.items():
                _require(_object(count) == {'cluster_count': sum(v == partition for v in partitions.values()), 'case_count': sum(r['partition'] == partition for r in cases)}, 'partition count mismatch')
        else:
            raise ScopeError(f'unknown manifest schema: {schema}')
    for row, partition in rows:
        instance, ids = _identity(row)
        _require(instance is not None, 'manifest row missing instance')
        for run_id in ids:
            _require(run_id not in aliases or aliases[run_id] == instance, 'conflicting run identity')
            aliases[run_id] = instance
        if partition in DENIED:
            instances.add(instance)
            denied_ids.update(ids)
    instances.update(aliases[i] for i in denied_ids if i in aliases)
    denied_ids.update(i for i, instance in aliases.items() if instance in instances)
    return HistoricalScope(frozenset(instances), frozenset(denied_ids), MappingProxyType(aliases), inputs)


def frozen_scope(root: Path = ASSIGNMENT_ROOT) -> HistoricalScope:
    return build_scope((root / p, h) for p, h in FROZEN_MANIFESTS)


def validate_panel(scope: HistoricalScope, path: Path, sha256: str = PANEL_SHA256):
    d = _read_bound(path, sha256)
    _require(d.get('schema_version') == 'assignment.configuration-confirmation-panel.v1', 'unknown panel schema')
    panel = _object(d.get('panel'))
    rows = _list(panel.get('instances'))
    _require(panel.get('instance_count') == 24 and len(rows) == 24, 'panel must declare 24 instances')
    identities = set()
    for row in rows:
        scope.assert_eligible(row)
        identities.add(_string(row.get('instance_id')))
    _require(len(identities) == 24, 'duplicate panel instances')
    cases = _list(d.get('candidate_cases'))
    _require(len(cases) == 96, 'panel must contain 96 candidate cases')
    for row in cases:
        scope.assert_eligible(row)
        _require(row.get('instance_id') in identities, 'candidate outside panel')
    return {'path': str(path.resolve()), 'sha256': sha256, 'instance_count': 24, 'disjoint': True, 'instance_ids': sorted(identities)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--assignment-root', type=Path, default=ASSIGNMENT_ROOT)
    parser.add_argument('--panel', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='NEW JSON artifact path; existing files are refused')
    args = parser.parse_args(argv)
    try:
        scope = frozen_scope(args.assignment_root)
        artifact = scope.artifact()
        artifact['panel_validation'] = validate_panel(scope, args.panel or args.assignment_root / PANEL_PATH)
        with args.output.open('x', encoding='utf-8') as handle:
            handle.write(json.dumps(artifact, indent=2, sort_keys=True) + '\n')
    except (ScopeError, OSError) as exc:
        parser.exit(2, f'scope validation failed: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
