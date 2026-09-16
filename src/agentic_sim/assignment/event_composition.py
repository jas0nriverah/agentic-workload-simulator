"""Compose inclusive event predictions for an explicitly supplied serial trace.

Parent predictions include their children. Children remain individual scoring
targets but are never added twice to E2E. This module has no measured labels,
timestamps, hardware scaling assumptions, or residual correction input.
"""
from math import isfinite


def compose_serial(nodes, predictions):
    """Return root-event sum and explicit missing predictions; reject bad trees."""
    indexed = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {'event_id', 'parent_event_id', 'event_class'}:
            raise ValueError('nodes require exactly event_id, parent_event_id, event_class')
        identity = node['event_id']
        if not isinstance(identity, str) or not identity or identity in indexed:
            raise ValueError('missing or duplicate event identity')
        if not isinstance(node['event_class'], str) or not node['event_class']:
            raise ValueError('missing event class')
        indexed[identity] = node
    if not indexed:
        raise ValueError('empty trace is not a complete execution')
    for node in nodes:
        seen = {node['event_id']}
        parent = node['parent_event_id']
        while parent is not None:
            if not isinstance(parent, str) or parent not in indexed:
                raise ValueError('missing parent identity')
            if parent in seen:
                raise ValueError('cyclic event ancestry')
            seen.add(parent)
            parent = indexed[parent]['parent_event_id']
    if set(predictions) - set(indexed):
        raise ValueError('prediction without matching event')
    for value in predictions.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
            raise ValueError('predictions must be finite nonnegative numbers')
    roots = [n['event_id'] for n in nodes if n['parent_event_id'] is None]
    missing = sorted(set(indexed) - set(predictions))
    missing_roots = sorted(set(roots) - set(predictions))
    return {'predicted_accounted_ms': None if missing_roots else sum(predictions[k] for k in roots),
            'root_event_ids': roots, 'missing_event_ids': missing,
            'all_events_predicted': not missing, 'e2e_coverage_established': False,
            'contract': 'inclusive parents, serial roots; unknown execution gaps are not predicted'}
