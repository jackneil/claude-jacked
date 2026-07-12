"""Tests for jacked.service.migrate — baked --host extraction, host-free
rewriting, the host->setting mapping, and the guarded DB migration."""

import logging

import pytest

from jacked.service.migrate import (
    extract_baked_host,
    map_host_to_setting,
    migrate_baked_host_to_db,
    remote_access_configured,
    strip_baked_host,
)


def _plist(host: str | None, port: int = 8321) -> str:
    host_pair = (
        f"        <string>--host</string>\n        <string>{host}</string>\n"
        if host is not None
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        "    <string>ai.hank.jacked</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        "        <string>/usr/local/bin/jacked</string>\n"
        "        <string>service</string>\n"
        "        <string>start</string>\n"
        f"{host_pair}"
        "        <string>--port</string>\n"
        f"        <string>{port}</string>\n"
        "    </array>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _vbs_pythonw(host: str | None, port: int = 8321) -> str:
    host_part = f" --host {host}" if host is not None else ""
    return (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.Run """C:\\Py\\pythonw.exe"" -m jacked service start'
        f'{host_part} --port {port}", 0, False\n'
    )


def _vbs_exe(host: str | None, port: int = 8321) -> str:
    host_part = f" --host {host}" if host is not None else ""
    return (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.Run """C:\\bin\\jacked.exe"" service start'
        f'{host_part} --port {port}", 0, False\n'
    )


class TestExtractBakedHostPlist:
    @pytest.mark.parametrize("host", ["0.0.0.0", "100.77.1.2", "127.0.0.1"])
    def test_extracts_host(self, host):
        assert extract_baked_host(_plist(host), "plist") == host

    def test_no_host_pair_returns_none(self):
        assert extract_baked_host(_plist(None), "plist") is None

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not xml at all",
            "<plist><string>--host</string>",  # pair truncated
            "<string>--host</string><string></string>",  # empty value
            "\x00\x01 binary garbage \xff",
        ],
    )
    def test_malformed_returns_none_without_raising(self, text):
        assert extract_baked_host(text, "plist") is None

    def test_none_input_does_not_raise(self):
        assert extract_baked_host(None, "plist") is None

    def test_unknown_kind_returns_none(self):
        assert extract_baked_host(_plist("0.0.0.0"), "systemd") is None


class TestExtractBakedHostVbs:
    @pytest.mark.parametrize("builder", [_vbs_pythonw, _vbs_exe])
    @pytest.mark.parametrize("host", ["0.0.0.0", "100.77.1.2", "127.0.0.1"])
    def test_extracts_host_both_branches(self, builder, host):
        assert extract_baked_host(builder(host), "vbs") == host

    @pytest.mark.parametrize("builder", [_vbs_pythonw, _vbs_exe])
    def test_hostfree_returns_none_both_branches(self, builder):
        assert extract_baked_host(builder(None), "vbs") is None

    def test_host_at_end_of_command_is_clean(self):
        # No trailing --port: the host is immediately followed by the VBS
        # quote/comma tail, which must not leak into the extracted value.
        text = (
            'Set WshShell = CreateObject("WScript.Shell")\n'
            'WshShell.Run """C:\\bin\\jacked.exe"" service start'
            ' --host 0.0.0.0", 0, False\n'
        )
        assert extract_baked_host(text, "vbs") == "0.0.0.0"

    @pytest.mark.parametrize("text", ["", "WshShell garbage", "--host"])
    def test_malformed_returns_none_without_raising(self, text):
        assert extract_baked_host(text, "vbs") is None


class TestStripBakedHost:
    @pytest.mark.parametrize("host", ["0.0.0.0", "100.77.1.2", "127.0.0.1"])
    def test_plist_strip_removes_only_the_host_pair(self, host):
        stripped = strip_baked_host(_plist(host), "plist")
        assert "--host" not in stripped
        # Everything else preserved byte-for-byte (binary, port, structure).
        assert stripped == _plist(None)

    def test_plist_strip_is_idempotent_on_hostfree_text(self):
        assert strip_baked_host(_plist(None), "plist") == _plist(None)

    @pytest.mark.parametrize("builder", [_vbs_pythonw, _vbs_exe])
    def test_vbs_strip_removes_host_keeps_port(self, builder):
        stripped = strip_baked_host(builder("0.0.0.0"), "vbs")
        assert "--host" not in stripped
        assert stripped == builder(None)

    def test_vbs_strip_is_idempotent_on_hostfree_text(self):
        assert strip_baked_host(_vbs_exe(None), "vbs") == _vbs_exe(None)

    def test_unknown_kind_returns_text_unchanged(self):
        text = _plist("0.0.0.0")
        assert strip_baked_host(text, "systemd") == text


class TestMapHostToSetting:
    def test_all_interfaces(self):
        assert map_host_to_setting("0.0.0.0") == ("true", "all")

    @pytest.mark.parametrize("host", ["100.64.0.1", "100.77.1.2", "100.127.255.254"])
    def test_tailscale_cgnat_range(self, host):
        assert map_host_to_setting(host) == ("true", "tailscale")

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
    def test_loopback(self, host):
        assert map_host_to_setting(host) == ("false", None)

    @pytest.mark.parametrize(
        "host",
        ["192.168.1.5", "10.0.0.7", "100.128.0.1", "example.ts.net", "::1"],
    )
    def test_unmapped_specific_ip_returns_none(self, host):
        assert map_host_to_setting(host) is None


@pytest.fixture()
def db():
    from jacked.web.database import Database

    return Database(":memory:")


class TestMigrateBakedHostToDb:
    def test_all_interfaces_writes_enabled_all_when_absent(self, db):
        msg = migrate_baked_host_to_db("0.0.0.0", db=db)
        assert db.get_setting("remote_access_enabled") == "true"
        assert db.get_setting("remote_access_scope") == "all"
        assert "migrated" in msg

    def test_tailscale_ip_writes_enabled_tailscale_when_absent(self, db):
        msg = migrate_baked_host_to_db("100.77.1.2", db=db)
        assert db.get_setting("remote_access_enabled") == "true"
        assert db.get_setting("remote_access_scope") == "tailscale"
        assert "migrated" in msg

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
    def test_loopback_never_writes(self, db, host):
        msg = migrate_baked_host_to_db(host, db=db)
        assert db.get_setting("remote_access_enabled") is None
        assert db.get_setting("remote_access_scope") is None
        assert "loopback" in msg

    def test_unmapped_ip_never_writes_and_info_logs(self, db, caplog):
        with caplog.at_level(logging.INFO, logger="jacked.service.migrate"):
            msg = migrate_baked_host_to_db("192.168.1.5", db=db)
        assert db.get_setting("remote_access_enabled") is None
        assert db.get_setting("remote_access_scope") is None
        assert "one-shot" in msg
        assert any("one-shot" in r.getMessage() for r in caplog.records)

    def test_never_overwrites_existing_enabled_false(self, db):
        """CRITICAL guard: a stale artifact must never clobber a GUI choice.
        The user turned remote access OFF in the dashboard; a leftover
        0.0.0.0 plist must not turn it back on during an upgrade."""
        db.set_setting("remote_access_enabled", "false")
        msg = migrate_baked_host_to_db("0.0.0.0", db=db)
        assert db.get_setting("remote_access_enabled") == "false"
        assert db.get_setting("remote_access_scope") is None
        assert "already present" in msg

    def test_never_overwrites_existing_enabled_true_scope(self, db):
        """Scope chosen in the GUI survives a stale artifact with a different
        implied scope."""
        db.set_setting("remote_access_enabled", "true")
        db.set_setting("remote_access_scope", "tailscale")
        migrate_baked_host_to_db("0.0.0.0", db=db)
        assert db.get_setting("remote_access_enabled") == "true"
        assert db.get_setting("remote_access_scope") == "tailscale"

    def test_db_error_is_swallowed_and_described(self):
        class _BoomDb:
            def get_setting(self, key):
                raise RuntimeError("db is toast")

            def set_setting(self, key, value):  # pragma: no cover - never reached
                raise AssertionError("must not write")

        msg = migrate_baked_host_to_db("0.0.0.0", db=_BoomDb())  # must not raise
        assert "could not migrate" in msg

    def test_scope_written_before_guard_key_so_partial_write_self_heals(self):
        """The guard key `remote_access_enabled` must be written LAST. If the
        scope write is the one that lands but enabled fails, the guard stays
        absent so a later boot retries cleanly instead of sticking a half-applied
        intent (enabled set, scope missing -> silently downgraded to default)."""
        writes = []

        class _PartialDb:
            def get_setting(self, key):
                return None  # key absent -> migration proceeds

            def set_setting(self, key, value):
                writes.append(key)
                if key == "remote_access_enabled":
                    raise RuntimeError("write failed after scope landed")

        msg = migrate_baked_host_to_db("0.0.0.0", db=_PartialDb())  # must not raise
        assert "could not migrate" in msg
        # scope was attempted BEFORE the guard key (so the guard never landed).
        assert writes == ["remote_access_scope", "remote_access_enabled"]


class TestRemoteAccessConfigured:
    def test_absent_key_is_unconfigured(self, db):
        assert remote_access_configured(db=db) is False

    def test_present_enabled_true_is_configured(self, db):
        db.set_setting("remote_access_enabled", "true")
        assert remote_access_configured(db=db) is True

    def test_present_enabled_false_is_still_configured(self, db):
        # 'false' is a deliberate GUI/CLI choice -> the DB is authoritative.
        db.set_setting("remote_access_enabled", "false")
        assert remote_access_configured(db=db) is True

    def test_db_error_treated_as_unconfigured_without_raising(self):
        class _BoomDb:
            def get_setting(self, key):
                raise RuntimeError("db is toast")

        assert remote_access_configured(db=_BoomDb()) is False
