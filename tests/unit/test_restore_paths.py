import pytest

from repoharbor.errors import HarborError
from repoharbor.restore import aliases_protected_mount


def test_detects_aliases_of_nested_backup_mounts(tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_bytes(
        b"1 0 0:3 / / rw - overlay none rw\n"
        b"2 1 8:1 /data/backup\\040space /backup\\040space ro - ext4 /dev/sda1 ro\n"
        b"3 2 8:2 / /backup\\040space/nested ro - ext4 /dev/sda2 ro\n"
        b"4 1 8:1 /data/backup\\040space/manual /exports rw - ext4 /dev/sda1 rw\n"
        b"5 1 8:2 /nested-alias /alias rw - ext4 /dev/sda2 rw\n"
        b"6 1 8:1 /data/exports /other rw - ext4 /dev/sda1 rw\n"
    )
    from pathlib import Path

    protected = (Path("/backup space"), Path("/secrets"))
    assert aliases_protected_mount(Path("/exports/project"), protected, mountinfo)
    assert aliases_protected_mount(Path("/alias"), protected, mountinfo)
    assert not aliases_protected_mount(Path("/other"), protected, mountinfo)
    mountinfo.write_bytes(b"")
    with pytest.raises(HarborError):
        aliases_protected_mount(Path("/other"), protected, mountinfo)
