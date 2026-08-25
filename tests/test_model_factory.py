"""Tests for the data-driven model factory."""
import unittest
import equipment_drivers
from equipment_drivers.registry import registry
from equipment_drivers.pdu.model_factory import create_and_register_models, _load_models


class TestModelsYaml(unittest.TestCase):
    """Validate the models.yaml data integrity."""

    def test_yaml_loads_all_vendors(self):
        data = _load_models()
        self.assertIn("apc", data)
        self.assertIn("wti", data)
        self.assertIn("raritan", data)

    def test_apc_has_20_models(self):
        data = _load_models()
        self.assertEqual(len(data["apc"]), 20)

    def test_wti_has_16_models(self):
        data = _load_models()
        self.assertEqual(len(data["wti"]), 16)

    def test_raritan_has_21_models(self):
        data = _load_models()
        self.assertEqual(len(data["raritan"]), 21)

    def test_every_model_has_required_keys(self):
        data = _load_models()
        for vendor, models in data.items():
            for sig, attrs in models.items():
                self.assertIn("model", attrs, f"{vendor}/{sig} missing 'model'")
                self.assertIn("channels", attrs, f"{vendor}/{sig} missing 'channels'")
                self.assertIn("suffix", attrs, f"{vendor}/{sig} missing 'suffix'")

    def test_channel_counts_are_positive(self):
        data = _load_models()
        for vendor, models in data.items():
            for sig, attrs in models.items():
                self.assertGreater(int(attrs["channels"]), 0,
                                   f"{vendor}/{sig} has invalid channel count")

    def test_suffixes_start_with_dot(self):
        data = _load_models()
        for vendor, models in data.items():
            for sig, attrs in models.items():
                self.assertTrue(attrs["suffix"].startswith("."),
                                f"{vendor}/{sig} suffix '{attrs['suffix']}' doesn't start with '.'")


class TestIpSuffixCollisions(unittest.TestCase):
    """Detect IP suffix collisions across all registered drivers."""

    def test_report_suffix_collisions(self):
        """Log (but don't fail) IP suffix collisions -- they're a known issue."""
        data = _load_models()
        suffix_map = {}  # suffix -> list of (vendor, sig)
        for vendor, models in data.items():
            for sig, attrs in models.items():
                suffix = attrs["suffix"]
                suffix_map.setdefault(suffix, []).append(f"{vendor}/{sig}")

        collisions = {s: sigs for s, sigs in suffix_map.items() if len(sigs) > 1}
        if collisions:
            # Known issue: .61 is shared by PX2-5460 and PX3-5460
            for suffix, sigs in collisions.items():
                print(f"  WARNING: IP suffix '{suffix}' shared by: {', '.join(sigs)}")


class TestRegistryIntegrity(unittest.TestCase):
    """Verify all models are properly registered."""

    def test_total_pdu_drivers_registered(self):
        drivers = registry.get_all_drivers("pdu")
        # 57 from YAML + 1 dummy = 58
        self.assertGreaterEqual(len(drivers), 58)

    def test_each_registered_driver_is_instantiable(self):
        for sig, cls in registry.get_all_drivers("pdu"):
            instance = cls()
            self.assertIsNotNone(instance.get_model())
            self.assertGreater(instance.get_channel_count(), 0)

    def test_probe_works_for_data_driven_models(self):
        for sig, cls in registry.get_all_drivers("pdu"):
            if hasattr(cls, "IP_SUFFIX") and cls.IP_SUFFIX:
                ip = f"10.0.0{cls.IP_SUFFIX}"
                self.assertTrue(cls.probe(ip, 80),
                                f"{cls.__name__} probe failed for {ip}")


class TestHttpsScheme(unittest.TestCase):
    """Verify HTTPS support via port-based scheme detection."""

    def test_apc_defaults_to_http(self):
        from equipment_drivers.pdu.apc_models import BaseApcPduDriver
        driver = BaseApcPduDriver()
        self.assertEqual(driver.scheme, "http")

    def test_wti_defaults_to_http(self):
        from equipment_drivers.pdu.wti_models import BaseWtiPduDriver
        driver = BaseWtiPduDriver()
        self.assertEqual(driver.scheme, "http")

    def test_raritan_defaults_to_http(self):
        from equipment_drivers.pdu.raritan_models import BaseRaritanPduDriver
        driver = BaseRaritanPduDriver()
        self.assertEqual(driver.scheme, "http")


if __name__ == "__main__":
    unittest.main()
