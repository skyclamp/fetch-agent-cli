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


RELEASES_BASE_URL = "https://releases.openai.com/codex"
LATEST_METADATA_URL = RELEASES_BASE_URL + "/channels/latest"
GITHUB_LATEST_URL = "https://api.github.com/repos/openai/codex/releases/latest"
GITHUB_RELEASE_BASE = "https://github.com/openai/codex/releases/download"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / ".state" / "fetch_codex.json"
STATE_SCHEMA_VERSION = 1
USER_AGENT = "fetch-cli-tools-codex-installer"
CHUNK_SIZE = 1024 * 1024
VERSION_PATTERN = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha(?:\.[0-9]+){0,2}|beta(?:\.[0-9]+)?))?$"
)
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_BINARIES = {
    "aarch64-apple-darwin",
    "aarch64-unknown-linux-musl",
    "x86_64-apple-darwin",
    "x86_64-unknown-linux-musl",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download and install the Codex CLI standalone package."
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
        help="download and replace Codex even when the tracked version matches",
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

    tool = data["tools"].get("codex")
    if not isinstance(tool, dict):
        raise RuntimeError("configuration must contain a 'tools.codex' object")

    channel = tool.get("channel", "latest")
    install_dir = tool.get("install_dir")
    bin_dir = tool.get("bin_dir")
    binary = tool.get("binary", "auto")

    if channel != "latest":
        raise RuntimeError("tools.codex.channel only supports 'latest'")
    if not isinstance(install_dir, str) or not install_dir:
        raise RuntimeError("tools.codex.install_dir must be a directory path")
    if not isinstance(bin_dir, str) or not bin_dir:
        raise RuntimeError("tools.codex.bin_dir must be a directory path")
    if not isinstance(binary, str) or (
        binary != "auto" and binary not in SUPPORTED_BINARIES
    ):
        raise RuntimeError(
            "tools.codex.binary must be 'auto' or one of: {}".format(
                ", ".join(sorted(SUPPORTED_BINARIES))
            )
        )

    return {
        "channel": channel,
        "install_dir": resolve_config_path(path, install_dir),
        "bin_dir": resolve_config_path(path, bin_dir),
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


def detect_binary():
    system = platform.system()
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        architecture = "x86_64"
    elif machine in ("arm64", "aarch64"):
        architecture = "aarch64"
    else:
        raise RuntimeError("unsupported CPU architecture: {}".format(machine))

    if system == "Darwin":
        if architecture == "x86_64" and is_rosetta_translated():
            architecture = "aarch64"
        return "{}-apple-darwin".format(architecture)
    if system == "Linux":
        return "{}-unknown-linux-musl".format(architecture)
    if system == "Windows":
        raise RuntimeError(
            "this script currently supports macOS and Linux; Codex uses a separate official Windows installer"
        )
    raise RuntimeError("unsupported operating system: {}".format(system))


def request(url):
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )


def read_remote_json(url):
    with urllib.request.urlopen(request(url), timeout=30) as response:
        return json.load(response)


def asset_digest(asset):
    digest = asset.get("digest") if isinstance(asset, dict) else None
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError("release asset does not contain a SHA-256 digest")
    value = digest.split(":", 1)[1].lower()
    if CHECKSUM_PATTERN.fullmatch(value) is None:
        raise RuntimeError("release asset contains an invalid SHA-256 digest")
    return value


def parse_release_metadata(metadata, binary, source_url):
    if not isinstance(metadata, dict):
        raise RuntimeError("release metadata must be a JSON object")
    tag = metadata.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("rust-v"):
        raise RuntimeError("release metadata contains an invalid tag")
    version = tag[len("rust-v") :]
    if VERSION_PATTERN.fullmatch(version) is None:
        raise RuntimeError("release metadata contains an invalid version: {!r}".format(version))

    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("release metadata does not contain assets")
    assets_by_name = {
        asset.get("name"): asset
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
    }
    package_name = "codex-package-{}.tar.gz".format(binary)
    checksum_name = "codex-package_SHA256SUMS"
    package = assets_by_name.get(package_name)
    checksum = assets_by_name.get(checksum_name)
    if not isinstance(package, dict) or not isinstance(checksum, dict):
        raise RuntimeError(
            "release {} does not contain the standalone package for {}".format(
                version, binary
            )
        )
    asset_digest(package)
    asset_digest(checksum)
    return {
        "version": version,
        "tag": tag,
        "package": package,
        "checksum": checksum,
        "source_url": source_url,
    }


def resolve_release(binary):
    errors = []
    for url in (LATEST_METADATA_URL, GITHUB_LATEST_URL):
        try:
            metadata = read_remote_json(url)
            return parse_release_metadata(metadata, binary, url)
        except (OSError, RuntimeError, ValueError, urllib.error.URLError) as error:
            errors.append("{}: {}".format(url, error))
    raise RuntimeError("could not resolve latest Codex release: {}".format("; ".join(errors)))


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


def github_asset_url(tag, name):
    return "{}/{}/{}".format(GITHUB_RELEASE_BASE, tag, name)


def download_verified(asset, tag, destination, expected_digest):
    name = asset["name"]
    urls = []
    primary_url = asset.get("browser_download_url")
    if isinstance(primary_url, str):
        urls.append(primary_url)
    fallback_url = github_asset_url(tag, name)
    if fallback_url not in urls:
        urls.append(fallback_url)

    errors = []
    for url in urls:
        try:
            actual_digest = download(url, destination)
        except (OSError, urllib.error.URLError) as error:
            errors.append("{}: {}".format(url, error))
            continue
        if actual_digest == expected_digest:
            return
        errors.append(
            "{}: checksum mismatch, expected {}, got {}".format(
                url, expected_digest, actual_digest
            )
        )
    raise RuntimeError("could not download or verify {}: {}".format(name, "; ".join(errors)))


def package_digest_from_manifest(path, package_name):
    with path.open(encoding="utf-8") as manifest:
        for line in manifest:
            parts = line.split()
            if len(parts) >= 2 and parts[1].lstrip("*") == package_name:
                digest = parts[0].lower()
                if CHECKSUM_PATTERN.fullmatch(digest) is not None:
                    return digest
    raise RuntimeError("checksum manifest does not contain {}".format(package_name))


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


def prepare_package(archive_path, staging_dir, binary, validate_runtime, version):
    with tarfile.open(str(archive_path), "r:gz") as archive:
        archive.extractall(str(staging_dir))

    executable_paths = [
        staging_dir / "bin" / "codex",
        staging_dir / "bin" / "codex-code-mode-host",
        staging_dir / "codex-path" / "rg",
    ]
    bwrap_path = staging_dir / "codex-resources" / "bwrap"
    if binary.endswith("unknown-linux-musl"):
        executable_paths.append(bwrap_path)
    for path in executable_paths:
        if not path.is_file():
            raise RuntimeError("Codex package is missing required file: {}".format(path))
        path.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
    if not (staging_dir / "codex-package.json").is_file():
        raise RuntimeError("Codex package is missing codex-package.json")

    codex_link = staging_dir / "codex"
    try:
        codex_link.unlink()
    except FileNotFoundError:
        pass
    os.symlink("bin/codex", str(codex_link))

    if validate_runtime:
        result = subprocess.run(
            [str(staging_dir / "bin" / "codex"), "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
        if result.returncode != 0 or not result.stdout.strip().endswith(version):
            raise RuntimeError("downloaded Codex package failed its version check")


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


def create_symlink(path, target):
    relative_target = os.path.relpath(str(target), str(path.parent))
    create_symlink_value(path, relative_target)


def copy_package(staging_dir, release_dir):
    installed_links = []
    installed_files = []
    installed_directories = [str(release_dir)]
    release_dir.mkdir(parents=True, exist_ok=True)
    sources = sorted(
        staging_dir.rglob("*"),
        key=lambda item: (len(item.relative_to(staging_dir).parts), str(item)),
    )
    for source in sources:
        target = release_dir / source.relative_to(staging_dir)
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


def state_matches(state, version, binary, install_dir, bin_dir):
    if (
        state.get("schema_version") != STATE_SCHEMA_VERSION
        or state.get("version") != version
        or state.get("binary") != binary
        or state.get("install_dir") != str(install_dir)
        or state.get("bin_dir") != str(bin_dir)
    ):
        return False
    links = read_installed_paths(state, "installed_links")
    files = read_installed_paths(state, "installed_files")
    directories = read_installed_paths(state, "installed_directories")
    return (
        bool(files)
        and bool(directories)
        and all(path.is_symlink() for path in links)
        and all(path.is_file() for path in files)
        and all(path.is_dir() for path in directories)
    )


def main():
    args = parse_args()
    config = load_config(args.config)
    detected_binary = detect_binary()
    binary = detected_binary if config["binary"] == "auto" else config["binary"]
    release = resolve_release(binary)
    version = release["version"]
    release_dir = config["install_dir"] / "{}-{}".format(version, binary)
    current_link = config["install_dir"].parent / "current"
    codex_entry = config["bin_dir"] / "codex"
    host_entry = config["bin_dir"] / "codex-code-mode-host"
    state = read_state()

    print("Checking Codex latest channel for {}...".format(binary), flush=True)
    if not args.force and state_matches(
        state, version, binary, config["install_dir"], config["bin_dir"]
    ):
        print("Codex {} is already installed at {}.".format(version, release_dir))
        return

    temporary_root = Path(tempfile.mkdtemp(prefix="fetch-codex-"))
    checksum_path = temporary_root / release["checksum"]["name"]
    archive_path = temporary_root / release["package"]["name"]
    staging_dir = temporary_root / "package"
    staging_dir.mkdir()
    print(
        "Downloading Codex {} ({}) for {}...".format(
            version, binary, config["install_dir"]
        ),
        flush=True,
    )
    try:
        download_verified(
            release["checksum"],
            release["tag"],
            checksum_path,
            asset_digest(release["checksum"]),
        )
        expected_package_digest = package_digest_from_manifest(
            checksum_path, release["package"]["name"]
        )
        download_verified(
            release["package"],
            release["tag"],
            archive_path,
            expected_package_digest,
        )
        prepare_package(
            archive_path,
            staging_dir,
            binary,
            config["binary"] == "auto",
            version,
        )

        remove_installed_paths(state)
        installed_links, installed_files, installed_directories = copy_package(
            staging_dir, release_dir
        )
        create_symlink(current_link, release_dir)
        installed_links.append(str(current_link))
        create_symlink(codex_entry, current_link / "bin" / "codex")
        installed_links.append(str(codex_entry))
        if binary.endswith("apple-darwin"):
            create_symlink(
                host_entry, current_link / "bin" / "codex-code-mode-host"
            )
            installed_links.append(str(host_entry))
    finally:
        shutil.rmtree(str(temporary_root), ignore_errors=True)

    write_state(
        {
            "bin_dir": str(config["bin_dir"]),
            "binary": binary,
            "channel": config["channel"],
            "install_dir": str(config["install_dir"]),
            "installed_directories": installed_directories,
            "installed_files": installed_files,
            "installed_links": installed_links,
            "schema_version": STATE_SCHEMA_VERSION,
            "version": version,
        }
    )
    print("Installed Codex {} at {}.".format(version, release_dir))
    print("Created symlink command entry at {}.".format(codex_entry))
    if binary.endswith("apple-darwin"):
        print("Created symlink command entry at {}.".format(host_entry))


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        tarfile.TarError,
        UnicodeDecodeError,
        urllib.error.URLError,
        ValueError,
    ) as error:
        print("Error: {}".format(error), file=sys.stderr)
        sys.exit(1)
