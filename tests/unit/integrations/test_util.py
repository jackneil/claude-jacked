"""Unit tests for agent-reach _util helpers (doctor normalization)."""

import pytest

from jacked.integrations._util import ReachDoctorShapeError, normalize_doctor


class TestNormalizeDoctor:
    def test_none_passes_through(self):
        assert normalize_doctor(None) is None

    def test_canonical_channels_list_passthrough(self):
        raw = {"channels": [{"name": "twitter", "active_backend": "twitter-cli", "status": "ok"}]}
        assert normalize_doctor(raw) is raw

    def test_slug_keyed_shape_normalized(self):
        raw = {
            "github": {"active_backend": "gh CLI", "backends": ["gh CLI"], "message": "ok",
                       "name": "GitHub", "status": "ok", "tier": 0},
            "twitter": {"active_backend": None, "backends": ["twitter-cli"], "message": "not installed",
                        "name": "Twitter/X", "status": "warn", "tier": 1},
        }
        out = normalize_doctor(raw)
        assert [c["name"] for c in out["channels"]] == ["github", "twitter"]  # tier-sorted
        gh = out["channels"][0]
        assert gh["active_backend"] == "gh CLI"
        assert gh["hint"] == "ok"
        assert gh["display_name"] == "GitHub"

    @pytest.mark.parametrize("raw", [
        [{"name": "twitter"}],                              # top-level list
        {"schema": 2, "channels": {"twitter": {}}},         # envelope (non-dict value)
        {"twitter": "twitter-cli"},                          # scalar values
        {},                                                  # empty dict
        "some string",                                       # scalar
    ])
    def test_unrecognized_shape_raises(self, raw):
        with pytest.raises(ReachDoctorShapeError):
            normalize_doctor(raw)


class TestRunDoctor:
    def _patch(self, side, which):
        from unittest import mock
        return (mock.patch("jacked.integrations._util.subprocess.run", side_effect=side),
                mock.patch("jacked.integrations._util.shutil.which", side_effect=lambda n: which.get(n)))

    def test_absent_cli(self):
        from jacked.integrations._util import run_doctor
        p1, p2 = self._patch(lambda *a, **k: None, {})
        with p1, p2:
            doctor, err = run_doctor()
        assert doctor is None and "not found" in err

    def test_nonzero_exit(self):
        import subprocess
        from jacked.integrations._util import run_doctor
        cp = subprocess.CompletedProcess(["agent-reach"], 1, "", "boom")
        p1, p2 = self._patch(lambda *a, **k: cp, {"agent-reach": "/x/agent-reach"})
        with p1, p2:
            doctor, err = run_doctor()
        assert doctor is None and "exited 1" in err

    def test_invalid_json(self):
        import subprocess
        from jacked.integrations._util import run_doctor
        cp = subprocess.CompletedProcess(["agent-reach"], 0, "not json{", "")
        p1, p2 = self._patch(lambda *a, **k: cp, {"agent-reach": "/x/agent-reach"})
        with p1, p2:
            doctor, err = run_doctor()
        assert doctor is None and "invalid JSON" in err

    def test_unknown_shape_surfaces_error(self):
        import json as _json
        import subprocess
        from jacked.integrations._util import run_doctor
        cp = subprocess.CompletedProcess(["agent-reach"], 0, _json.dumps([1, 2, 3]), "")
        p1, p2 = self._patch(lambda *a, **k: cp, {"agent-reach": "/x/agent-reach"})
        with p1, p2:
            doctor, err = run_doctor()
        assert doctor is None and "unrecognized" in err
