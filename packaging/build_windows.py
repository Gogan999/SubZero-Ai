"""Build dist/SubZeroAI-windows.zip: the app with its own Python, the WinSW service wrapper, the Cloudflare
connector, and the install, update, and restore scripts.

    python packaging/build_windows.py

Runs on Windows, macOS, or Linux with Python 3.10 or newer and internet access. Every download is pinned to
an exact version and checked against its SHA-256: Python, WinSW, and cloudflared below, and the Python packages
in requirements-windows.txt. Downloads are cached in packaging/.cache so repeat builds are quick.

After changing requirements-lock.txt, refresh the Windows hashes with:

    python packaging/build_windows.py --write-hashes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
WINDOWS = PACKAGING / "windows"

PYTHON_VERSION = "3.13.17"
DOWNLOADS = {
    "python": (f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip",
               "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"),
    "winsw": ("https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe",
              "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"),
    "cloudflared": ("https://github.com/cloudflare/cloudflared/releases/download/2026.10.3/"
                    "cloudflared-windows-amd64.exe",
                    "76b2805253d0d6ffacb092a7acfbee99e7af6917835c0c5410e23f3ef5052da2"),
}
ASSET_NAME = "SubZeroAI-windows.zip"
UPDATE_REPO = "Gogan999/SubZero-Ai"
TEXT_SUFFIXES = {".ps1", ".cmd", ".txt", ".xml"}  # given Windows line endings in the zip
WHEEL_TARGET = ["--only-binary=:all:", "--platform", "win_amd64", "--implementation", "cp"]


def app_version() -> str:
    text = (ROOT / "subzero_ai" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__\s*=\s*"([^"]+)"', text).group(1)


def pth_contents(python_version: str = PYTHON_VERSION) -> str:
    """The embedded Python's path file: its standard library, the bundled packages, and the app folder.
    `import site` is needed for the packages' .pth files to work."""
    short = "".join(python_version.split(".")[:2])
    return f"python{short}.zip\n.\nLib\\site-packages\n..\\app\nimport site\n"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(name: str, cache: Path) -> Path:
    url, expected = DOWNLOADS[name]
    target = cache / url.rsplit("/", 1)[1]
    if not target.exists() or sha256(target) != expected:
        cache.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {url}")
        partial = target.with_suffix(target.suffix + ".partial")
        with urllib.request.urlopen(url, timeout=120) as resp, open(partial, "wb") as out:
            shutil.copyfileobj(resp, out)
        partial.replace(target)
    actual = sha256(target)
    if actual != expected:
        raise SystemExit(f"{target.name} doesn't match its pinned SHA-256 (got {actual}). Not building.")
    return target


def copy_text(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() in TEXT_SUFFIXES:
        text = src.read_text(encoding="utf-8")
        if any(ord(ch) > 127 for ch in text) and src.suffix.lower() in (".ps1", ".cmd"):
            raise SystemExit(f"{src} has non-ASCII characters, which Windows PowerShell 5.1 misreads.")
        dest.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    else:
        shutil.copy2(src, dest)


def build(out_dir: Path, cache: Path) -> Path:
    version = app_version()
    stage = out_dir / f"SubZeroAI-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # Python, with the app's packages installed for 64-bit Windows
    python_dir = stage / "python"
    with zipfile.ZipFile(fetch("python", cache)) as zf:
        zf.extractall(python_dir)
    short = "".join(PYTHON_VERSION.split(".")[:2])
    (python_dir / f"python{short}._pth").write_text(pth_contents(), encoding="ascii")
    site_packages = python_dir / "Lib" / "site-packages"
    print("Installing packages from requirements-windows.txt for Windows (hash-checked)")
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-deps",
        "--require-hashes", *WHEEL_TARGET, "--python-version", short,
        "--target", str(site_packages), "-r", str(PACKAGING / "requirements-windows.txt"),
    ], check=True)
    shutil.rmtree(site_packages / "bin", ignore_errors=True)  # console scripts for the build machine
    for cache_dir in site_packages.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    # The app
    app = stage / "app"
    shutil.copytree(ROOT / "subzero_ai", app / "subzero_ai", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("config.example.env", "slack-manifest.yaml", "requirements.txt"):
        shutil.copy2(ROOT / name, app / name)
    shutil.copy2(PACKAGING / "requirements-lock.txt", app / "requirements-lock.txt")

    # Service wrapper, Cloudflare connector, scripts, and the .cmd files people double-click
    (stage / "service").mkdir()
    shutil.copy2(fetch("winsw", cache), stage / "service" / "SubZeroAI.exe")
    copy_text(WINDOWS / "service" / "SubZeroAI.xml", stage / "service" / "SubZeroAI.xml")
    (stage / "cloudflared").mkdir()
    shutil.copy2(fetch("cloudflared", cache), stage / "cloudflared" / "cloudflared.exe")
    for script in sorted((WINDOWS / "scripts").glob("*.ps1")):
        copy_text(script, stage / "scripts" / script.name)
    for cmd in sorted((WINDOWS / "cmd").glob("*.cmd")):
        copy_text(cmd, stage / cmd.name)
    copy_text(WINDOWS / "README.txt", stage / "README.txt")
    (stage / "VERSION").write_text(version + "\n", encoding="ascii")
    (stage / "release.json").write_text(json.dumps({
        "version": version, "repo": UPDATE_REPO, "asset": ASSET_NAME, "python": PYTHON_VERSION,
    }, indent=2) + "\n", encoding="ascii")

    # Zip it, with everything inside one SubZeroAI-<version> folder
    archive = out_dir / ASSET_NAME
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zf.write(path, f"{stage.name}/{path.relative_to(stage).as_posix()}")
    print(f"Built {archive} ({archive.stat().st_size / 1_048_576:.1f} MB)")
    print(f"SHA-256 {sha256(archive)}")
    return archive


def write_hashes(cache: Path) -> None:
    """Download the Windows wheels for requirements-lock.txt and write requirements-windows.txt with their
    SHA-256 hashes, so the build installs exactly these files."""
    short = "".join(PYTHON_VERSION.split(".")[:2])
    wheels = cache / "wheels"
    shutil.rmtree(wheels, ignore_errors=True)
    subprocess.run([sys.executable, "-m", "pip", "download", "--quiet", "--disable-pip-version-check", "--no-deps",
                    *WHEEL_TARGET, "--python-version", short, "-d", str(wheels),
                    "-r", str(PACKAGING / "requirements-lock.txt")], check=True)
    lines = ["# Generated by: python packaging/build_windows.py --write-hashes",
             "# The exact Windows wheels (64-bit, Python 3.13) for requirements-lock.txt, with their SHA-256 hashes.",
             "# The build installs them with --require-hashes."]
    for pin in pinned(PACKAGING / "requirements-lock.txt"):
        name, version = pin.split("==")
        key = re.sub(r"[-_.]+", "_", name).lower()
        found = [w for w in wheels.glob("*.whl")
                 if re.sub(r"[-_.]+", "_", w.name.split("-")[0]).lower() == key and w.name.split("-")[1] == version]
        if len(found) != 1:
            raise SystemExit(f"Expected one Windows wheel for {pin}, found {len(found)}.")
        lines.append(f"{pin} \\\n    --hash=sha256:{sha256(found[0])}")
    (PACKAGING / "requirements-windows.txt").write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"Wrote {PACKAGING / 'requirements-windows.txt'}")


def pinned(path: Path) -> list[str]:
    """The name==version lines of a requirements file."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip().rstrip("\\").strip()
        if "==" in line:
            out.append(line)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(ROOT / "dist"), help="output folder (default: dist)")
    parser.add_argument("--cache", default=str(PACKAGING / ".cache"), help="download cache folder")
    parser.add_argument("--write-hashes", action="store_true",
                        help="refresh requirements-windows.txt from requirements-lock.txt, then stop")
    args = parser.parse_args(argv)
    if args.write_hashes:
        write_hashes(Path(args.cache))
        return 0
    build(Path(args.out), Path(args.cache))
    return 0


if __name__ == "__main__":
    sys.exit(main())
