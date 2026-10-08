import json
import os
import shutil
import tempfile
import unittest

import equipment_drivers  # noqa: F401 -- triggers driver registration
from equipment_drivers.capture import CaptureSession
from equipment_drivers.discovery import discover_and_instantiate
from equipment_drivers.pdu.apc_models import ApcAp7900Driver
from equipment_drivers.pdu.raritan_models import RaritanPx35460Driver
from equipment_drivers.simulator import MockPduServer
import requests
from equipment_drivers.cli_transport import SshCliTransport


class TestCaptureSession(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def entries(self, capture):
        with open(os.path.join(capture.path, "exchanges.jsonl")) as f:
            return [json.loads(line) for line in f]

    def read(self, capture, name):
        with open(os.path.join(capture.path, name)) as f:
            return f.read()

    def test_apc_cli_exchanges_and_no_password(self):
        with MockPduServer(vendor="apc", channel_count=8, model_name="APC AP7900 Switched Rack PDU") as server:
            with CaptureSession(self.dir, label="apc") as capture:
                driver = ApcAp7900Driver()
                driver.connect(server.host, server.port, username="ops", password="hunter2-secret")
                driver.turn_off(3)
                driver.disconnect()
        entries = self.entries(capture)
        commands = [e["request"].get("command") for e in entries]
        self.assertEqual(commands, [None, "olStatus all", "olOff 3"])  # login, outlet count, command
        self.assertEqual(entries[0]["request"], {"host": server.host, "port": server.port, "username": "ops"})
        self.assertIn("Network Management Card", self.read(capture, entries[0]["body_file"]))
        self.assertEqual(self.read(capture, entries[2]["body_file"]), "E000: Success")
        for name in os.listdir(capture.path):
            self.assertNotIn("hunter2-secret", self.read(capture, name))

    def test_raritan_jsonrpc_files_named_by_method(self):
        with MockPduServer(vendor="raritan", channel_count=30, model_name="Raritan PX3-5460 Switched PDU") as server:
            with CaptureSession(self.dir) as capture:
                driver = RaritanPx35460Driver()
                driver.connect(server.host, server.port)
                driver.get_status(1)
        files = sorted(e.get("body_file") for e in self.entries(capture))
        self.assertTrue(any(f.endswith("getMetaData.json") for f in files), files)
        self.assertTrue(any(f.endswith("outlet-0-getState.json") for f in files), files)
        body = json.loads(self.read(capture, next(f for f in files if f.endswith("getState.json"))))
        self.assertIn("_ret_", body["result"])

    def test_identification_and_failed_attempts_are_recorded(self):
        with MockPduServer(vendor="wti", channel_count=20, model_name="WTI VMR-HD4D20 C19") as server:
            with CaptureSession(self.dir) as capture:
                discover_and_instantiate(server.host, server.port, "pdu")
        entries = self.entries(capture)
        self.assertTrue(any("status/status" in e["request"].get("url", "") and e.get("status") == 200
                            for e in entries))
        self.assertTrue(any("error" in e for e in entries))  # e.g. the APC SSH attempt on an HTTP port

    def test_hooks_are_removed_afterwards(self):
        send, cli_send = requests.Session.send, SshCliTransport.send
        with CaptureSession(self.dir):
            self.assertIsNot(requests.Session.send, send)
        self.assertIs(requests.Session.send, send)
        self.assertIs(SshCliTransport.send, cli_send)

    def test_same_second_runs_get_separate_directories(self):
        first, second = CaptureSession(self.dir, "x"), None
        with first:
            pass
        second = CaptureSession(self.dir, "x")
        self.assertNotEqual(first.path, second.path)


if __name__ == "__main__":
    unittest.main()
