"""The Starlette TestClient must run on httpx2, not on the httpx fallback.

starlette.testclient imports httpx2 when it is installed. Without httpx2, it
falls back to httpx and gives a StarletteDeprecationWarning on each import.
The dev dependency group in pyproject.toml supplies httpx2 (#200i). This test
fails if httpx2 leaves the dev environment. It does not filter the warning.
"""


def test_starlette_testclient_runs_on_httpx2():
    import starlette.testclient

    assert starlette.testclient.httpx.__name__ == "httpx2", (
        "starlette.testclient uses the deprecated httpx fallback: httpx2 is "
        "missing from the dev environment ([dependency-groups] dev in pyproject.toml)"
    )
