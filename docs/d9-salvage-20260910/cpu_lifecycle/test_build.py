import unittest
from build import bind

class BindingTests(unittest.TestCase):
    def inventory(self):
        return {'clock':{'clock_id':'CLOCK_MONOTONIC_RAW','boot_id':'cpu-boot','hostname':'cpu-host'},
                'local_cpu_profile':{'source':'local_proc_sysfs_inventory','architecture':'x86_64',
                                     'logical_cpu_count':32,'model_name':'CPU','kernel_release':'kernel','system':'Linux'}}
    def row(self):
        return {'host_id':'cpu-host','clock_id':'CLOCK_MONOTONIC_RAW|boot=cpu-boot',
                'feature_provenance':{'hardware_fingerprint':'mixed-profile'}}
    def test_remote_host_is_rejected(self):
        row=self.row();row['host_id']='gpu-host'
        with self.assertRaises(ValueError):bind(row,self.inventory(),'sha')
    def test_remote_gpu_metadata_cannot_split_cpu_domain(self):
        row=self.row();first=bind(row,self.inventory(),'one')
        row['feature_provenance']['hardware_fingerprint']='other-gpu'
        second=bind(row,self.inventory(),'two')
        self.assertEqual(first['feature_provenance']['hardware_fingerprint'],second['feature_provenance']['hardware_fingerprint'])
        self.assertEqual(second['feature_provenance']['original_profile_fingerprint'],'other-gpu')

if __name__ == '__main__':unittest.main()
