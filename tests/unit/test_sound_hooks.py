"""Tests for sound hook generation and installation."""

from unittest.mock import patch


class TestGetSoundCommand:
    """Verify platform-specific sound command generation."""

    def test_windows_complete(self):
        """Windows complete sound uses powershell SystemSounds.

        >>> # sys.platform == "win32" -> powershell Asterisk
        """
        with patch("sys.platform", "win32"):
            from jacked.cli import _get_sound_command
            cmd = _get_sound_command("complete")
        assert "powershell" in cmd
        assert "Asterisk" in cmd
        assert "uname" not in cmd
        assert "printf" not in cmd
        assert "afplay" not in cmd

    def test_windows_notification(self):
        """Windows notification sound uses powershell Exclamation.

        >>> # sys.platform == "win32" -> powershell Exclamation
        """
        with patch("sys.platform", "win32"):
            from jacked.cli import _get_sound_command
            cmd = _get_sound_command("notification")
        assert "powershell" in cmd
        assert "Exclamation" in cmd
        assert "uname" not in cmd

    def test_darwin_complete(self):
        """macOS complete sound uses afplay Glass.

        >>> # sys.platform == "darwin" -> afplay Glass.aiff
        """
        with patch("sys.platform", "darwin"):
            from jacked.cli import _get_sound_command
            cmd = _get_sound_command("complete")
        assert "afplay" in cmd
        assert "Glass.aiff" in cmd
        assert "jacked log hook" in cmd

    def test_darwin_notification(self):
        """macOS notification sound uses afplay Basso.

        >>> # sys.platform == "darwin" -> afplay Basso.aiff
        """
        with patch("sys.platform", "darwin"):
            from jacked.cli import _get_sound_command
            cmd = _get_sound_command("notification")
        assert "afplay" in cmd
        assert "Basso.aiff" in cmd

    def test_linux_complete(self):
        """Linux complete sound uses paplay with WSL fallback.

        >>> # sys.platform == "linux" -> paplay + WSL detection
        """
        with patch("sys.platform", "linux"):
            from jacked.cli import _get_sound_command
            cmd = _get_sound_command("complete")
        assert "paplay" in cmd
        assert "complete.oga" in cmd
        assert "grep -qi microsoft" in cmd
        assert "powershell.exe" in cmd

    def test_linux_notification(self):
        """Linux notification sound uses paplay dialog-warning.

        >>> # sys.platform == "linux" -> paplay dialog-warning.oga
        """
        with patch("sys.platform", "linux"):
            from jacked.cli import _get_sound_command
            cmd = _get_sound_command("notification")
        assert "paplay" in cmd
        assert "dialog-warning.oga" in cmd

    def test_windows_no_log_command(self):
        """Windows commands skip the jacked log hook call.

        >>> # cmd.exe can't run the backgrounded log command
        """
        with patch("sys.platform", "win32"):
            from jacked.cli import _get_sound_command
            cmd = _get_sound_command("complete")
        assert "jacked log hook" not in cmd

    def test_unix_has_log_command(self):
        """macOS and Linux include the jacked log hook call.

        >>> # Unix shells can background the log command
        """
        for platform in ("darwin", "linux"):
            with patch("sys.platform", platform):
                from jacked.cli import _get_sound_command
                cmd = _get_sound_command("complete")
            assert "jacked log hook" in cmd, f"Missing log command on {platform}"


class TestReplaceStaleSoundHook:
    """Verify stale Unix-style hook detection and replacement."""

    def test_replaces_uname_hook(self):
        """Stale hooks containing uname are replaced.

        >>> # Old Unix-style hook -> new platform-specific hook
        """
        from jacked.cli import _replace_stale_sound_hook, _sound_hook_marker
        marker = _sound_hook_marker()
        entries = [{"hooks": [{"command": marker + 'OS=$(uname -s); case "$OS" in ...'}]}]

        with patch("sys.platform", "win32"):
            result = _replace_stale_sound_hook(entries, marker, "complete")

        assert result is True
        new_cmd = entries[0]["hooks"][0]["command"]
        assert "uname" not in new_cmd
        assert "powershell" in new_cmd

    def test_skips_current_hook(self):
        """Already-current hooks are not replaced.

        >>> # No uname in command -> no replacement
        """
        from jacked.cli import _replace_stale_sound_hook, _sound_hook_marker
        marker = _sound_hook_marker()
        entries = [{"hooks": [{"command": marker + 'powershell -Command "..."'}]}]

        result = _replace_stale_sound_hook(entries, marker, "complete")
        assert result is False

    def test_skips_non_jacked_hook(self):
        """Hooks without the jacked marker are untouched.

        >>> # No marker -> not our hook, skip
        """
        from jacked.cli import _replace_stale_sound_hook, _sound_hook_marker
        marker = _sound_hook_marker()
        entries = [{"hooks": [{"command": "some other hook with uname"}]}]

        result = _replace_stale_sound_hook(entries, marker, "complete")
        assert result is False

    def test_empty_entries(self):
        """Empty hook list returns False.

        >>> # Nothing to replace
        """
        from jacked.cli import _replace_stale_sound_hook, _sound_hook_marker
        marker = _sound_hook_marker()

        result = _replace_stale_sound_hook([], marker, "complete")
        assert result is False

    def test_missing_hooks_key(self):
        """Entries without 'hooks' key are handled gracefully.

        >>> # Malformed entry -> skip, don't crash
        """
        from jacked.cli import _replace_stale_sound_hook, _sound_hook_marker
        marker = _sound_hook_marker()
        entries = [{"matcher": ""}]

        result = _replace_stale_sound_hook(entries, marker, "complete")
        assert result is False
