import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('entry_compare',Path(__file__).with_name('compare.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class EntryFeatureTests(unittest.TestCase):
    def test_result_does_not_enter_features(self):
        row={'scalar_args':{'open_flags':524288},'path_status_name':'observed','path':'pkg/a.py'}
        self.assertEqual(module.features(row),module.features(row | {'ret':-1,'status':'failure','duration_ns':100000,'end_ns':4444}))
        self.assertEqual(module.features(row)[2],'.py')

    def test_group_needs_two_training_instances(self):
        feat=('entry_path_relative',0,'.py')
        rows=[(0,feat,10,0,0,0)]*20
        self.assertEqual(module.fit(rows,'flags')[1],{})
        rows += [(1,feat,20,0,0,0)]*20
        self.assertEqual(module.fit(rows,'flags')[1],{(0,):15})

    def test_serialization_preserves_prediction_keys(self):
        saved=module.serialize((20,{(0,'.py'):10}))
        restored={tuple(g['key']):g['prediction_ns'] for g in saved['groups']}
        self.assertEqual(restored,{(0,'.py'):10})


if __name__=='__main__':unittest.main()
