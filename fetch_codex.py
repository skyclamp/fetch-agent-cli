#!/usr/bin/env python3
"""Install one verified release as independent CLI and daemon packages.

Supports macOS, Linux and Windows using only the Python standard library.
CODEX_HOME and CODEX_INSTALL_DIR override the official default locations.
"""

import argparse
import contextlib
import errno
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
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath


RELEASES_BASE_URL = "https://releases.openai.com/codex"
LATEST_METADATA_URL = RELEASES_BASE_URL + "/channels/latest"
GITHUB_LATEST_URL = "https://api.github.com/repos/openai/codex/releases/latest"
GITHUB_RELEASE_BASE = "https://github.com/openai/codex/releases/download"
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / ".state" / "fetch_codex.json"
STATE_SCHEMA_VERSION = 3
USER_AGENT = "fetch-cli-tools-codex-installer"
CHUNK_SIZE = 1024 * 1024
VERSION_PATTERN = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-(?:alpha(?:\.[0-9]+){0,2}|beta(?:\.[0-9]+)?))?$"
)
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_BINARIES = {
    "aarch64-apple-darwin",
    "aarch64-pc-windows-msvc",
    "aarch64-unknown-linux-musl",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "x86_64-unknown-linux-musl",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Install the latest Codex CLI and its dedicated daemon package.",
        epilog=("Uses CODEX_HOME (default: ~/.codex) and CODEX_INSTALL_DIR "
                "(default: ~/.local/bin on Unix, LOCALAPPDATA/Programs/OpenAI/Codex/bin "
                "on Windows). Does not read config.json or restart a running daemon."),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="download and verify the package even when this version is installed",
    )
    parser.add_argument(
        "--binary", choices=["auto"] + sorted(SUPPORTED_BINARIES), default="auto",
        help="target platform (default: detect this computer)",
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


def installation_paths():
    """Use the official installer's locations without reading config.json."""
    home = Path.home()
    codex_home = Path(os.path.expandvars(os.environ.get("CODEX_HOME") or str(home / ".codex"))).expanduser().absolute()
    if platform.system() == "Windows":
        local_appdata = Path(os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local"))
        default_bin = local_appdata / "Programs" / "OpenAI" / "Codex" / "bin"
    else:
        default_bin = home / ".local" / "bin"
    bin_dir = Path(os.path.expandvars(os.environ.get("CODEX_INSTALL_DIR") or str(default_bin))).expanduser().absolute()
    return codex_home, bin_dir


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
        return "{}-pc-windows-msvc".format(architecture)
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
    if source_url == LATEST_METADATA_URL:
        package = dict(package, browser_download_url="{}/releases/{}/{}".format(
            RELEASES_BASE_URL, version, package_name
        ))
        checksum = dict(checksum, browser_download_url="{}/releases/{}/{}".format(
            RELEASES_BASE_URL, version, checksum_name
        ))
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


def is_within(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_archive_member(member, destination):
    """Also reject Windows paths when inspecting an archive on Unix."""
    name = member.name
    if ("\\" in name or ":" in name or PurePosixPath(name).is_absolute()
            or PureWindowsPath(name).drive or ".." in PurePosixPath(name).parts):
        raise RuntimeError("unsafe archive member: {}".format(name))
    root = Path(destination)
    path = root / name
    if not is_within(path, root):
        raise RuntimeError("archive member escapes its destination: {}".format(name))
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        raise RuntimeError("unsupported archive member: {}".format(name))
    if member.issym() or member.islnk():
        link = member.linkname
        if ("\\" in link or ":" in link or PurePosixPath(link).is_absolute()
                or PureWindowsPath(link).drive):
            raise RuntimeError("unsafe archive link: {}".format(name))
        target = (path.parent if member.issym() else root) / link
        if not is_within(target, root):
            raise RuntimeError("archive link escapes its destination: {}".format(name))
    return member


def extract_package(archive_path, staging_dir):
    with tarfile.open(str(archive_path), "r:gz") as archive:
        data_filter = getattr(tarfile, "data_filter", None)
        if data_filter is not None:
            def package_filter(member, destination):
                safe_archive_member(member, destination)
                return data_filter(member, destination)
            # Explicit filtering avoids the Python 3.12/3.13 deprecation warning
            # and keeps the same policy when Python 3.14 changes the default.
            archive.extractall(str(staging_dir), filter=package_filter)
        else:
            # Older Python versions have no filter argument. Validate all member
            # types first, then resolve paths again immediately before extraction.
            members = archive.getmembers()
            for member in members:
                safe_archive_member(member, staging_dir)
            for member in members:
                safe_archive_member(member, staging_dir)
                archive.extract(member, str(staging_dir))


def package_executables(package_dir, binary, version):
    metadata = read_json_file(package_dir / "codex-package.json", "package manifest")
    windows = binary.endswith("pc-windows-msvc")
    entrypoint = "bin/codex.exe" if windows else "bin/codex"
    if (not isinstance(metadata, dict) or metadata.get("layoutVersion") != 1
            or metadata.get("version") != version or metadata.get("target") != binary
            or metadata.get("entrypoint") != entrypoint):
        raise RuntimeError("Codex package manifest does not match {} ({})".format(version, binary))
    if windows:
        executable_paths = [
            package_dir / "bin" / "codex.exe",
            package_dir / "bin" / "codex-code-mode-host.exe",
            package_dir / "codex-path" / "rg.exe",
            package_dir / "codex-resources" / "codex-command-runner.exe",
            package_dir / "codex-resources" / "codex-windows-sandbox-setup.exe",
        ]
    else:
        executable_paths = [
            package_dir / "bin" / "codex",
            package_dir / "bin" / "codex-code-mode-host",
            package_dir / "codex-path" / "rg",
        ]
        if binary.endswith("unknown-linux-musl"):
            executable_paths.append(package_dir / "codex-resources" / "bwrap")
    if "execServer" in metadata:
        expected = "bin/codex-exec-server.exe" if windows else "bin/codex-exec-server"
        if metadata["execServer"] != expected:
            raise RuntimeError("unexpected executor path in Codex package manifest")
        executable_paths.append(package_dir / expected)
    for path in executable_paths:
        if not path.is_file() or not is_within(path, package_dir):
            raise RuntimeError("Codex package is missing required file: {}".format(path))
    return executable_paths


def check_runtime(command, version):
    result = subprocess.run(
        [str(command), "--version"], check=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = result.stdout.strip().split()
    if result.returncode != 0 or not output or output[-1] != version:
        raise RuntimeError("Codex package failed its version check: {}".format(command))


def package_is_complete(package_dir, binary, version, validate_runtime):
    try:
        paths = package_executables(package_dir, binary, version)
        if not binary.endswith("pc-windows-msvc") and any(
            not path.stat().st_mode & stat.S_IXUSR for path in paths
        ):
            return False
        if validate_runtime:
            check_runtime(paths[0], version)
        return True
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return False


def prepare_package(archive_path, staging_dir, binary, validate_runtime, version):
    extract_package(archive_path, staging_dir)
    executable_paths = package_executables(staging_dir, binary, version)
    if not binary.endswith("pc-windows-msvc"):
        for path in executable_paths:
            path.chmod(0o755)
        codex_link = staging_dir / "codex"
        try:
            codex_link.unlink()
        except FileNotFoundError:
            pass
        os.symlink("bin/codex", str(codex_link))
    if validate_runtime:
        check_runtime(executable_paths[0], version)


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


def windows_file(path, disposition, flags=0):
    """Open an exclusive Windows handle, matching the official installer lock."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(path), 0xC0000000, 0, None, disposition, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


def create_junction(path, target):
    """Create a directory junction without administrator rights or cmd escaping."""
    import ctypes
    import struct
    from ctypes import wintypes

    path.mkdir()
    try:
        kernel, handle = windows_file(path, 3, 0x02200000)
        try:
            name = str(target.absolute())
            if name.startswith("\\\\?\\UNC\\"):
                name = "\\\\" + name[8:]
            elif name.startswith("\\\\?\\"):
                name = name[4:]
            substitute = "\\??\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\??\\" + name
            substitute_bytes = substitute.encode("utf-16-le")
            print_bytes = name.encode("utf-16-le")
            names = substitute_bytes + b"\0\0" + print_bytes + b"\0\0"
            buffer = struct.pack("<IHHHHHH", 0xA0000003, len(names) + 8, 0,
                                 0, len(substitute_bytes), len(substitute_bytes) + 2,
                                 len(print_bytes)) + names
            kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               wintypes.LPVOID, wintypes.DWORD,
                                               wintypes.LPVOID, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
            kernel.DeviceIoControl.restype = wintypes.BOOL
            returned = wintypes.DWORD()
            data = ctypes.create_string_buffer(buffer)
            if not kernel.DeviceIoControl(handle, 0x000900A4, data, len(buffer),
                                          None, 0, ctypes.byref(returned), None):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
    except BaseException:
        path.rmdir()
        raise


def is_link(path):
    if path.is_symlink():
        return True
    try:
        return getattr(path.lstat(), "st_reparse_tag", 0) == 0xA0000003
    except FileNotFoundError:
        return False


def validate_link(path, allowed_root):
    if os.path.lexists(str(path)) and (
        not is_link(path) or not is_within(path, allowed_root)
    ):
        raise RuntimeError("refusing to replace an unmanaged path: {}".format(path))


def remove_link(path):
    if not is_link(path):
        raise RuntimeError("refusing to remove a non-link: {}".format(path))
    if path.is_symlink():
        path.unlink()
    else:
        path.rmdir()  # Removes only the junction, never its target.


def rename_with_retry(source, destination):
    """Allow Windows to release transient executable and scanner handles."""
    deadline = time.monotonic() + 5
    while True:
        try:
            source.rename(destination)
            return
        except OSError as error:
            if (os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33)
                    or time.monotonic() >= deadline):
                raise
            time.sleep(0.1)


def replace_link(path, target, windows):
    if is_link(path) and path.resolve() == target.resolve():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not windows:
        create_symlink(path, target)
        return
    temporary = path.with_name(".{}.{}.link".format(path.name, uuid.uuid4().hex))
    previous = path.with_name(".{}.{}.previous".format(path.name, uuid.uuid4().hex))
    create_junction(temporary, target)
    had_previous = False
    try:
        if os.path.lexists(str(path)):
            rename_with_retry(path, previous)
            had_previous = True
        try:
            rename_with_retry(temporary, path)
        except BaseException:
            if had_previous:
                rename_with_retry(previous, path)
            raise
    finally:
        if os.path.lexists(str(temporary)):
            remove_link(temporary)
    if had_previous:
        remove_link(previous)


@contextlib.contextmanager
def install_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 30
    if os.name == "nt":
        while True:
            try:
                kernel, handle = windows_file(root / "install.lock", 4)
                break
            except OSError as error:
                if getattr(error, "winerror", None) not in (5, 32, 33):
                    raise
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out waiting for installer lock: {}".format(root)) from error
                time.sleep(0.1)
        try:
            yield
        finally:
            kernel.CloseHandle(handle)
        return

    import fcntl
    use_lockf = platform.system() == "Darwin" and shutil.which("lockf") is not None
    if use_lockf or shutil.which("flock"):
        lock_function = fcntl.lockf if use_lockf else fcntl.flock
        with (root / "install.lock").open("a+b") as lock_file:
            while True:
                try:
                    lock_function(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    if time.monotonic() >= deadline:
                        raise RuntimeError("timed out waiting for installer lock: {}".format(root)) from error
                    time.sleep(0.1)
            try:
                yield
            finally:
                lock_function(lock_file.fileno(), fcntl.LOCK_UN)
        return

    # Match the shell installer's directory-lock fallback. Publish the PID and
    # timestamp before the link so another installer cannot see an empty owner.
    with tempfile.TemporaryDirectory(prefix="install-lock-", dir=str(root)) as owner:
        Path(owner, "pid").write_text(str(os.getpid()), encoding="ascii")
        Path(owner, "started_at").write_text(str(int(time.time())), encoding="ascii")
        directory = root / "install.lock.d"
        while True:
            try:
                os.symlink(owner, str(directory), target_is_directory=True)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out waiting for installer lock: {}".format(root))
                time.sleep(0.1)
        try:
            yield
        finally:
            directory.unlink()


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        if is_link(path):
            if not is_within(path, root):
                raise RuntimeError("package link escapes its root: {}".format(path))
            digest.update(b"link\0" + os.readlink(str(path)).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"directory\0")
        else:
            raise RuntimeError("unsupported package entry: {}".format(path))
    return digest.digest()


def publish_release(source, root, version, binary, validate_runtime, force):
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / "{}-{}".format(version, binary)
    if is_link(destination) or destination.resolve().parent != releases.resolve():
        raise RuntimeError("invalid release directory: {}".format(destination))
    complete = package_is_complete(destination, binary, version, validate_runtime)
    if complete and (not force or tree_digest(source) == tree_digest(destination)):
        return destination
    if destination.exists() and not destination.is_dir():
        raise RuntimeError("release path is not a directory: {}".format(destination))
    # Cached official installations can be reused without our old .state file;
    # inspect their extra resources and links as well before copying them.
    tree_digest(source)
    with tempfile.TemporaryDirectory(prefix=".staging.", dir=str(releases)) as temporary:
        staged = Path(temporary) / "package"
        shutil.copytree(str(source), str(staged), symlinks=True)
        # Executing from staging can leave short-lived Windows image/scanner
        # handles behind and prevent the directory rename. Check its contents
        # here, then run the binary at its final path before selecting it.
        if not package_is_complete(staged, binary, version, False):
            raise RuntimeError("staged package failed validation: {}".format(staged))
        previous = None
        if destination.exists():
            # Keep the old bytes for running processes and recovery. No old
            # package files are deleted, including during same-version repair.
            previous = releases / (".replaced." + uuid.uuid4().hex)
            rename_with_retry(destination, previous)
        published = False
        try:
            rename_with_retry(staged, destination)
            published = True
            if validate_runtime:
                check_runtime(package_executables(destination, binary, version)[0], version)
        except BaseException:
            if published:
                rename_with_retry(destination, staged)
            if previous is not None:
                rename_with_retry(previous, destination)
            raise
    return destination


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".{}.".format(path.name), delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(str(temporary), str(path))
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink()


def select_packages(roots, release_dirs, bin_dir, binary, version):
    windows = binary.endswith("pc-windows-msvc")
    links = [(root / "current", release_dir, root / "releases")
             for root, release_dir in zip(roots, release_dirs)]
    if windows:
        links.append((bin_dir, roots[0] / "current" / "bin", roots[0]))
    else:
        links.append((bin_dir / "codex", roots[0] / "current" / "bin" / "codex", roots[0]))
        if binary.endswith("apple-darwin"):
            links.append((bin_dir / "codex-code-mode-host",
                          roots[0] / "current" / "bin" / "codex-code-mode-host", roots[0]))
    for path, target, allowed_root in links:
        validate_link(path, allowed_root)
    old_links = []
    old_markers = []
    try:
        for path, target, allowed_root in links:
            old_target = os.readlink(str(path)) if is_link(path) else None
            replace_link(path, target, windows)
            old_links.append((path, old_target))
        name = "{}-{}".format(version, binary).encode("ascii")
        for root in roots:
            marker = root / "auto-update-version"
            old_data = marker.read_bytes() if marker.exists() else None
            atomic_write(marker, name)
            old_markers.append((marker, old_data))
    except BaseException:
        for marker, old_data in reversed(old_markers):
            if old_data is None:
                marker.unlink()
            else:
                atomic_write(marker, old_data)
        for path, old_target in reversed(old_links):
            if old_target is None:
                remove_link(path)
            else:
                target = Path(old_target)
                replace_link(path, target if target.is_absolute() else path.parent / target, windows)
        raise


def main():
    args = parse_args()
    detected_binary = detect_binary()
    binary = detected_binary if args.binary == "auto" else args.binary
    if binary.endswith("pc-windows-msvc") != (os.name == "nt"):
        raise RuntimeError("the selected binary must use this operating system's link format")
    validate_runtime = binary == detected_binary
    codex_home, bin_dir = installation_paths()
    roots = [codex_home / "packages" / "standalone", codex_home / "packages" / "app-server-daemon"]
    # Inspect the entry's parent: an existing Windows bin junction is expected
    # to resolve into packages, while the entry itself must live outside it.
    if is_within(bin_dir.parent, codex_home / "packages") or bin_dir == codex_home / "packages":
        raise RuntimeError("CODEX_INSTALL_DIR must be outside the package directories")
    print("Checking Codex latest channel for {}...".format(binary), flush=True)
    release = resolve_release(binary)
    version = release["version"]
    name = "{}-{}".format(version, binary)
    with contextlib.ExitStack() as stack:
        for root in sorted(roots):
            stack.enter_context(install_lock(root))
        source = None
        if not args.force:
            for root in roots:
                candidate = root / "releases" / name
                if package_is_complete(candidate, binary, version, validate_runtime):
                    source = candidate
                    print("Reusing Codex {} at {}.".format(version, source), flush=True)
                    break
        if source is None:
            temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="fetch-codex-")))
            checksum_path = temporary / release["checksum"]["name"]
            archive_path = temporary / release["package"]["name"]
            source = temporary / "package"
            source.mkdir()
            print("Downloading Codex {} ({})...".format(version, binary), flush=True)
            download_verified(release["checksum"], release["tag"], checksum_path,
                              asset_digest(release["checksum"]))
            expected_digest = package_digest_from_manifest(checksum_path, release["package"]["name"])
            download_verified(release["package"], release["tag"], archive_path, expected_digest)
            prepare_package(archive_path, source, binary, validate_runtime, version)
        release_dirs = [publish_release(source, root, version, binary, validate_runtime, args.force)
                        for root in roots]
        select_packages(roots, release_dirs, bin_dir, binary, version)
        write_state({"schema_version": STATE_SCHEMA_VERSION, "version": version,
                     "binary": binary, "channel": "latest", "bin_dir": str(bin_dir),
                     "package_roots": [str(root) for root in roots]})
    print("CLI package: {}".format(release_dirs[0]))
    print("Daemon package: {}".format(release_dirs[1]))
    command = bin_dir / ("codex.exe" if os.name == "nt" else "codex")
    print("Command: {}".format(command))
    if str(bin_dir).casefold() not in {entry.casefold() for entry in os.get_exec_path()}:
        print("Add {} to PATH.".format(bin_dir))
    print("An already running daemon uses its current process until restarted.")


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
