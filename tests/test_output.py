"""--json and --event-dir: machine-readable output from iss_pdu_utility."""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from equipment_drivers.pb.equipment.v1 import equipment_pb2 as pb
except Exception:  # protobuf missing or older than the generated stubs
    pb = None


def run_utility(*args, env=None):
    return subprocess.run([sys.executable, "iss_pdu_utility", *args], capture_output=True, text=True, cwd=REPO,
                          env={**os.environ, "PYTHONPATH": REPO, **(env or {})})


class TestJsonOutput(unittest.TestCase):
    def test_success_with_verify(self):
        res = run_utility("--buyoff", "--model", "apc_ap7900", "--json", "--verify", "off", "1-2")
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(res.stdout)  # stdout is exactly one JSON document
        self.assertTrue(doc["success"])
        self.assertEqual(doc["model"], "APC AP7900 Switched Rack PDU")
        self.assertIn("Network Management Card", doc["raw_connection"])
        self.assertEqual([r["channel"] for r in doc["results"]], [1, 2])
        self.assertEqual(doc["results"][0]["raw"], "E000: Success")
        self.assertEqual(doc["results"][1]["verify"]["status"], "OFF")
        self.assertTrue(doc["results"][1]["verify"]["confirmed"])
        self.assertEqual(doc["errors"], [])

    def test_device_error_is_reported_in_json(self):
        res = run_utility("--buyoff", "--model", "raritan_px3_5460", "--fault", "command_error", "--json", "on", "1")
        self.assertEqual(res.returncode, 1)
        doc = json.loads(res.stdout)
        self.assertFalse(doc["success"])
        self.assertFalse(doc["results"][0]["success"])
        self.assertIn('"_ret_": 1', doc["results"][0]["raw"])
        self.assertTrue(any("failed" in e for e in doc["errors"]))

    def test_unreachable_pdu_still_yields_json(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]
        res = run_utility("--ip_address", "127.0.0.1", "--port", str(closed_port), "--json", "status", "1")
        self.assertEqual(res.returncode, 1)
        doc = json.loads(res.stdout)
        self.assertFalse(doc["success"])
        self.assertEqual(doc["results"], [])
        self.assertIn("No PDU answered", doc["errors"][0])

    def test_capture_notice_goes_to_stderr_under_json(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        res = run_utility("--buyoff", "--model", "wti_vmr_hd4d20", "--json", "--capture", tmp, "status", "1")
        json.loads(res.stdout)
        self.assertIn("Captured", res.stderr)


@unittest.skipIf(pb is None, "protobuf runtime not installed")
class TestEventDir(unittest.TestCase):
    def test_one_event_per_channel(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        res = run_utility("--buyoff", "--model", "wti_vmr_hd4d20", "--ip_address", "192.168.1.40",
                          "--event-dir", tmp, "off", "2-3", env={"STATION_ID": "PPTR-V2-004"})
        self.assertEqual(res.returncode, 0, res.stderr)
        events = []
        for path in glob.glob(os.path.join(tmp, "*.pb")):
            event = pb.EquipmentEvent()
            with open(path, "rb") as f:
                event.ParseFromString(f.read())
            events.append(event)
        self.assertEqual(sorted(e.pdu_response.channel for e in events), [2, 3])
        event = events[0]
        self.assertEqual(event.station_id, "PPTR-V2-004")
        self.assertEqual(event.ip_address, "192.168.1.40")
        self.assertEqual(event.equipment_type, pb.EQUIPMENT_TYPE_PDU)
        self.assertEqual(event.pdu_response.action, pb.ACTION_TURN_OFF)
        self.assertEqual(event.pdu_response.model, "WTI VMR-HD4D20 C19")
        self.assertIn('"powerplugs"', event.pdu_response.raw)


if __name__ == "__main__":
    unittest.main()
