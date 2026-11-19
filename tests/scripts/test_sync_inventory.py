import os

from scripts.sync_inventory import _recently_synchronized


def test_recent_success_marker_throttles_redundant_inventory_scan(tmp_path):
    marker = tmp_path / "inventory-sync.stamp"
    assert not _recently_synchronized(marker, now=1_000)

    marker.touch()
    os.utime(marker, (900, 900))

    assert _recently_synchronized(marker, now=1_000, interval=300)
    assert not _recently_synchronized(marker, now=1_201, interval=300)
