"""The install key round-trips byte for byte on every platform.

Windows opens a file in text mode unless O_BINARY is set: byte 0x1a ends a
read and ``\\r\\n`` collapses to ``\\n``. A random 256-bit key contains 0x1a
about one time in nine, so the provider must read and write in binary mode.
"""

import os

from jacked.credentials.key import FileInstallKeyProvider


def test_a_key_with_every_control_byte_reads_back_intact(tmp_path):
    path = tmp_path / "credential-recovery.key"
    key = bytes(range(32))  # holds 0x0a, 0x0d, 0x1a
    path.write_bytes(key)
    if os.name != "nt":
        path.chmod(0o600)
    assert FileInstallKeyProvider(path).get_key() == key


def test_a_created_key_reads_back_intact(tmp_path):
    path = tmp_path / "credential-recovery.key"
    provider = FileInstallKeyProvider(path)
    created = provider.get_key()
    assert created is not None and len(created) == 32
    assert FileInstallKeyProvider(path).get_key() == created
    assert path.read_bytes() == created
