"""Make the sample reports of the test-diet recorder from real runs.

Run it from the repository root:

    uv run python tests/fixtures/testdiet/capture.py

For each sample project in ``projects/``, the script:

1. copies the project to a new temporary folder;
2. writes the dependency manifest there, with exact tool versions;
3. installs the tools there, with install scripts off;
4. runs the tests and the test listing of the project;
5. changes two values in each output: the absolute root of the temporary
   project becomes ``/work``, and each ``hostname`` attribute becomes
   ``gate.example``;
6. stops when an output still holds a private value of this machine;
7. writes the JUnit file, the listing and the console total into
   ``reports/``, and writes ``PROVENANCE.json``.

The script changes no other byte of a report. It installs nothing outside
the temporary folder. It downloads the release binaries of ``gotestsum`` and
``cargo-nextest`` and checks their published SHA-256. It needs ``uv``,
``npm``, ``node``, ``go`` and ``cargo`` on the PATH.

Each tool version was published 14 days or more before the capture date.
Read each command in the documentation of its runner before you change it:
see ``DOCS`` below.
"""

from __future__ import annotations

import datetime
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
PROJECTS = HERE / "projects"
REPORTS = HERE / "reports"
PROVENANCE = HERE / "PROVENANCE.json"
REPORT_ROOT = "/work"
REPORT_HOST = "gate.example"

sys.path.insert(0, str(REPO_ROOT))
from tests._testdiet import private_hits  # noqa: E402

# Exact versions, with the publish date of each one (from the registry).
PYTHON_PACKAGES = {
    "pytest": ("9.2.0", "2026-10-27"),
    "pytest-rerunfailures": ("16.7", "2026-09-17"),
}
NPM_PACKAGES = {
    "vitest": ("5.1.0", "2026-11-02"),
    "jest": ("30.5.2", "2026-09-18"),
    "jest-junit": ("17.0.0", "2026-04-24"),
    "@playwright/test": ("1.65.0", "2026-11-05"),
}
GOTESTSUM = ("1.13.0", "2025-09-11")
NEXTEST = ("0.9.151", "2026-11-03")

DOCS = {
    "pytest": "https://docs.pytest.org/en/stable/how-to/output.html#creating-junitxml-format-files",
    "pytest-rerunfailures": "https://github.com/pytest-dev/pytest-rerunfailures#readme",
    "vitest": "https://vitest.dev/guide/reporters#junit-reporter",
    "jest-junit": "https://github.com/jest-community/jest-junit#configuration",
    "playwright": "https://playwright.dev/docs/test-reporters#junit-reporter",
    "gotestsum": "https://github.com/gotestyourself/gotestsum#junit-xml-output",
    "nextest": "https://nexte.st/docs/machine-readable/junit/",
}

# Environment for every runner: no colour codes, no watch mode.
RUN_ENV = {"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", "TERM": "dumb"}

# The jest-junit settings that the reader expects (see jacked/testdiet/ids.py).
JEST_JUNIT_ENV = {
    "JEST_JUNIT_OUTPUT_DIR": ".",
    "JEST_JUNIT_OUTPUT_NAME": "junit.xml",
    "JEST_JUNIT_ADD_FILE_ATTRIBUTE": "true",
    "JEST_JUNIT_CLASSNAME": "{classname}",
    "JEST_JUNIT_TITLE": "{title}",
    "JEST_JUNIT_ANCESTOR_SEPARATOR": " > ",
    "JEST_JUNIT_REPORT_TEST_SUITE_ERRORS": "true",
}

NEXTEST_CONFIG = '[profile.default.junit]\npath = "junit.xml"\n'


class CaptureError(RuntimeError):
    """A step of the capture failed. The message says which step."""


def _run(argv: list[str], cwd: Path, env: dict[str, str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run one command. Return the result. Raise CaptureError when check is on and it fails."""
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        raise CaptureError(
            f"{' '.join(argv)} exited {proc.returncode}\n"
            f"{proc.stdout.decode(errors='replace')}\n{proc.stderr.decode(errors='replace')}"
        )
    return proc


def _download(url: str, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / url.rsplit("/", 1)[1]
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 (fixed https URL)
        target.write_bytes(response.read())
    return target


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _extract(archive: Path, member_name: str, bin_dir: Path) -> Path:
    with tarfile.open(archive) as tar:
        member = next((m for m in tar.getmembers() if Path(m.name).name == member_name and m.isfile()), None)
        if member is None:
            raise CaptureError(f"{archive.name} has no file {member_name}")
        tar.extract(member, archive.parent / "x", filter="data")
    binary = bin_dir / member_name
    shutil.copyfile(archive.parent / "x" / member.name, binary)
    binary.chmod(0o755)
    return binary


def _platform_keys() -> tuple[str, str]:
    """The release asset names of gotestsum and cargo-nextest for this system."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        return (f"darwin_{'arm64' if machine == 'arm64' else 'amd64'}", "universal-apple-darwin")
    if system == "Linux" and machine in ("x86_64", "amd64"):
        return ("linux_amd64", "x86_64-unknown-linux-gnu")
    if system == "Linux" and machine in ("aarch64", "arm64"):
        return ("linux_arm64", "aarch64-unknown-linux-gnu")
    raise CaptureError(f"no release binary is set for {system} {machine}")


def install_gotestsum(tmp: Path, bin_dir: Path) -> dict:
    version = GOTESTSUM[0]
    key = _platform_keys()[0]
    base = f"https://github.com/gotestyourself/gotestsum/releases/download/v{version}"
    archive = _download(f"{base}/gotestsum_{version}_{key}.tar.gz", tmp / "dl-gotestsum")
    sums = _download(f"{base}/gotestsum-{version}-checksums.txt", tmp / "dl-gotestsum-sums")
    expected = {line.split()[1]: line.split()[0] for line in sums.read_text().splitlines() if line.strip()}
    actual = _sha256(archive)
    if expected.get(archive.name) != actual:
        raise CaptureError(f"checksum of {archive.name} does not match the published value")
    _extract(archive, "gotestsum", bin_dir)
    return {"version": version, "published": GOTESTSUM[1], "asset": archive.name, "sha256": actual}


def install_nextest(tmp: Path, bin_dir: Path) -> dict:
    version = NEXTEST[0]
    key = _platform_keys()[1]
    base = f"https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-{version}"
    name = f"cargo-nextest-{version}-{key}.tar.gz"
    archive = _download(f"{base}/{name}", tmp / "dl-nextest")
    sums = _download(f"{base}/cargo-nextest-{version}-{key}.sha256", tmp / "dl-nextest-sums")
    expected = sums.read_text().split()[0]
    actual = _sha256(archive)
    if expected != actual:
        raise CaptureError(f"checksum of {name} does not match the published value")
    _extract(archive, "cargo-nextest", bin_dir)
    return {"version": version, "published": NEXTEST[1], "asset": name, "sha256": actual}


def install_python(tmp: Path) -> tuple[Path, dict]:
    venv = tmp / "venv"
    _run(["uv", "venv", "--quiet", str(venv)], tmp, os.environ.copy())
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    pins = [f"{name}=={version}" for name, (version, _) in PYTHON_PACKAGES.items()]
    _run(["uv", "pip", "install", "--quiet", "--python", str(python), *pins], tmp, os.environ.copy())
    py_version = _run([str(python), "-c", "import sys; print(sys.version.split()[0])"], tmp, os.environ.copy())
    return python, {"python": py_version.stdout.decode().strip()}


def write_package_json(work: Path, names: list[str]) -> None:
    manifest = {
        "name": "testdiet-sample",
        "private": True,
        "devDependencies": {name: NPM_PACKAGES[name][0] for name in names},
    }
    (work / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="")


NPM_INSTALL = ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", "--save-exact"]


def captures(python: Path) -> list[dict]:
    """The sample runs. Each entry names a project, its setup and its commands.

    A command list is (kind, argv, extra env). The kind is "run" (the test run
    that writes junit.xml) or "list" (the test listing).
    """
    py = str(python)
    pytest_run = [py, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir", ".", "--junitxml", "junit.xml"]
    pytest_list = [py, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir", ".", "--collect-only", "-q"]
    return [
        {"name": "pytest", "project": "pytest", "npm": [],
         "commands": [("run", pytest_run + ["tests"], {}), ("list", pytest_list + ["tests"], {})]},
        {"name": "pytest_rerun", "project": "pytest_rerun", "npm": [],
         "commands": [("run", pytest_run + ["--reruns", "1", "tests"], {})]},
        {"name": "pytest_import_error", "project": "pytest_import_error", "npm": [],
         "commands": [("run", pytest_run + ["tests"], {}), ("list", pytest_list + ["tests"], {})]},
        {"name": "vitest", "project": "vitest", "npm": ["vitest"],
         "commands": [
             ("run", ["npx", "--no-install", "vitest", "run", "--reporter=default", "--reporter=junit",
                      "--outputFile.junit=junit.xml"], {}),
             # By default "vitest list" parses the files statically and prints the
             # template of test.each ("parametrized %i"). --no-staticParse runs the
             # collection, so the listing holds the names that the report holds.
             ("list", ["npx", "--no-install", "vitest", "list", "--no-staticParse"], {}),
         ]},
        {"name": "jest", "project": "jest", "npm": ["jest", "jest-junit"],
         "commands": [
             ("run", ["npx", "--no-install", "jest", "--ci", "--reporters=default", "--reporters=jest-junit"],
              JEST_JUNIT_ENV),
             ("list", ["npx", "--no-install", "jest", "--listTests"], {}),
         ]},
        {"name": "playwright", "project": "playwright", "npm": ["@playwright/test"],
         "commands": [
             ("run", ["npx", "--no-install", "playwright", "test", "--reporter=list,junit", "--workers=1"],
              {"PLAYWRIGHT_JUNIT_OUTPUT_FILE": "junit.xml"}),
             ("list", ["npx", "--no-install", "playwright", "test", "--list"], {}),
         ]},
        {"name": "playwright_moved", "project": "playwright_moved", "npm": ["@playwright/test"],
         "commands": [
             ("run", ["npx", "--no-install", "playwright", "test", "--reporter=list,junit", "--workers=1"],
              {"PLAYWRIGHT_JUNIT_OUTPUT_FILE": "junit.xml"}),
             ("list", ["npx", "--no-install", "playwright", "test", "--list"], {}),
         ]},
        {"name": "go", "project": "go", "npm": [],
         "commands": [
             ("run", ["gotestsum", "--junitfile", "junit.xml", "--format", "standard-verbose", "--",
                      "-count=1", "./..."], {}),
             ("list", ["go", "test", "-list", ".", "./..."], {}),
         ]},
        {"name": "go_rerun", "project": "go_rerun", "npm": [],
         "commands": [
             ("run", ["gotestsum", "--junitfile", "junit.xml", "--rerun-fails=1", "--packages", "./...",
                      "--format", "standard-verbose", "--", "-count=1"], {}),
         ]},
        {"name": "nextest", "project": "nextest", "npm": [], "nextest_config": True,
         "commands": [
             ("run", ["cargo", "nextest", "run", "--no-fail-fast"], {}),
             ("list", ["cargo", "nextest", "list"], {}),
         ]},
    ]


# The console line that holds the totals of each runner, from its real output.
TOTAL_LINE = {
    "pytest": re.compile(r"^=+ .*\d+ (passed|failed|error).* in [\d.]+s.* =+$"),
    "vitest": re.compile(r"^\s*Tests\s+.*\d"),
    "jest": re.compile(r"^Tests:\s+.*\d+ total$"),
    "playwright": re.compile(r"^\s*\d+ (passed|failed|skipped)"),
    "go": re.compile(r"^DONE \d+ (tests|runs)"),
    "nextest": re.compile(r"^\s*Summary \["),
}


def _rewrite(data: bytes, roots: list[str]) -> bytes:
    for root in roots:
        data = data.replace(root.encode(), REPORT_ROOT.encode())
    return re.sub(rb'hostname="[^"]*"', b'hostname="' + REPORT_HOST.encode() + b'"', data)


def _check_private(label: str, data: bytes, private: dict[str, str]) -> None:
    hits = private_hits(data.decode("utf-8", errors="replace"), **private)
    if hits:
        raise CaptureError(f"{label} still holds a private value of this machine ({len(hits)} hit(s)); nothing written")


def _total_line(runner: str, console: str) -> str:
    pattern = TOTAL_LINE[runner.split("_")[0]]
    lines = [line.rstrip() for line in console.splitlines() if pattern.search(line)]
    if not lines:
        raise CaptureError(f"no total line in the console output of {runner}")
    return "\n".join(lines) + "\n"


def capture_one(spec: dict, tmp: Path, env: dict[str, str], private: dict[str, str]) -> tuple[dict, dict[str, bytes]]:
    work = (tmp / spec["name"] / "work")
    shutil.copytree(PROJECTS / spec["project"], work)
    work = work.resolve()
    roots = sorted({str(work), str(tmp / spec["name"] / "work")}, key=len, reverse=True)
    record: dict = {"project": f"projects/{spec['project']}", "commands": []}
    if spec["npm"]:
        write_package_json(work, spec["npm"])
        install = NPM_INSTALL + ["--cache", str(tmp / "npm-cache")]
        _run(install, work, env)
        record["setup"] = [" ".join(NPM_INSTALL), "package.json: " + ", ".join(
            f"{name}@{NPM_PACKAGES[name][0]}" for name in spec["npm"])]
    if spec.get("nextest_config"):
        (work / ".config").mkdir()
        (work / ".config" / "nextest.toml").write_text(NEXTEST_CONFIG, encoding="utf-8", newline="")
        record["setup"] = [".config/nextest.toml: " + NEXTEST_CONFIG.strip().replace("\n", " ")]
    outputs: dict[str, bytes] = {}
    for kind, argv, extra in spec["commands"]:
        proc = _run(argv, work, {**env, **extra}, check=False)
        console = proc.stdout + proc.stderr
        shown = [Path(argv[0]).name if os.path.isabs(argv[0]) else argv[0], *argv[1:]]
        record["commands"].append({"kind": kind, "argv": shown, "env": extra, "exit_code": proc.returncode})
        if kind == "run":
            junit = work / ("target/nextest/default/junit.xml" if spec["name"] == "nextest" else "junit.xml")
            if not junit.is_file():
                raise CaptureError(f"{spec['name']}: the run wrote no JUnit file\n{console.decode(errors='replace')}")
            outputs[f"{spec['name']}.xml"] = _rewrite(junit.read_bytes(), roots)
            total = _total_line(spec["name"], _rewrite(console, roots).decode("utf-8", errors="replace"))
            outputs[f"{spec['name']}.total.txt"] = total.encode()
        else:
            if proc.returncode != 0 and spec["name"] != "pytest_import_error":
                raise CaptureError(f"{spec['name']}: the listing exited {proc.returncode}\n"
                                   f"{console.decode(errors='replace')}")
            outputs[f"{spec['name']}.list.txt"] = _rewrite(proc.stdout, roots)
    for name, data in outputs.items():
        _check_private(name, data, private)
    return record, outputs


def tool_versions(env: dict[str, str], python: Path, tmp: Path) -> dict:
    def first_line(argv: list[str]) -> str:
        proc = _run(argv, tmp, env)
        return (proc.stdout or proc.stderr).decode().strip().splitlines()[0]

    return {
        "node": first_line(["node", "--version"]),
        "npm": first_line(["npm", "--version"]),
        "go": first_line(["go", "version"]),
        "cargo": first_line(["cargo", "--version"]),
        "rustc": first_line(["rustc", "--version"]),
        "uv": first_line(["uv", "--version"]),
    }


def main() -> int:
    private = {"host": socket.gethostname(), "login": getpass.getuser(), "home": str(Path.home())}
    with tempfile.TemporaryDirectory(prefix="testdiet-capture-") as tmp_name:
        tmp = Path(tmp_name).resolve()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        tools: dict = {
            "gotestsum": install_gotestsum(tmp, bin_dir),
            "cargo-nextest": install_nextest(tmp, bin_dir),
        }
        python, py_info = install_python(tmp)
        tools.update(py_info)
        env = {
            **os.environ,
            **RUN_ENV,
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "GOPATH": str(tmp / "gopath"),
            "GOCACHE": str(tmp / "gocache"),
            "GOFLAGS": "-mod=mod",
            "GOTOOLCHAIN": "local",
            "npm_config_update_notifier": "false",
        }
        for var in ("PYTHONPATH", "VIRTUAL_ENV", "PYTEST_ADDOPTS"):
            env.pop(var, None)
        tools.update(tool_versions(env, python, tmp))
        runs = []
        written: dict[str, bytes] = {}
        for spec in captures(python):
            record, outputs = capture_one(spec, tmp, env, private)
            record["name"] = spec["name"]
            record["files"] = sorted(outputs)
            runs.append(record)
            written.update(outputs)
    if REPORTS.exists():
        shutil.rmtree(REPORTS)
    REPORTS.mkdir()
    for name, data in sorted(written.items()):
        (REPORTS / name).write_bytes(data)
    provenance = {
        "date": datetime.date.today().isoformat(),
        "script": "tests/fixtures/testdiet/capture.py",
        "system": f"{platform.system()} {platform.machine()}",
        "tools": tools,
        "python_packages": {name: {"version": v, "published": d} for name, (v, d) in PYTHON_PACKAGES.items()},
        "npm_packages": {name: {"version": v, "published": d} for name, (v, d) in NPM_PACKAGES.items()},
        "docs": DOCS,
        "environment": RUN_ENV,
        "changes": [
            f"The absolute root of the temporary project is replaced by {REPORT_ROOT}.",
            f"Each hostname attribute is replaced by {REPORT_HOST}.",
        ],
        "runs": runs,
    }
    text = json.dumps(provenance, indent=2, sort_keys=False) + "\n"
    _check_private("PROVENANCE.json", text.encode(), private)
    PROVENANCE.write_bytes(text.encode())
    print(f"wrote {len(written)} files to {REPORTS.relative_to(REPO_ROOT)} and PROVENANCE.json")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CaptureError as exc:
        print(f"capture failed: {exc}", file=sys.stderr)
        sys.exit(1)
