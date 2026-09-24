"""Unit tests for Jetson GPU utilization parsing (no hardware required)."""
import unittest
from pathlib import Path
from unittest import mock
import subprocess
import agents.local as local


class GpuUtilizationTests(unittest.TestCase):
    def test_sysfs_millipercent(self):
        with mock.patch.object(Path, 'read_text', return_value='725'):
            self.assertEqual(local._read_sysfs_gpu_load('/x'), 72.5)

    def test_sysfs_already_percent(self):
        with mock.patch.object(Path, 'read_text', return_value='45'):
            self.assertEqual(local._read_sysfs_gpu_load('/x'), 45.0)

    def test_sysfs_out_of_range_is_none(self):
        with mock.patch.object(Path, 'read_text', return_value='5000'):
            self.assertIsNone(local._read_sysfs_gpu_load('/x'))

    def test_sysfs_unreadable_is_none(self):
        with mock.patch.object(Path, 'read_text', side_effect=OSError):
            self.assertIsNone(local._read_sysfs_gpu_load('/x'))

    def test_tegrastats_parse(self):
        sample = 'RAM 1000/7000MB GR3D_FREQ 33%@1147 EMC_FREQ 20%'
        with mock.patch.object(local.shutil, 'which', return_value='/usr/bin/tegrastats'):
            with mock.patch.object(
                local.subprocess, 'run',
                side_effect=subprocess.TimeoutExpired(cmd=['tegrastats'], timeout=2.0, output=sample),
            ):
                self.assertEqual(local._read_tegrastats_gpu(), 33.0)

    def test_gpu_prefers_hint_sysfs(self):
        with mock.patch.object(local, 'GPU_LOAD_HINTS', ('/hint',)):
            with mock.patch.object(local, '_read_sysfs_gpu_load', side_effect=lambda p: 12.0 if p == '/hint' else None):
                with mock.patch.object(local, '_read_tegrastats_gpu', return_value=99.0):
                    self.assertEqual(local.gpu_utilization_percent(), 12.0)

    def test_metrics_includes_gpu_field(self):
        with mock.patch.object(local, 'gpu_utilization_percent', return_value=7.0):
            with mock.patch.object(local.os, 'getloadavg', return_value=(0.1, 0.1, 0.1), create=True):
                with mock.patch.object(Path, 'read_text', side_effect=OSError):
                    m = local.metrics()
                    self.assertEqual(m['gpu_utilization_percent'], 7.0)


if __name__ == '__main__':
    unittest.main()
