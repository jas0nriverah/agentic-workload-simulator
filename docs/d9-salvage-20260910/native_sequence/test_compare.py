import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('sequence_compare',Path(__file__).with_name('compare.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FeatureContractTests(unittest.TestCase):
    def test_result_and_timing_changes_cannot_change_design(self):
        row = {'prompt_tokens':1000,'completion_tokens':50,'cached_tokens':500,
               'start_ordinal':0,'observed_ms':30,'status':'success'}
        for candidate in ('token','token_first','cache','cache_first'):
            self.assertEqual(module.design(row,candidate),module.design(
                row | {'observed_ms':99999,'status':'failure','residual_ms':888},candidate))

    def test_first_indicator_does_not_depend_on_final_request_count(self):
        row = {'prompt_tokens':1000,'completion_tokens':50,'cached_tokens':500,'start_ordinal':0}
        self.assertEqual(module.design(row,'token_first')[-1],1)
        self.assertEqual(module.design(row | {'start_ordinal':1},'token_first')[-1],0)
        self.assertEqual(module.design(row | {'total_requests':1000},'token_first'),module.design(row,'token_first'))


if __name__ == '__main__':unittest.main()
