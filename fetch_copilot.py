#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


LATEST_RELEASE_URL = "https://api.github.com/repos/github/copilot-cli/releases/latest"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / ".state" / "fetch_copilot.json"
STATE_SCHEMA_VERSION = 1
USER_AGENT = "fetch-cli-tools-copilot-installer"
CHUNK_SIZE = 1024 * 1024
VERSION_PATTERN = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z._-]+)?$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_BINARIES = {
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "linuxmusl-arm64",
    "linuxmusl-x64",
    "win32-arm64",
    "win32-x64",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download and install GitHub Copilot CLI from GitHub Releases."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help="configuration file (default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="download and replace Copilot even when the tracked version matches",
    )
    return parser.parse_args()


def read_json_file(path, description):
    try:
        with path.open(encoding="utf-8") as input_file:
            return json.load(input_file)
    except FileNotFoundError:
        raise RuntimeError("{} does not exist: {}".format(description, path))
    except (OSError, ValueError) as error:
        raise RuntimeError("could not read {} {}: {}".format(description, path, error))


def resolve_config_path(config_path, value):
    result = Path(value).expanduser()
    if not result.is_absolute():
        result = config_path.expanduser().resolve().parent / result
    return result


def load_config(path):
    data = read_json_file(path.expanduser(), "configuration file")
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        raise RuntimeError("configuration must contain a 'tools' object")

    tool = data["tools"].get("copilot")
    if not isinstance(tool, dict):
        raise RuntimeError("configuration must contain a 'tools.copilot' object")

    channel = tool.get("channel", "latest")
    install_dir = tool.get("install_dir")
    binary = tool.get("binary", "auto")
    if channel != "latest":
        raise RuntimeError("tools.copilot.channel only supports 'latest'")
    if not isinstance(install_dir, str) or not install_dir:
        raise RuntimeError("tools.copilot.install_dir must be a directory path")
    if not isinstance(binary, str) or (
        binary != "auto" and binary not in SUPPORTED_BINARIES
    ):
        raise RuntimeError(
            "tools.copilot.binary must be 'auto' or one of: {}".format(
                ", ".join(sorted(SUPPORTED_BINARIES))
            )
        )
    return {
        "channel": channel,
        "install_dir": resolve_config_path(path, install_dir),
        "binary": binary,
    }


def is_rosetta_translated():
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def is_musl_linux():
    if Path("/lib/libc.musl-x86_64.so.1").is_file() or Path(
        "/lib/libc.musl-aarch64.so.1"
    ).is_file():
        return True
    try:
        result = subprocess.run(
            ["ldd", "/bin/ls"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
    except OSError:
        return False
    return "musl" in result.stdout.lower()


def detect_binary():
    system = platform.system()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        architecture = "x64"
    elif machine in ("arm64", "aarch64"):
        architecture = "arm64"
    else:
        raise RuntimeError("unsupported CPU architecture: {}".format(machine))

    if system == "Darwin":
        if architecture == "x64" and is_rosetta_translated():
            architecture = "arm64"
        return "darwin-{}".format(architecture)
    if system == "Linux":
        prefix = "linuxmusl" if is_musl_linux() else "linux"
        return "{}-{}".format(prefix, architecture)
    if system == "Windows":
        return "win32-{}".format(architecture)
    raise RuntimeError("unsupported operating system: {}".format(system))


def request_headers(binary=False):
    headers = {
        "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def read_remote_json(url):
    request = urllib.request.Request(url, headers=request_headers())
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def release_asset(binary):
    release = read_remote_json(LATEST_RELEASE_URL)
    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not isinstance(tag, str) or VERSION_PATTERN.fullmatch(tag) is None:
        raise RuntimeError("GitHub returned an invalid Copilot release version")

    extension = ".zip" if binary.startswith("win32-") else ".tar.gz"
    asset_name = "copilot-{}{}".format(binary, extension)
    assets = release.get("assets")
    matches = [
        asset
        for asset in assets if isinstance(asset, dict) and asset.get("name") == asset_name
    ] if isinstance(assets, list) else []
    if len(matches) != 1:
        raise RuntimeError(
            "Copilot release {} does not contain exactly one {} asset".format(
                tag, asset_name
            )
        )
    asset = matches[0]
    digest = asset.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError("GitHub does not publish a SHA-256 digest for {}".format(asset_name))
    checksum = digest.split(":", 1)[1].lower()
    if CHECKSUM_PATTERN.fullmatch(checksum) is None:
        raise RuntimeError("GitHub returned an invalid SHA-256 digest for {}".format(asset_name))
    url = asset.get("browser_download_url")
    if not isinstance(url, str) or not url:
        raise RuntimeError("GitHub did not return a download URL for {}".format(asset_name))
    return {
        "version": tag,
        "name": asset_name,
        "url": url,
        "checksum": checksum,
    }


def format_bytes(value):
    units = ("B", "KiB", "MiB", "GiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            break
        amount /= 1024.0
    if unit == "B":
        return "{} {}".format(int(amount), unit)
    return "{:.1f} {}".format(amount, unit)


class DownloadProgress:
    def __init__(self, total):
        self.total = total
        self.is_terminal = sys.stdout.isatty()
        self.last_bucket = -1
        self.last_size_bucket = -1
        self.last_update = 0.0

    def update(self, downloaded, force=False):
        if self.is_terminal:
            now = time.monotonic()
            if not force and now - self.last_update < 0.1:
                return
            self.last_update = now
            if self.total:
                percent = min(100.0, downloaded * 100.0 / self.total)
                message = "Progress: {:5.1f}% ({} / {})".format(
                    percent, format_bytes(downloaded), format_bytes(self.total)
                )
            else:
                message = "Downloaded: {}".format(format_bytes(downloaded))
            print("\r{}".format(message), end="", flush=True)
            return
        if self.total:
            percent = min(100, int(downloaded * 100 / self.total))
            bucket = 100 if force else (percent // 10) * 10
            if bucket > self.last_bucket:
                self.last_bucket = bucket
                print(
                    "Progress: {}% ({} / {})".format(
                        bucket, format_bytes(downloaded), format_bytes(self.total)
                    ),
                    flush=True,
                )
        else:
            bucket = downloaded // (10 * 1024 * 1024)
            if bucket > self.last_size_bucket or force:
                self.last_size_bucket = bucket
                print("Downloaded: {}".format(format_bytes(downloaded)), flush=True)

    def finish(self, downloaded):
        self.update(downloaded, force=True)
        if self.is_terminal:
            print()


def download(url, destination):
    request = urllib.request.Request(url, headers=request_headers(binary=True))
    digest = hashlib.sha256()
    downloaded = 0
    with urllib.request.urlopen(request, timeout=300) as response:
        content_length = response.headers.get("Content-Length")
        total = int(content_length) if content_length and content_length.isdigit() else None
        progress = DownloadProgress(total)
        progress.update(0)
        with destination.open("wb") as output:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                progress.update(downloaded)
            output.flush()
            os.fsync(output.fileno())
        progress.finish(downloaded)
    return digest.hexdigest()


def extract_archive(archive_path, staging_dir, binary):
    if binary.startswith("win32-"):
        with zipfile.ZipFile(str(archive_path), "r") as archive:
            archive.extractall(str(staging_dir))
        command = staging_dir / "copilot.exe"
    else:
        with tarfile.open(str(archive_path), "r:gz") as archive:
            archive.extractall(str(staging_dir))
        command = staging_dir / "copilot"
        if command.is_file():
            command.chmod(
                stat.S_IRUSR
                | stat.S_IWUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
    if not command.is_file():
        raise RuntimeError("Copilot archive does not contain {}".format(command.name))
    return command


def read_state():
    try:
        with STATE_FILE.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise RuntimeError("could not read state file {}: {}".format(STATE_FILE, error))
    if not isinstance(state, dict):
        raise RuntimeError("state file must contain a JSON object: {}".format(STATE_FILE))
    return state


def read_installed_paths(state, key):
    paths = state.get(key, [])
    if not isinstance(paths, list) or any(
        not isinstance(path, str) or not Path(path).is_absolute() for path in paths
    ):
        raise RuntimeError("state {} must contain absolute paths".format(key))
    return [Path(path) for path in paths]


def remove_installed_paths(state):
    links = read_installed_paths(state, "installed_links")
    files = read_installed_paths(state, "installed_files")
    directories = read_installed_paths(state, "installed_directories")
    for path in sorted(links + files, key=lambda item: len(item.parts), reverse=True):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except FileNotFoundError:
            pass


def write_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(STATE_FILE.parent),
            prefix=".{}.".format(STATE_FILE.name),
            delete=False,
        ) as state_file:
            temporary_path = Path(state_file.name)
            json.dump(state, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(str(temporary_path), str(STATE_FILE))
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def create_symlink_value(path, link_target):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(".{}.{}.link".format(path.name, os.getpid()))
    try:
        temporary_path.unlink()
    except FileNotFoundError:
        pass
    try:
        os.symlink(link_target, str(temporary_path))
        os.replace(str(temporary_path), str(path))
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def staged_targets(staging_dir, install_dir):
    links = []
    files = []
    directories = []
    for source in sorted(staging_dir.rglob("*"), key=lambda item: str(item)):
        target = install_dir / source.relative_to(staging_dir)
        if source.is_symlink():
            links.append(target)
        elif source.is_dir():
            directories.append(target)
        elif source.is_file():
            files.append(target)
    return links, files, directories


def validate_targets(links, files, directories):
    for path in links + files:
        if os.path.lexists(str(path)) and path.is_dir() and not path.is_symlink():
            raise RuntimeError("target file is occupied by a directory: {}".format(path))
    for path in directories:
        if os.path.lexists(str(path)) and (
            not path.is_dir() or path.is_symlink()
        ):
            raise RuntimeError("target directory is occupied by a file: {}".format(path))


def copy_staged_package(staging_dir, install_dir):
    installed_links = []
    installed_files = []
    installed_directories = []
    install_dir.mkdir(parents=True, exist_ok=True)
    sources = sorted(
        staging_dir.rglob("*"),
        key=lambda item: (len(item.relative_to(staging_dir).parts), str(item)),
    )
    for source in sources:
        target = install_dir / source.relative_to(staging_dir)
        if source.is_symlink():
            create_symlink_value(target, os.readlink(str(source)))
            installed_links.append(str(target))
        elif source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            installed_directories.append(str(target))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=str(target.parent),
                    prefix=".{}.".format(target.name),
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                shutil.copy2(str(source), str(temporary_path))
                os.replace(str(temporary_path), str(target))
                temporary_path = None
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
            installed_files.append(str(target))
    return installed_links, installed_files, installed_directories


def state_matches(state, version, binary, install_dir, command):
    if (
        state.get("schema_version") != STATE_SCHEMA_VERSION
        or state.get("version") != version
        or state.get("binary") != binary
        or state.get("install_dir") != str(install_dir)
    ):
        return False
    links = read_installed_paths(state, "installed_links")
    files = read_installed_paths(state, "installed_files")
    directories = read_installed_paths(state, "installed_directories")
    return (
        command.is_file()
        and all(path.is_symlink() for path in links)
        and all(path.is_file() for path in files)
        and all(path.is_dir() for path in directories)
    )


def main():
    args = parse_args()
    config = load_config(args.config)
    binary = detect_binary() if config["binary"] == "auto" else config["binary"]
    release = release_asset(binary)
    command_name = "copilot.exe" if binary.startswith("win32-") else "copilot"
    command = config["install_dir"] / command_name
    state = read_state()

    print("Checking Copilot latest release for {}...".format(binary), flush=True)
    if not args.force and state_matches(
        state, release["version"], binary, config["install_dir"], command
    ):
        print("Copilot {} is already installed at {}.".format(release["version"], command))
        return

    temporary_root = Path(tempfile.mkdtemp(prefix="fetch-copilot-"))
    archive_path = temporary_root / release["name"]
    staging_dir = temporary_root / "package"
    staging_dir.mkdir()
    print(
        "Downloading Copilot {} ({}) for {}...".format(
            release["version"], binary, config["install_dir"]
        ),
        flush=True,
    )
    try:
        actual_checksum = download(release["url"], archive_path)
        if actual_checksum != release["checksum"]:
            raise RuntimeError(
                "SHA-256 mismatch: expected {}, got {}".format(
                    release["checksum"], actual_checksum
                )
            )
        extract_archive(archive_path, staging_dir, binary)
        links, files, directories = staged_targets(
            staging_dir, config["install_dir"]
        )
        validate_targets(links, files, directories)
        remove_installed_paths(state)
        installed_links, installed_files, installed_directories = (
            copy_staged_package(staging_dir, config["install_dir"])
        )
    finally:
        shutil.rmtree(str(temporary_root), ignore_errors=True)

    write_state(
        {
            "binary": binary,
            "channel": config["channel"],
            "install_dir": str(config["install_dir"]),
            "installed_directories": installed_directories,
            "installed_files": installed_files,
            "installed_links": installed_links,
            "schema_version": STATE_SCHEMA_VERSION,
            "version": release["version"],
        }
    )
    print("Installed Copilot {} at {}.".format(release["version"], command))
    print("Ensure {} is in PATH.".format(config["install_dir"]))


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        RuntimeError,
        tarfile.TarError,
        UnicodeDecodeError,
        urllib.error.URLError,
        ValueError,
        zipfile.BadZipFile,
    ) as error:
        print("Error: {}".format(error), file=sys.stderr)
        sys.exit(1)
