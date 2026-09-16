"""Synthetic acceptance regressions; no historical model or holdout is loaded."""
import copy
import unittest
from scripts.validation.check_d9_predictions import assess


def fixture():
    expected = [dict(event_id=str(i), case_id='fixture', kind='cpu' if i < 8 else 'model' if i == 8 else 'e2e',
                     observed_ms=100, availability='measured') for i in range(10)]
    predicted = [dict(event_id=r['event_id'], case_id='fixture', kind=r['kind'], predicted_ms=100) for r in expected]
    return expected, predicted


class IndividualAcceptanceTests(unittest.TestCase):
    def test_mean_error_cannot_hide_one_bad_individual_event(self):
        e, p = fixture()
        p[0]['predicted_ms'] = 200
        result = assess(e, p)
        self.assertEqual(result['mean_ape_diagnostic_only'], 10)
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['failed_or_unscorable_count'], 1)

    def test_boundary_and_independent_e2e_gate(self):
        e, p = fixture()
        p[-1]['predicted_ms'] = 125
        self.assertEqual(assess(e, p)['status'], 'pass')
        p[-1]['predicted_ms'] = 125.001
        self.assertEqual(assess(e, p)['status'], 'fail')

    def test_missing_unsupported_and_nonfinite_predictions_never_disappear(self):
        for value in (None, float('nan'), float('inf'), -1, True):
            e, p = fixture()
            p[0]['predicted_ms'] = value
            self.assertEqual(assess(e, p)['status'], 'fail')
        e, p = fixture()
        self.assertEqual(assess(e, p[1:])['status'], 'fail')

    def test_zero_or_censored_observation_is_explicitly_unscorable(self):
        e, p = fixture()
        e[0]['observed_ms'] = 0
        result = assess(e, p)
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['individual_results'][0]['reason'], 'unscorable_observation_or_censored_event')
        e, p = fixture()
        e[0]['availability'] = 'censored'
        self.assertEqual(assess(e, p)['status'], 'fail')

    def test_duplicate_mismatched_and_unexpected_identities_rejected(self):
        e, p = fixture()
        with self.assertRaises(ValueError):
            assess(e, p + [copy.deepcopy(p[0])])
        p[0]['case_id'] = 'other'
        with self.assertRaises(ValueError):
            assess(e, p)
        e, p = fixture()
        p.append(dict(event_id='extra', case_id='fixture', kind='cpu', predicted_ms=100))
        self.assertEqual(assess(e, p)['status'], 'fail')
