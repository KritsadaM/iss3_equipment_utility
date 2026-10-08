import unittest
import subprocess
import sys
import json
import os

PYTHON = sys.executable

class TestPduUtility(unittest.TestCase):
    def run_utility(self, args):
        cmd = [PYTHON, "iss_pdu_utility"] + args
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        return result

    def test_missing_ip_without_buyoff_fails(self):
        res = self.run_utility(["status", "1"])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--ip_address (unless in Buy-off mode)", res.stderr)

    def test_buyoff_default_apc(self):
        res = self.run_utility(["--buyoff", "status", "1"])
        self.assertEqual(res.returncode, 0, msg=f"Failed with stderr: {res.stderr}")
        self.assertIn("Detected Model: APC AP7900 Switched Rack PDU", res.stdout)
        self.assertIn("Channel 1 Status: ON", res.stdout)

        # APC is driven over the NMC CLI: RAW_CONNECTION is the SSH login banner,
        # RAW_OUTPUT is the CLI's text reply.
        conn = res.stdout.split("RAW_CONNECTION:\n")[1].split("Channel 1 Status:")[0]
        self.assertIn("Network Management Card", conn)
        self.assertIn("RAW_OUTPUT:\nE000: Success\n 1: Outlet 1: On", res.stdout)

    def test_buyoff_wti_by_ip(self):
        res = self.run_utility(["--buyoff", "--ip_address", "192.168.1.40", "status", "1"])
        self.assertEqual(res.returncode, 0, msg=f"Failed with stderr: {res.stderr}")
        self.assertIn("Detected Model: WTI VMR-HD4D20 C19", res.stdout)
        self.assertIn("RAW_CONNECTION:\n{", res.stdout)
        self.assertIn("Channel 1 Status: ON", res.stdout)
        self.assertIn("RAW_OUTPUT:\n{", res.stdout)

        conn_json_str = res.stdout.split("RAW_CONNECTION:\n")[1].split("Channel 1 Status:")[0].strip()
        data = json.loads(conn_json_str)
        self.assertEqual(data.get("product"), "VMR-HD4D20")
        self.assertEqual(data.get("status"), {"code": "0", "text": "OK"})

    def test_buyoff_raritan_by_ip(self):
        res = self.run_utility(["--buyoff", "--ip_address", "192.168.1.61", "status", "1"])
        self.assertEqual(res.returncode, 0, msg=f"Failed with stderr: {res.stderr}")
        self.assertIn("Detected Model: Raritan PX2-5460 Switched PDU", res.stdout)
        self.assertIn("RAW_CONNECTION:\n{", res.stdout)
        self.assertIn("Channel 1 Status: ON", res.stdout)
        self.assertIn("RAW_OUTPUT:\n{", res.stdout)

    def test_buyoff_model_override(self):
        res = self.run_utility(["--buyoff", "--model", "raritan_px3_5460", "status", "1"])
        self.assertEqual(res.returncode, 0, msg=f"Failed with stderr: {res.stderr}")
        self.assertIn("Detected Model: Raritan PX3-5460 Switched PDU", res.stdout)
        self.assertIn("RAW_CONNECTION:\n{", res.stdout)

    def test_buyoff_multi_channel(self):
        res = self.run_utility(["--buyoff", "status", "1-2"])
        self.assertEqual(res.returncode, 0, msg=f"Failed with stderr: {res.stderr}")
        self.assertIn("Channel 1 Status: ON", res.stdout)
        self.assertIn("Channel 2 Status: ON", res.stdout)

    def test_buyoff_turn_off_action(self):
        res = self.run_utility(["--buyoff", "off", "1"])
        self.assertEqual(res.returncode, 0, msg=f"Failed with stderr: {res.stderr}")
        self.assertIn("Channel 1 turned OFF successfully.", res.stdout)
        self.assertIn("RAW_OUTPUT:\nE000: Success", res.stdout)

    def test_mock_alias_backward_compatibility(self):
        res = self.run_utility(["--mock", "status", "1"])
        self.assertEqual(res.returncode, 0, msg=f"Failed with stderr: {res.stderr}")
        self.assertIn("RAW_CONNECTION:\n", res.stdout)
        self.assertIn("RAW_OUTPUT:\nE000: Success", res.stdout)

if __name__ == '__main__':
    unittest.main()
