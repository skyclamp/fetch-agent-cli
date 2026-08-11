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
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_URL = "https://downloads.claude.ai/claude-code-releases"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / ".state" / "fetch_claude_code.json"
USER_AGENT = "fetch-cli-tools-claude-code-installer"
CHUNK_SIZE = 1024 * 1024
STATE_SCHEMA_VERSION = 5
VERSION_PATTERN = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z._-]+)?$"
)
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_BINARIES = {
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "linux-arm64-musl",
    "linux-x64-musl",
    "win32-arm64",
    "win32-x64",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download and install Claude Code from the official release service."
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
        help="download and replace the package even when the tracked version matches",
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


def load_config(path):
    data = read_json_file(path.expanduser(), "configuration file")
    if not isinstance(data, dict) or not isinstance(data.get("tools"), dict):
        raise RuntimeError("configuration must contain a 'tools' object")

    tool = data["tools"].get("claude_code")
    if not isinstance(tool, dict):
        raise RuntimeError("configuration must contain a 'tools.claude_code' object")

    channel = tool.get("channel", "latest")
    install_dir = tool.get("install_dir")
    bin_dir = tool.get("bin_dir")
    binary = tool.get("binary", "auto")

    if not isinstance(channel, str) or not channel:
        raise RuntimeError("tools.claude_code.channel must be a non-empty string")
    if channel not in ("stable", "latest") and VERSION_PATTERN.fullmatch(channel) is None:
        raise RuntimeError(
            "tools.claude_code.channel must be 'stable', 'latest', or a version"
        )
    if not isinstance(install_dir, str) or not install_dir:
        raise RuntimeError("tools.claude_code.install_dir must be a directory path")
    if not isinstance(bin_dir, str) or not bin_dir:
        raise RuntimeError("tools.claude_code.bin_dir must be a directory path")
    if not isinstance(binary, str) or (
        binary != "auto" and binary not in SUPPORTED_BINARIES
    ):
        raise RuntimeError(
            "tools.claude_code.binary must be 'auto' or one of: {}".format(
                ", ".join(sorted(SUPPORTED_BINARIES))
            )
        )

    install_path = Path(install_dir).expanduser()
    if not install_path.is_absolute():
        install_path = path.expanduser().resolve().parent / install_path
    bin_path = Path(bin_dir).expanduser()
    if not bin_path.is_absolute():
        bin_path = path.expanduser().resolve().parent / bin_path

    return {
        "channel": channel,
        "install_dir": install_path,
        "bin_dir": bin_path,
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
        suffix = "-musl" if is_musl_linux() else ""
        return "linux-{}{}".format(architecture, suffix)

    if system == "Windows":
        return "win32-{}".format(architecture)

    raise RuntimeError("unsupported operating system: {}".format(system))


def request(url):
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def read_remote_text(url):
    with urllib.request.urlopen(request(url), timeout=30) as response:
        value = response.read(256).decode("utf-8").strip()
    return value


def read_remote_json(url):
    with urllib.request.urlopen(request(url), timeout=30) as response:
        return json.load(response)


def resolve_version(channel):
    if VERSION_PATTERN.fullmatch(channel) is not None:
        return channel

    version = read_remote_text("{}/{}".format(BASE_URL, channel))
    if VERSION_PATTERN.fullmatch(version) is None:
        raise RuntimeError(
            "release service returned an invalid version for {}: {!r}".format(
                channel, version
            )
        )
    return version


def get_artifact(version, binary):
    manifest_url = "{}/{}/manifest.json".format(BASE_URL, version)
    manifest = read_remote_json(manifest_url)
    if not isinstance(manifest, dict):
        raise RuntimeError("release manifest must be a JSON object")

    platforms = manifest.get("platforms")
    artifact = platforms.get(binary) if isinstance(platforms, dict) else None
    if not isinstance(artifact, dict):
        raise RuntimeError(
            "binary {} is not available for version {}".format(binary, version)
        )

    checksum = artifact.get("checksum")
    artifact_name = "claude.exe" if binary.startswith("win32-") else "claude"
    if not isinstance(checksum, str) or CHECKSUM_PATTERN.fullmatch(checksum) is None:
        raise RuntimeError("manifest contains an invalid SHA-256 checksum")

    return {
        "name": artifact_name,
        "install_name": "claude-{}-{}{}".format(
            version,
            binary,
            ".exe" if artifact_name == "claude.exe" else "",
        ),
        "url": "{}/{}/{}/{}".format(BASE_URL, version, binary, artifact_name),
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
            bucket = (percent // 10) * 10
            if force:
                bucket = 100
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
    digest = hashlib.sha256()
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


def staged_targets(staging_dir, install_dir):
    links = []
    files = []
    directories = []
    for source in sorted(staging_dir.rglob("*"), key=lambda item: str(item)):
        relative_path = source.relative_to(staging_dir)
        target = install_dir / relative_path
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
    sources = sorted(
        staging_dir.rglob("*"),
        key=lambda item: (len(item.relative_to(staging_dir).parts), str(item)),
    )
    install_dir.mkdir(parents=True, exist_ok=True)

    for source in sources:
        relative_path = source.relative_to(staging_dir)
        target = install_dir / relative_path
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = target.with_name(
                ".{}.{}.link".format(target.name, os.getpid())
            )
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            try:
                os.symlink(
                    os.readlink(str(source)),
                    str(temporary_path),
                    target_is_directory=source.is_dir(),
                )
                os.replace(str(temporary_path), str(target))
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
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


def entry_type():
    return "copy" if platform.system() == "Windows" else "symlink"


def validate_entry_path(path):
    if os.path.lexists(str(path)) and path.is_dir() and not path.is_symlink():
        raise RuntimeError("entry path is occupied by a directory: {}".format(path))


def create_entry(target, bin_dir, name, kind):
    bin_dir.mkdir(parents=True, exist_ok=True)
    entry = bin_dir / name
    temporary_entry = entry.with_name(
        ".{}.{}.link".format(entry.name, os.getpid())
    )
    try:
        temporary_entry.unlink()
    except FileNotFoundError:
        pass

    try:
        if kind == "symlink":
            relative_target = os.path.relpath(str(target), str(bin_dir))
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
    return entry


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


def entry_matches(entry, target, kind):
    if kind == "symlink":
        return entry.is_symlink() and entry.resolve() == target.resolve()
    if kind == "copy":
        return entry.is_file() and target.is_file()
    return False


def state_matches(
    state,
    version,
    binary,
    install_dir,
    destination,
    entry,
    kind,
    checksum,
):
    installed_links = read_installed_paths(state, "installed_links")
    installed_files = read_installed_paths(state, "installed_files")
    installed_directories = read_installed_paths(state, "installed_directories")
    expected_links = [str(entry)] if kind == "symlink" else []
    expected_files = [str(destination)]
    if kind == "copy":
        expected_files.append(str(entry))
    return (
        state.get("schema_version") == STATE_SCHEMA_VERSION
        and destination.is_file()
        and state.get("version") == version
        and state.get("binary") == binary
        and state.get("checksum") == checksum
        and state.get("install_dir") == str(install_dir)
        and state.get("installed_links") == expected_links
        and state.get("installed_files") == expected_files
        and state.get("entry")
        == {
            "path": str(entry),
            "target": str(destination),
            "type": kind,
        }
        and entry_matches(entry, destination, kind)
        and all(path.is_symlink() for path in installed_links)
        and all(path.is_file() for path in installed_files)
        and all(path.is_dir() for path in installed_directories)
    )


def build_package(artifact, staging_dir):
    destination = staging_dir / artifact["install_name"]
    actual_checksum = download(artifact["url"], destination)
    if actual_checksum != artifact["checksum"]:
        raise RuntimeError(
            "SHA-256 mismatch: expected {}, got {}".format(
                artifact["checksum"], actual_checksum
            )
        )

    if artifact["name"] != "claude.exe":
        destination.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
    return destination


def main():
    args = parse_args()
    config = load_config(args.config)
    binary = detect_binary() if config["binary"] == "auto" else config["binary"]

    print(
        "Checking Claude Code {} channel for {}...".format(
            config["channel"], binary
        ),
        flush=True,
    )
    version = resolve_version(config["channel"])
    artifact = get_artifact(version, binary)
    install_dir = config["install_dir"]
    bin_dir = config["bin_dir"]
    destination = install_dir / artifact["install_name"]
    entry = bin_dir / artifact["name"]
    kind = entry_type()
    state = read_state()

    if not args.force and state_matches(
        state,
        version,
        binary,
        install_dir,
        destination,
        entry,
        kind,
        artifact["checksum"],
    ):
        print("Claude Code {} is already installed at {}.".format(version, destination))
        return

    staging_dir = Path(
        tempfile.mkdtemp(
            prefix="fetch-claude-code-",
        )
    )
    print(
        "Downloading Claude Code {} ({}) for {}...".format(
            version, binary, install_dir
        ),
        flush=True,
    )
    try:
        build_package(artifact, staging_dir)
        target_links, target_files, target_directories = staged_targets(
            staging_dir, install_dir
        )
        validate_targets(target_links, target_files, target_directories)
        validate_entry_path(entry)
        remove_installed_paths(state)
        installed_links, installed_files, installed_directories = (
            copy_staged_package(staging_dir, install_dir)
        )
        create_entry(destination, bin_dir, artifact["name"], kind)
        if kind == "symlink":
            installed_links.append(str(entry))
        else:
            installed_files.append(str(entry))
    finally:
        shutil.rmtree(str(staging_dir), ignore_errors=True)

    write_state(
        {
            "binary": binary,
            "channel": config["channel"],
            "checksum": artifact["checksum"],
            "entry": {
                "path": str(entry),
                "target": str(destination),
                "type": kind,
            },
            "install_dir": str(install_dir),
            "installed_directories": installed_directories,
            "installed_files": installed_files,
            "installed_links": installed_links,
            "schema_version": STATE_SCHEMA_VERSION,
            "version": version,
        }
    )
    print("Installed Claude Code {} at {}.".format(version, destination))
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
