import importlib.util
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "nas_status_server.py"
SPEC = importlib.util.spec_from_file_location("nas_status_server", MODULE_PATH)
nas_status_server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(nas_status_server)


class CpuSamplingTests(unittest.TestCase):
    def test_parse_cpu_times_combines_busy_and_idle_counters(self):
        self.assertEqual(
            (810, 450),
            nas_status_server.parse_cpu_times("cpu  100 20 30 400 50 60 70 80 0 0\n"),
        )

    def test_cpu_percent_uses_counter_deltas(self):
        self.assertEqual(75, nas_status_server.cpu_percent_between((1000, 700), (1200, 750)))

    def test_invalid_delta_is_rejected(self):
        self.assertIsNone(nas_status_server.cpu_percent_between((1000, 700), (1000, 700)))
        self.assertIsNone(nas_status_server.cpu_percent_between((1000, 700), (900, 650)))

    def test_measurement_reads_proc_stat_twice(self):
        snapshots = [
            "cpu 100 0 100 800 0 0 0 0\n",
            "cpu 130 0 120 850 0 0 0 0\n",
        ]
        with mock.patch.object(nas_status_server, "read_text", side_effect=snapshots), mock.patch.object(
            nas_status_server.time, "sleep"
        ):
            self.assertEqual((50, "proc_stat"), nas_status_server.measure_cpu_percent("9.0", 4))

    def test_measurement_falls_back_when_proc_stat_is_unavailable(self):
        with mock.patch.object(nas_status_server, "read_text", return_value=""):
            self.assertEqual(
                (25, "load_average_fallback"),
                nas_status_server.measure_cpu_percent("1.0", 4),
            )


class ActivitySamplingTests(unittest.TestCase):
    def test_network_counters_sum_selected_interfaces(self):
        proc = """Inter-| Receive | Transmit
  eth0: 100 1 0 0 0 0 0 0 300 1 0 0 0 0 0 0
  eth1: 200 1 0 0 0 0 0 0 400 1 0 0 0 0 0 0
     lo: 900 1 0 0 0 0 0 0 900 1 0 0 0 0 0 0
"""
        self.assertEqual(
            {"rx": 100, "tx": 300},
            nas_status_server.parse_network_counters(proc, ["eth0"]),
        )

    def test_disk_counters_use_512_byte_sectors(self):
        proc = "8 0 sda 1 0 100 0 1 0 200 0 0 0 0 0 0 0 0\n"
        self.assertEqual(
            {"read": 51200, "write": 102400},
            nas_status_server.parse_disk_counters(proc, ["sda"]),
        )

    def test_counter_rates_use_elapsed_time(self):
        self.assertEqual(
            {"rx": 200, "tx": 300},
            nas_status_server.counter_rates(
                {"rx": 100, "tx": 200},
                {"rx": 500, "tx": 800},
                2,
                "rx",
                "tx",
            ),
        )

    def test_smart_attribute_returns_raw_value(self):
        payload = {"ata_smart_attributes": {"table": [{"id": 197, "raw": {"value": 3}}]}}
        self.assertEqual(3, nas_status_server.smart_attribute(payload, 197))
        self.assertEqual(0, nas_status_server.smart_attribute(payload, 198))

    def test_docker_stats_are_cached(self):
        nas_status_server.docker_stats_cache = {}
        nas_status_server.docker_stats_cache_at = 0.0
        payload = '{"Name":"demo","CPUPerc":"1.2%","MemUsage":"10MiB / 1GiB"}'
        with mock.patch.object(nas_status_server, "run", return_value=payload) as run_command, mock.patch.object(
            nas_status_server.time, "monotonic", side_effect=[100.0, 105.0]
        ):
            first = nas_status_server.load_docker_stats()
            second = nas_status_server.load_docker_stats()

        self.assertEqual(first, second)
        self.assertEqual("1.2%", second["demo"]["CPUPerc"])
        self.assertEqual(1, run_command.call_count)


if __name__ == "__main__":
    unittest.main()
