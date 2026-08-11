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
from pathlib import Path


BASE_URL = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / ".state" / "fetch_antigravity.json"
STATE_SCHEMA_VERSION = 1
USER_AGENT = "fetch-cli-tools-antigravity-installer"
CHUNK_SIZE = 1024 * 1024
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z._-]+)?$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{128}$")
SUPPORTED_BINARIES = {
    "darwin_amd64",
    "darwin_arm64",
    "linux_amd64",
    "linux_amd64_musl",
    "linux_arm64",
    "linux_arm64_musl",
    "windows_amd64",
    "windows_arm64",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download and install Antigravity CLI from platform manifests."
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
        help="download and replace Antigravity even when the tracked version matches",
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
    expanded = os.path.expandvars(value)
    result = Path(expanded).expanduser()
    if not result.is_absolute():
        result = config_path.expanduser().resolve().parent / result
    return result


def configured_install_dir(config_path, value):
    if isinstance(value, str) and value:
        return resolve_config_path(config_path, value)
    if not isinstance(value, dict):
        raise RuntimeError(
            "tools.antigravity.install_dir must be a path or platform path object"
        )
    system = platform.system().lower()
    selected = value.get(system)
    if not isinstance(selected, str) or not selected:
        raise RuntimeError(
            "tools.antigravity.install_dir does not contain a path for {}".format(
                system
            )
        )
    return resolve_config_path(config_path, selected)


def load_config(path):
    data = read_json_file(path.expanduser(), "configuration file")
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        raise RuntimeError("configuration must contain a 'tools' object")
    tool = data["tools"].get("antigravity")
    if not isinstance(tool, dict):
        raise RuntimeError("configuration must contain a 'tools.antigravity' object")

    channel = tool.get("channel", "latest")
    install_dir = tool.get("install_dir")
    binary = tool.get("binary", "auto")
    if channel != "latest":
        raise RuntimeError("tools.antigravity.channel only supports 'latest'")
    if not isinstance(binary, str) or (
        binary != "auto" and binary not in SUPPORTED_BINARIES
    ):
        raise RuntimeError(
            "tools.antigravity.binary must be 'auto' or one of: {}".format(
                ", ".join(sorted(SUPPORTED_BINARIES))
            )
        )
    return {
        "channel": channel,
        "install_dir": configured_install_dir(path, install_dir),
        "binary": binary,
    }


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
        architecture = "amd64"
    elif machine in ("arm64", "aarch64"):
        architecture = "arm64"
    else:
        raise RuntimeError("unsupported CPU architecture: {}".format(machine))

    if system == "Darwin":
        return "darwin_{}".format(architecture)
    if system == "Linux":
        suffix = "_musl" if is_musl_linux() else ""
        return "linux_{}{}".format(architecture, suffix)
    if system == "Windows":
        return "windows_{}".format(architecture)
    raise RuntimeError("unsupported operating system: {}".format(system))


def request(url):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def release_manifest(binary):
    url = "{}/manifests/{}.json".format(BASE_URL, binary)
    try:
        with urllib.request.urlopen(request(url), timeout=30) as response:
            manifest = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise RuntimeError(
                "Antigravity does not currently publish a release manifest for {}".format(
                    binary
                )
            )
        raise
    if not isinstance(manifest, dict):
        raise RuntimeError("release manifest must be a JSON object")
    version = manifest.get("version")
    download_url = manifest.get("url")
    checksum = manifest.get("sha512")
    if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
        raise RuntimeError("release manifest contains an invalid version")
    if not isinstance(download_url, str) or not download_url:
        raise RuntimeError("release manifest does not contain a download URL")
    if not isinstance(checksum, str) or CHECKSUM_PATTERN.fullmatch(checksum.lower()) is None:
        raise RuntimeError("release manifest contains an invalid SHA-512 checksum")
    return {
        "version": version,
        "url": download_url,
        "checksum": checksum.lower(),
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
    digest = hashlib.sha512()
    downloaded = 0
    with urllib.request.urlopen(request(url), timeout=300) as response:
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


def prepare_binary(payload, staging_dir, binary, url):
    command_name = "agy.exe" if binary.startswith("windows_") else "agy"
    command = staging_dir / command_name
    if url.split("?", 1)[0].endswith(".tar.gz"):
        with tarfile.open(str(payload), "r:gz") as archive:
            try:
                source = archive.extractfile("antigravity")
            except KeyError:
                source = None
            if source is None:
                raise RuntimeError("Antigravity archive does not contain antigravity")
            with command.open("wb") as output:
                shutil.copyfileobj(source, output, CHUNK_SIZE)
                output.flush()
                os.fsync(output.fileno())
    else:
        shutil.copy2(str(payload), str(command))

    if not binary.startswith("windows_"):
        command.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
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


def install_binary(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(destination.parent),
            prefix=".{}.".format(destination.name),
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        shutil.copy2(str(source), str(temporary_path))
        os.replace(str(temporary_path), str(destination))
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def state_matches(state, version, binary, install_dir, command):
    return (
        state.get("schema_version") == STATE_SCHEMA_VERSION
        and state.get("version") == version
        and state.get("binary") == binary
        and state.get("install_dir") == str(install_dir)
        and state.get("installed_files") == [str(command)]
        and state.get("installed_links") == []
        and state.get("installed_directories") == []
        and command.is_file()
    )


def main():
    args = parse_args()
    config = load_config(args.config)
    binary = detect_binary() if config["binary"] == "auto" else config["binary"]
    release = release_manifest(binary)
    command_name = "agy.exe" if binary.startswith("windows_") else "agy"
    command = config["install_dir"] / command_name
    state = read_state()

    print("Checking Antigravity latest release for {}...".format(binary), flush=True)
    if not args.force and state_matches(
        state, release["version"], binary, config["install_dir"], command
    ):
        print(
            "Antigravity {} is already installed at {}.".format(
                release["version"], command
            )
        )
        return

    temporary_root = Path(tempfile.mkdtemp(prefix="fetch-antigravity-"))
    payload = temporary_root / "payload"
    staging_dir = temporary_root / "package"
    staging_dir.mkdir()
    print(
        "Downloading Antigravity {} ({}) for {}...".format(
            release["version"], binary, config["install_dir"]
        ),
        flush=True,
    )
    try:
        actual_checksum = download(release["url"], payload)
        if actual_checksum != release["checksum"]:
            raise RuntimeError(
                "SHA-512 mismatch: expected {}, got {}".format(
                    release["checksum"], actual_checksum
                )
            )
        staged_command = prepare_binary(
            payload, staging_dir, binary, release["url"]
        )
        remove_installed_paths(state)
        install_binary(staged_command, command)
    finally:
        shutil.rmtree(str(temporary_root), ignore_errors=True)

    write_state(
        {
            "binary": binary,
            "channel": config["channel"],
            "install_dir": str(config["install_dir"]),
            "installed_directories": [],
            "installed_files": [str(command)],
            "installed_links": [],
            "schema_version": STATE_SCHEMA_VERSION,
            "version": release["version"],
        }
    )
    print("Installed Antigravity {} at {}.".format(release["version"], command))
    print("Ensure {} is in PATH.".format(config["install_dir"]))
    print("Run '{} install' if you want Antigravity to configure your shell.".format(command))


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
    ) as error:
        print("Error: {}".format(error), file=sys.stderr)
        sys.exit(1)
