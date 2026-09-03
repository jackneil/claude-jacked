"""The compatibility adapter must never reinstall legacy startup artifacts."""

from unittest.mock import MagicMock, patch


def test_ensure_native_lifecycle_uses_exact_install_contract():
    from jacked.service import platform as plat

    spec = MagicMock()
    with (
        patch(
            "jacked.service.lifecycle.provision_service_contract",
            return_value=(spec, {"PATH": "safe"}),
        ),
        patch(
            "jacked.service.lifecycle.install_native_owned",
            return_value=MagicMock(ok=True, reason="activated exact generation"),
        ) as install,
    ):
        ok, state, reason = plat.ensure_native_lifecycle()

    assert (ok, state) == (True, "just_installed")
    assert "exact" in reason
    install.assert_called_once_with(spec, environment={"PATH": "safe"})


def test_ensure_native_lifecycle_fails_closed():
    from jacked.service import platform as plat

    with patch(
        "jacked.service.lifecycle.provision_service_contract",
        side_effect=ValueError("invalid launcher"),
    ):
        ok, state, reason = plat.ensure_native_lifecycle()

    assert (ok, state, reason) == (False, "unavailable", "ValueError")
