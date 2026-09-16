import unittest
from compare import design, fit, predict

class ContractTests(unittest.TestCase):
    def test_timing_poison_does_not_change_design(self):
        model = {'input_tokens':120,'output_tokens':30}
        before = design([{}],[model])
        model.update(observed_ms=1e99, residual_ms=1e99, outcome=True)
        self.assertEqual(before,design([{'wall_ms':1e99}],[model]))

    def test_known_nonnegative_linear_law(self):
        x = [[1.,float(i)] for i in range(20)]
        y = [3.+2.*i for i in range(20)]
        for relative in (False,True):
            b = fit(x,y,relative)
            self.assertTrue(all(v>=0 for v in b))
            self.assertAlmostEqual(predict(b,[1.,9.]),21.,places=5)

if __name__ == '__main__': unittest.main()
