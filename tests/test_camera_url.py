"""Camera snapshots are always fetched from the printer's own address, whatever its Moonraker webcam entry says."""
import unittest

from farm.hosts import snapshot_url


class SnapshotUrl(unittest.TestCase):
    def test_paths_and_full_urls(self):
        self.assertEqual(snapshot_url("http://192.168.1.184:7128", "/webcam/?action=snapshot"),
                         "http://192.168.1.184/webcam/?action=snapshot")
        self.assertEqual(snapshot_url("http://192.168.1.184:7125", "webcam2/?action=snapshot"),
                         "http://192.168.1.184/webcam2/?action=snapshot")
        self.assertEqual(snapshot_url("http://192.168.1.200:7125", "http://192.168.1.200:8080/?action=snapshot"),
                         "http://192.168.1.200:8080/?action=snapshot")

    def test_the_nebula_pads_typo_is_repaired(self):
        self.assertEqual(snapshot_url("http://192.168.1.200:7125", "http://192.168.1.200s:8080/?action=snapshot"),
                         "http://192.168.1.200:8080/?action=snapshot")

    def test_never_another_host(self):
        self.assertEqual(snapshot_url("http://192.168.1.68:7125", "http://example.com:9/x.jpg"), "http://192.168.1.68:9/x.jpg")

    def test_stream_when_no_snapshot(self):
        self.assertEqual(snapshot_url("http://192.168.1.200:7125", "", "http://192.168.1.200:8080/?action=stream"),
                         "http://192.168.1.200:8080/?action=snapshot")


if __name__ == "__main__":
    unittest.main()
