#!/usr/bin/env python3

import argparse
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


PRIMARY_BASE_URL = "https://x.ai/cli"
FALLBACK_BASE_URL = "https://storage.googleapis.com/grok-build-public-artifacts/cli"
UPSTREAM_CHANNEL = "stable"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / ".state" / "fetch_grok.json"
STATE_SCHEMA_VERSION = 1
USER_AGENT = "fetch-cli-tools-grok-installer"
CHUNK_SIZE = 1024 * 1024
VERSION_PATTERN = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z._]+)?$"
)
SUPPORTED_BINARIES = {
    "linux-aarch64",
    "linux-x86_64",
    "macos-aarch64",
    "macos-x86_64",
    "windows-aarch64",
    "windows-x86_64",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download and install Grok from the official release service."
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
        help="download and replace Grok even when the tracked version matches",
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

    tool = data["tools"].get("grok")
    if not isinstance(tool, dict):
        raise RuntimeError("configuration must contain a 'tools.grok' object")

    channel = tool.get("channel", "stable")
    install_dir = tool.get("install_dir")
    bin_dir = tool.get("bin_dir")
    binary = tool.get("binary", "auto")

    if channel != "stable":
        raise RuntimeError("tools.grok.channel only supports 'stable'")
    if not isinstance(install_dir, str) or not install_dir:
        raise RuntimeError("tools.grok.install_dir must be a directory path")
    if not isinstance(bin_dir, str) or not bin_dir:
        raise RuntimeError("tools.grok.bin_dir must be a directory path")
    if not isinstance(binary, str) or (
        binary != "auto" and binary not in SUPPORTED_BINARIES
    ):
        raise RuntimeError(
            "tools.grok.binary must be 'auto' or one of: {}".format(
                ", ".join(sorted(SUPPORTED_BINARIES))
            )
        )

    return {
        "channel": channel,
        "install_dir": resolve_config_path(path, install_dir),
        "bin_dir": resolve_config_path(path, bin_dir),
        "binary": binary,
    }


def detect_binary():
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin":
        operating_system = "macos"
    elif system == "Linux":
        operating_system = "linux"
    elif system == "Windows":
        operating_system = "windows"
    else:
        raise RuntimeError("unsupported operating system: {}".format(system))

    if machine in ("x86_64", "amd64"):
        architecture = "x86_64"
    elif machine in ("arm64", "aarch64"):
        architecture = "aarch64"
    else:
        raise RuntimeError("unsupported CPU architecture: {}".format(machine))

    return "{}-{}".format(operating_system, architecture)


def request(url):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def read_remote_text(url):
    with urllib.request.urlopen(request(url), timeout=30) as response:
        return response.read(256).decode("utf-8").strip()


def resolve_release():
    errors = []
    for base_url in (PRIMARY_BASE_URL, FALLBACK_BASE_URL):
        try:
            version = read_remote_text("{}/{}".format(base_url, UPSTREAM_CHANNEL))
        except (OSError, urllib.error.URLError) as error:
            errors.append("{}: {}".format(base_url, error))
            continue
        if VERSION_PATTERN.fullmatch(version) is None:
            errors.append("{} returned an invalid version: {!r}".format(base_url, version))
            continue
        return version, base_url
    raise RuntimeError("could not resolve Grok stable release: {}".format("; ".join(errors)))


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
                downloaded += len(chunk)
                progress.update(downloaded)
            output.flush()
            os.fsync(output.fileno())
        progress.finish(downloaded)


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


def entry_type():
    return "copy" if platform.system() == "Windows" else "symlink"


def validate_entry_path(path):
    if os.path.lexists(str(path)) and path.is_dir() and not path.is_symlink():
        raise RuntimeError("entry path is occupied by a directory: {}".format(path))


def create_entry(target, entry, kind):
    entry.parent.mkdir(parents=True, exist_ok=True)
    temporary_entry = entry.with_name(
        ".{}.{}.entry".format(entry.name, os.getpid())
    )
    try:
        temporary_entry.unlink()
    except FileNotFoundError:
        pass
    try:
        if kind == "symlink":
            relative_target = os.path.relpath(str(target), str(entry.parent))
            os.symlink(relative_target, str(temporary_entry))
        elif kind == "copy":
            shutil.copy2(str(target), str(temporary_entry))
        else:
            raise RuntimeError("unsupported entry type: {}".format(kind))
        os.replace(str(temporary_entry), str(entry))
    finally:
        try:
            temporary_entry.unlink()
        except FileNotFoundError:
            pass


def entry_matches(entry, target, kind):
    if kind == "symlink":
        return entry.is_symlink() and entry.resolve() == target.resolve()
    return kind == "copy" and entry.is_file()


def state_matches(state, version, binary, binary_path, entries, kind):
    expected_links = [str(entry) for entry in entries] if kind == "symlink" else []
    expected_files = [str(binary_path)]
    if kind == "copy":
        expected_files.extend(str(entry) for entry in entries)
    expected_entries = [
        {"path": str(entry), "target": str(binary_path), "type": kind}
        for entry in entries
    ]
    return (
        state.get("schema_version") == STATE_SCHEMA_VERSION
        and state.get("version") == version
        and state.get("channel") == "stable"
        and state.get("binary") == binary
        and state.get("installed_links") == expected_links
        and state.get("installed_files") == expected_files
        and state.get("installed_directories") == []
        and state.get("entries") == expected_entries
        and binary_path.is_file()
        and all(entry_matches(entry, binary_path, kind) for entry in entries)
    )


def build_binary(base_url, version, binary, staging_dir, validate_runtime):
    windows = binary.startswith("windows-")
    filename = "grok-{}{}".format(binary, ".exe" if windows else "")
    destination = staging_dir / filename
    artifact_base = "{}/grok-{}-{}".format(base_url, version, binary)
    urls = [artifact_base + ".exe", artifact_base] if windows else [artifact_base]
    errors = []
    for url in urls:
        try:
            download(url, destination)
            break
        except (OSError, urllib.error.URLError) as error:
            errors.append("{}: {}".format(url, error))
    else:
        raise RuntimeError("binary download failed: {}".format("; ".join(errors)))

    if not windows:
        destination.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
    if validate_runtime:
        result = subprocess.run(
            [str(destination), "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError("downloaded Grok binary failed its --version check")
    return destination


def main():
    args = parse_args()
    config = load_config(args.config)
    detected_binary = detect_binary()
    binary = detected_binary if config["binary"] == "auto" else config["binary"]
    version, base_url = resolve_release()
    windows = binary.startswith("windows-")
    binary_name = "grok-{}{}".format(binary, ".exe" if windows else "")
    binary_path = config["install_dir"] / binary_name
    entry_names = ("grok.exe", "agent.exe") if windows else ("grok", "agent")
    entries = [config["bin_dir"] / name for name in entry_names]
    kind = entry_type()
    state = read_state()

    print(
        "Checking Grok stable channel for {}...".format(binary),
        flush=True,
    )
    if not args.force and state_matches(
        state, version, binary, binary_path, entries, kind
    ):
        print("Grok {} is already installed at {}.".format(version, binary_path))
        return

    for entry in entries:
        validate_entry_path(entry)

    staging_dir = Path(tempfile.mkdtemp(prefix="fetch-grok-"))
    print(
        "Downloading Grok {} ({}) for {}...".format(
            version, binary, config["install_dir"]
        ),
        flush=True,
    )
    try:
        staged_binary = build_binary(
            base_url,
            version,
            binary,
            staging_dir,
            config["binary"] == "auto" and platform.system() != "Windows",
        )
        remove_installed_paths(state)
        install_binary(staged_binary, binary_path)
        for entry in entries:
            create_entry(binary_path, entry, kind)
    finally:
        shutil.rmtree(str(staging_dir), ignore_errors=True)

    installed_links = [str(entry) for entry in entries] if kind == "symlink" else []
    installed_files = [str(binary_path)]
    if kind == "copy":
        installed_files.extend(str(entry) for entry in entries)
    entry_records = [
        {"path": str(entry), "target": str(binary_path), "type": kind}
        for entry in entries
    ]
    write_state(
        {
            "binary": binary,
            "channel": config["channel"],
            "entries": entry_records,
            "install_dir": str(config["install_dir"]),
            "installed_directories": [],
            "installed_files": installed_files,
            "installed_links": installed_links,
            "schema_version": STATE_SCHEMA_VERSION,
            "version": version,
        }
    )
    print("Installed Grok {} at {}.".format(version, binary_path))
    for entry in entries:
        print("Created {} command entry at {}.".format(kind, entry))


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        RuntimeError,
        UnicodeDecodeError,
        urllib.error.URLError,
        ValueError,
    ) as error:
        print("Error: {}".format(error), file=sys.stderr)
        sys.exit(1)
