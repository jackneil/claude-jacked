"""Unit tests for the agent-reach pin-file loader/validator."""

import copy
import json

import pytest

from jacked.integrations.pinfile import (
    PinFileError,
    _spec_is_exactly_pinned,
    load_pin,
)


def _valid_pin_dict() -> dict:
    """A minimal-but-complete pin dict covering every backend kind + a note-only backend."""
    return {
        "schema_version": 1,
        "upstream": "https://github.com/Panniantong/Agent-Reach",
        "commit_sha": "b" * 40,
        "version_label": "v1.5.0",
        "vetted_at": "2026-07-11",
        "constraints_file": "agent-reach-constraints.txt",
        "skill_hashes": {
            "SKILL.md": "a" * 64,
            "references/twitter.md": "c" * 64,
        },
        "channels": {
            "twitter": {"backends": [{"kind": "pipx", "spec": "twitter-cli==1.2.3"}]},
            "opencli": {
                "backends": [
                    {"kind": "npm", "spec": "@jackwener/opencli@0.5.1"},
                    {"note": "Chrome extension ildkmabpimmkaediidaifkhjpohdnifk is manual"},
                ]
            },
            "reddit": {
                "backends": [
                    {"kind": "pipx-git", "spec": "git+https://github.com/public-clis/rdt-cli.git@" + "d" * 40}
                ]
            },
        },
    }


def _write_pin(tmp_path, data: dict):
    p = tmp_path / "agent-reach.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _expect_reject(tmp_path, mutate):
    data = _valid_pin_dict()
    mutate(data)
    p = _write_pin(tmp_path, data)
    with pytest.raises(PinFileError):
        load_pin(p)


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #

class TestValidPin:
    def test_loads_all_fields(self, tmp_path):
        pin = load_pin(_write_pin(tmp_path, _valid_pin_dict()))
        assert pin.schema_version == 1
        assert pin.commit_sha == "b" * 40
        assert pin.version_label == "v1.5.0"
        assert pin.vetted_at == "2026-07-11"
        assert pin.short_sha == "b" * 12
        assert set(pin.skill_hashes) == {"SKILL.md", "references/twitter.md"}

    def test_channels_and_backends_parsed(self, tmp_path):
        pin = load_pin(_write_pin(tmp_path, _valid_pin_dict()))
        assert set(pin.channels) == {"twitter", "opencli", "reddit"}
        twitter = pin.channels["twitter"].backends[0]
        assert twitter.kind == "pipx" and twitter.spec == "twitter-cli==1.2.3"
        assert twitter.is_installable

    def test_note_only_backend_is_non_installable(self, tmp_path):
        pin = load_pin(_write_pin(tmp_path, _valid_pin_dict()))
        opencli = pin.channels["opencli"]
        installable = opencli.installable_backends()
        assert len(installable) == 1
        assert installable[0].spec == "@jackwener/opencli@0.5.1"
        note_backend = opencli.backends[1]
        assert not note_backend.is_installable
        assert note_backend.note is not None

    def test_constraints_path_resolves_next_to_pin(self, tmp_path):
        pin = load_pin(_write_pin(tmp_path, _valid_pin_dict()))
        assert pin.constraints_path() == tmp_path / "agent-reach-constraints.txt"

    def test_accepts_sha256_prefixed_hashes(self, tmp_path):
        data = _valid_pin_dict()
        data["skill_hashes"] = {"SKILL.md": "sha256:" + "a" * 64}
        pin = load_pin(_write_pin(tmp_path, data))
        assert pin.skill_hashes["SKILL.md"].startswith("sha256:")


# --------------------------------------------------------------------------- #
# rejections
# --------------------------------------------------------------------------- #

class TestRejections:
    def test_missing_file(self, tmp_path):
        with pytest.raises(PinFileError):
            load_pin(tmp_path / "does-not-exist.json")

    def test_not_json_object(self, tmp_path):
        p = tmp_path / "agent-reach.json"
        p.write_text("[]", encoding="utf-8")
        with pytest.raises(PinFileError):
            load_pin(p)

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "agent-reach.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(PinFileError):
            load_pin(p)

    def test_missing_commit_sha(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.pop("commit_sha"))

    def test_short_commit_sha(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.update(commit_sha="abc123"))

    def test_non_hex_commit_sha(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.update(commit_sha="z" * 40))

    def test_tag_looking_commit_sha(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.update(commit_sha="v1.5.0"))

    def test_unknown_schema_version(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.update(schema_version=2))

    def test_missing_schema_version(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.pop("schema_version"))

    def test_empty_skill_hashes(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.update(skill_hashes={}))

    def test_skill_hash_empty_value(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.update(skill_hashes={"SKILL.md": ""}))

    def test_missing_upstream(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.pop("upstream"))

    def test_missing_constraints_file(self, tmp_path):
        _expect_reject(tmp_path, lambda d: d.pop("constraints_file"))

    def test_npm_spec_without_version(self, tmp_path):
        def mutate(d):
            d["channels"]["opencli"]["backends"][0]["spec"] = "@jackwener/opencli"
        _expect_reject(tmp_path, mutate)

    def test_npm_spec_latest_tag(self, tmp_path):
        def mutate(d):
            d["channels"]["opencli"]["backends"][0]["spec"] = "@jackwener/opencli@latest"
        _expect_reject(tmp_path, mutate)

    def test_pipx_spec_without_double_equals(self, tmp_path):
        def mutate(d):
            d["channels"]["twitter"]["backends"][0]["spec"] = "twitter-cli>=1.2.3"
        _expect_reject(tmp_path, mutate)

    def test_pipx_git_spec_without_sha(self, tmp_path):
        def mutate(d):
            d["channels"]["reddit"]["backends"][0]["spec"] = "git+https://github.com/public-clis/rdt-cli.git@main"
        _expect_reject(tmp_path, mutate)

    def test_unknown_backend_kind(self, tmp_path):
        def mutate(d):
            d["channels"]["twitter"]["backends"][0] = {"kind": "brew", "spec": "twitter-cli"}
        _expect_reject(tmp_path, mutate)

    def test_backend_missing_spec(self, tmp_path):
        def mutate(d):
            d["channels"]["twitter"]["backends"][0] = {"kind": "pipx"}
        _expect_reject(tmp_path, mutate)

    def test_backend_neither_kind_nor_note(self, tmp_path):
        def mutate(d):
            d["channels"]["twitter"]["backends"][0] = {"foo": "bar"}
        _expect_reject(tmp_path, mutate)

    def test_channel_empty_backends(self, tmp_path):
        def mutate(d):
            d["channels"]["twitter"]["backends"] = []
        _expect_reject(tmp_path, mutate)

    def test_unknown_channel_names_do_not_crash_valid_load(self, tmp_path):
        # sanity: the valid fixture itself is not accidentally rejected
        data = copy.deepcopy(_valid_pin_dict())
        load_pin(_write_pin(tmp_path, data))


class TestExactPinValidator:
    """The npm/pipx spec validator enforces EXACT versions (V3): a range, a
    partial, a `.x`, or a `||` compound must be rejected, else npm/uv could
    resolve a floating version at enable-channel time."""

    @pytest.mark.parametrize("spec", [
        "@jackwener/opencli@1.8.6", "mcporter@0.12.3", "pkg@1.2.3", "pkg@1.2.3-beta.1",
    ])
    def test_npm_exact_accepted(self, spec):
        assert _spec_is_exactly_pinned("npm", spec) is True

    @pytest.mark.parametrize("spec", [
        "mcporter@1.2", "pkg@1.2.x", "pkg@1.2.3 || 2.0.0", "pkg@latest", "pkg@1.x",
        "pkg@^1.2.3", "pkg@~1.2.3", "pkg@>=1.0.0", "pkg",
    ])
    def test_npm_inexact_rejected(self, spec):
        assert _spec_is_exactly_pinned("npm", spec) is False

    @pytest.mark.parametrize("spec", ["twitter-cli==0.8.5", "bilibili-cli==0.6.2"])
    def test_pipx_exact_accepted(self, spec):
        assert _spec_is_exactly_pinned("pipx", spec) is True

    @pytest.mark.parametrize("spec", [
        "pkg==1.2", "pkg==1.2.x", "pkg>=1.0.0", "pkg==1.2.3,<2", "pkg==1.2.3 || 2",
    ])
    def test_pipx_inexact_rejected(self, spec):
        assert _spec_is_exactly_pinned("pipx", spec) is False

    def test_a_range_backend_fails_pin_load(self, tmp_path):
        data = copy.deepcopy(_valid_pin_dict())
        data["channels"]["twitter"]["backends"] = [{"kind": "npm", "spec": "pkg@1.2.x"}]
        with pytest.raises(PinFileError):
            load_pin(_write_pin(tmp_path, data))


class TestPinHardening:
    def test_non_https_upstream_rejected(self, tmp_path):
        data = copy.deepcopy(_valid_pin_dict())
        data["upstream"] = "ext::sh -c 'evil'"
        with pytest.raises(PinFileError, match="https"):
            load_pin(_write_pin(tmp_path, data))

    def test_dash_leading_channel_spec_rejected(self, tmp_path):
        data = copy.deepcopy(_valid_pin_dict())
        data["channels"]["twitter"]["backends"] = [{"kind": "npm", "spec": "--evil@1.0.0"}]
        with pytest.raises(PinFileError, match="may not start with"):
            load_pin(_write_pin(tmp_path, data))


class TestPinHardening2:
    @pytest.mark.parametrize("spec,ok", [
        ("git+https://github.com/x/y.git@" + "a"*40, True),
        ("git+ssh://git@github.com/x/y.git@" + "a"*40, False),
        ("git+file:///tmp/y@" + "a"*40, False),
    ])
    def test_pipx_git_requires_https(self, spec, ok):
        assert _spec_is_exactly_pinned("pipx-git", spec) is ok

    @pytest.mark.parametrize("spec,ok", [
        ("pkg@1.2.3-rc.1+build.5", True),
        ("pkg@1.2.3+b", True),
        ("pkg@1.2.3-beta.1", True),
        ("pkg@1.2", False),
    ])
    def test_npm_combined_semver(self, spec, ok):
        assert _spec_is_exactly_pinned("npm", spec) is ok
