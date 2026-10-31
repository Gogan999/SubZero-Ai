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
NODE_VERSION = "22.23.4"
MIN_NPM = (9, 6)  # --os and --cpu for optional dependencies
# update.ps1 unpacks the zip under %TEMP%\SZ-<yyyyMMdd-HHmmss>\u\ with Windows PowerShell 5.1, which fails past 259
# characters. Assume the worst: a 30-character user name. Explorer's Extract All (about 45 characters in front,
# C:\Users\<name>\Downloads\SubZeroAI-windows\) is then covered too.
UPDATE_PREFIX = "C:\\Users\\" + "u" * 30 + "\\AppData\\Local\\Temp" + "\\SZ-20260101-120000\\u\\"
PATH_LIMIT = 259
PATH_PREFIX = len(UPDATE_PREFIX)
GATEWAY_FILES = ("package.json", "package-lock.json", "gateway.mjs", "README.md")
DOWNLOADS = {
    "python": (f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip",
               "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"),
    "node": (f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip",
             "cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20"),
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


def prune_node_modules(root: Path) -> int:
    """Remove files the gateway never loads: type declarations (.d.ts, .d.mts, .d.cts), source maps, and every
    node_modules/.bin folder. Keeps .ts sources (pi-claude-bridge loads its src/*.ts), docs and licenses. Returns the
    number of files removed."""
    removed = 0
    for path in list(root.rglob("*")):
        if path.is_symlink() or not path.exists():
            continue
        if path.is_dir() and path.name == ".bin" and path.parent.name == "node_modules":
            removed += sum(1 for f in path.rglob("*") if f.is_file() or f.is_symlink())
            shutil.rmtree(path)
        elif path.is_file() and re.search(r"\.d\.[cm]?ts$|\.map$", path.name):
            path.unlink()
            removed += 1
    return removed


def long_zip_paths(names: list[str], prefix: int = PATH_PREFIX, limit: int = PATH_LIMIT) -> list[tuple[int, str]]:
    """The zip paths that would pass `limit` characters once extracted under a folder `prefix` characters long,
    longest first."""
    bad = [(len(name) + prefix, name) for name in names if len(name) + prefix > limit]
    return sorted(bad, reverse=True)


def npm_version() -> tuple[str, tuple[int, ...]]:
    """The build computer's npm program and its version. Stops with a plain message if it is missing or old."""
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm isn't installed on this computer. Install Node.js 22 or newer (it includes npm) to build.")
    out = subprocess.run([npm, "--version"], capture_output=True, text=True).stdout.strip()
    found = re.match(r"(\d+)\.(\d+)", out)
    if not found or (int(found.group(1)), int(found.group(2))) < MIN_NPM:
        raise SystemExit(f"npm {out or '(unknown version)'} is too old; the build needs npm "
                         f"{'.'.join(map(str, MIN_NPM))} or newer for --os and --cpu. Update Node.js.")
    return npm, tuple(int(n) for n in found.groups())


def add_node(stage: Path, cache: Path) -> None:
    """Unpack the pinned Node.js zip into <stage>/node, without its top-level node-v<version>-win-x64 folder."""
    node_dir = stage / "node"
    top = f"node-v{NODE_VERSION}-win-x64/"
    with zipfile.ZipFile(fetch("node", cache)) as zf:
        for info in zf.infolist():
            if not info.filename.startswith(top):
                raise SystemExit(f"Unexpected file in the Node.js zip: {info.filename}")
            rel = info.filename[len(top):]
            if not rel:
                continue
            target = node_dir / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
    if not (node_dir / "node.exe").is_file():
        raise SystemExit("The Node.js zip has no node.exe.")


def add_gateway(app: Path) -> None:
    """Copy pi_gateway (not its tests or the dev machine's node_modules) and install its packages for 64-bit
    Windows from the lock file, without running any install scripts."""
    npm, _ = npm_version()
    gateway = app / "pi_gateway"
    gateway.mkdir(parents=True)
    for name in GATEWAY_FILES:
        shutil.copy2(ROOT / "pi_gateway" / name, gateway / name)
    shutil.copytree(ROOT / "pi_gateway" / "lib", gateway / "lib")
    print("Installing the AI helper's npm packages for Windows (from package-lock.json)")
    subprocess.run([npm, "ci", "--ignore-scripts", "--omit=dev", "--os=win32", "--cpu=x64",
                    "--no-audit", "--no-fund"], cwd=gateway, check=True)
    print(f"Pruned {prune_node_modules(gateway / 'node_modules')} files (type declarations, source maps, .bin)")
    sdk = gateway / "node_modules" / "@anthropic-ai"
    if not (sdk / "claude-agent-sdk-win32-x64" / "claude.exe").is_file():
        raise SystemExit("npm didn't install claude.exe (@anthropic-ai/claude-agent-sdk-win32-x64).")
    other = sorted(p.name for p in sdk.glob("claude-agent-sdk-*") if p.name != "claude-agent-sdk-win32-x64")
    if other:
        raise SystemExit(f"npm installed packages for other systems: {', '.join(other)}.")


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

    # Node.js and the AI helper that runs on it
    add_node(stage, cache)
    add_gateway(app)

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
        "node": NODE_VERSION,
    }, indent=2) + "\n", encoding="ascii")

    # Zip it, with everything inside one SubZeroAI-<version> folder
    names = [f"{stage.name}/{p.relative_to(stage).as_posix()}" for p in sorted(stage.rglob("*")) if p.is_file()]
    too_long = long_zip_paths(names)
    if too_long:
        shown = "\n".join(f"  {n} {p}" for n, p in too_long[:10])
        raise SystemExit(f"{len(too_long)} paths in the zip are too long to unpack in the worst case (limit "
                         f"{PATH_LIMIT} with {PATH_PREFIX} characters of folders in front); the longest is "
                         f"{too_long[0][0]}:\n{shown}")
    print(f"Longest path in the zip: {max(map(len, names))} characters (limit {PATH_LIMIT - PATH_PREFIX}, for the worst-case update folder)")
    archive = out_dir / ASSET_NAME
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in names:
            zf.write(out_dir / name, name)
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
