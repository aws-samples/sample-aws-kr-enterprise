"""Build an AgentCore Python 3.12 CodeZip from a trusted ARM64 SDK image."""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path


MAX_ZIP_BYTES = 250_000_000
MAX_EXPANDED_BYTES = 750_000_000
MAX_APP_SOURCE_BYTES = 1024 * 1024
MAX_RUNTIME_NODE_BYTES = 256 * 1024 * 1024
MAX_SDK_SOURCE_BYTES = 32 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


def _docker(*args: str, capture: bool = False) -> str:
    result = subprocess.run(["docker", *args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else ""


def _check_arm64(path: Path, *, required: bool = False) -> None:
    with path.open("rb") as source:
        header = source.read(20)
    if header[:4] == b"\x7fELF":
        if len(header) < 20 or header[4:6] != b"\x02\x01" or int.from_bytes(header[18:20], "little") != 183:
            raise ValueError(f"Non-ARM64 executable or library: {path.name}")
    elif required or path.name.endswith((".so", ".node")) or ".so." in path.name:
        raise ValueError(f"Invalid native executable or library: {path.name}")


def _prepare_binary_bundle(stage: Path) -> None:
    bundle = stage / "kiro-bundle"
    manifest = {}
    for name in ("kiro-cli", "kiro-cli-chat", "kiro-cli-term"):
        path = bundle / name
        _check_arm64(path, required=True)
        digest = hashlib.sha256()
        size = 0
        if name == "kiro-cli-chat":
            with path.open("rb") as source, lzma.open(str(path) + ".xz", "wb", preset=6) as destination:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    destination.write(chunk)
            path.unlink()
        else:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
        manifest[name] = {"size": size, "sha256": digest.hexdigest()}
    (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def _check_stage_packages(stage: Path) -> None:
    adapter = stage / "kiro_agent_sdk" / "agentcore.py"
    if not adapter.is_file():
        raise ValueError("Image does not contain the Kiro AgentCore adapter")
    if not re.search(rb"(?m)^_CODEZIP_COMPRESSED_NODE_VERSION = 1$", adapter.read_bytes()):
        raise ValueError("Image SDK does not support compressed runtime nodes; rebuild the image")
    if not (stage / "yaml" / "__init__.py").is_file():
        raise ValueError("Image does not contain the required PyYAML dependency")


def _source_files(package: Path) -> dict[str, str]:
    """Hash package source without executing it or including interpreter caches."""
    if package.is_symlink() or not package.is_dir():
        raise ValueError("SDK source must be a regular package directory")
    files, total = {}, 0
    for parent, directories, names in os.walk(package, followlinks=False):
        directories[:] = sorted(name for name in directories if name != "__pycache__")
        if any((Path(parent) / name).is_symlink() for name in directories):
            raise ValueError("SDK source cannot contain directory symlinks")
        for name in sorted(names):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(parent) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("SDK source must contain regular files")
            info = path.stat()
            total += info.st_size
            if total > MAX_SDK_SOURCE_BYTES or len(files) >= 1024:
                raise ValueError("SDK source exceeds size or file-count limits")
            with path.open("rb") as stream:
                content = stream.read(info.st_size + 1)
            if len(content) != info.st_size:
                raise ValueError("SDK source changed during packaging")
            files[path.relative_to(package).as_posix()] = hashlib.sha256(content).hexdigest()
    if "__init__.py" not in files or "py.typed" not in files:
        raise ValueError("SDK source package is incomplete")
    return files


def _check_sdk_sources(stage: Path) -> None:
    """Reject stale images and record the exact source packaged in this CodeZip."""
    output = stage / "sdk-source-manifest.json"
    if output.exists() or output.is_symlink():
        raise ValueError("Image packages contain the reserved SDK source manifest")
    sources = {"kiro_agent_sdk": ROOT / "src" / "kiro_agent_sdk"}
    packages = {}
    for name, source in sources.items():
        expected = _source_files(source)
        actual = _source_files(stage / name)
        if actual != expected:
            raise ValueError(f"Image {name} source differs from this checkout; rebuild the image")
        encoded = json.dumps(actual, sort_keys=True, separators=(",", ":")).encode("utf-8")
        packages[name] = {"file_count": len(actual), "sha256": hashlib.sha256(encoded).hexdigest(),
                          "files": actual}
    output.write_text(json.dumps({"protocol": 1, "packages": packages}, sort_keys=True) + "\n",
                      encoding="utf-8")


def _compress_runtime_node(cache: Path) -> None:
    """Compress the verified prewarmed Node runtime for the CodeZip size limit."""
    node, checksum, output = cache / "node", cache / "node.sha256", cache / "node.xz"
    if node.is_symlink() or checksum.is_symlink() or output.exists() or output.is_symlink():
        raise ValueError("Invalid runtime node or checksum")
    expected = None
    if checksum.exists():
        with checksum.open("rb") as source:
            checksum_bytes = source.read(129)
        if len(checksum_bytes) > 128:
            raise ValueError("Invalid runtime node checksum")
        expected = checksum_bytes.decode("ascii").strip()
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise ValueError("Invalid runtime node checksum")
    digest, size = hashlib.sha256(), 0
    try:
        with node.open("rb") as source, lzma.open(output, "wb", preset=6) as destination:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_RUNTIME_NODE_BYTES:
                    raise ValueError("Runtime node exceeds size limit")
                digest.update(chunk)
                destination.write(chunk)
        if not size or (expected is not None and digest.hexdigest() != expected):
            raise ValueError("Runtime node checksum mismatch")
        if expected is None:
            checksum.write_text(digest.hexdigest(), encoding="ascii")
        node.unlink()
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _files(stage: Path):
    """Enumerate regular files and safe relative symlinks without following directories."""
    stage = stage.resolve()
    for parent, directories, files in os.walk(stage, followlinks=False):
        directories[:] = sorted(name for name in directories if name != "__pycache__")
        for name in directories:
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError(f"Directory symlinks are not supported: {path.relative_to(stage)}")
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = Path(parent) / name
            relative = path.relative_to(stage).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                resolved = path.resolve(strict=True)
                if os.path.isabs(target) or not resolved.is_relative_to(stage) or not resolved.is_file():
                    raise ValueError(f"Unsafe symlink: {relative}")
                _check_arm64(resolved)
                yield path, relative, target
            elif path.is_file():
                _check_arm64(path)
                yield path, relative, None
            else:
                raise ValueError(f"Unsupported archive entry: {relative}")


def _write_archive(stage: Path, output: Path) -> None:
    entries = list(_files(stage))
    expanded = sum(len(target.encode("utf-8")) if target is not None else path.stat().st_size
                   for path, _, target in entries)
    if expanded > MAX_EXPANDED_BYTES:
        raise ValueError("Bundle exceeds the 750 MB expanded AgentCore limit")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, relative, target in entries:
            info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            if target is not None:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, target.encode("utf-8"))
                continue
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = (zipfile.ZIP_STORED if relative in ("kiro-bundle/kiro-cli-chat.xz",
                                                                     "kiro-prewarm/node.xz")
                                  else zipfile.ZIP_DEFLATED)
            with path.open("rb") as source, archive.open(info, "w") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
    if output.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError("Bundle exceeds the 250 MB compressed AgentCore limit")
    print(f"CodeZip: {output.stat().st_size:,} compressed bytes, {expanded:,} expanded bytes")


def _stage_custom_app(stage: Path, source: Path) -> None:
    """Add one explicitly supplied, trusted Python app factory to the bundle."""
    if source.suffix != ".py" or source.is_symlink():
        raise ValueError("App source must be a regular Python file, not a symlink")
    target = stage / "kiro_agentcore_app.py"
    if target.exists() or target.is_symlink():
        raise ValueError("Image packages contain the reserved custom app path")
    try:
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise ValueError("App source must be a readable regular Python file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_APP_SOURCE_BYTES:
            raise ValueError("App source must be a regular Python file of at most 1 MiB")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            content = stream.read(MAX_APP_SOURCE_BYTES + 1)
        if not content or len(content) > MAX_APP_SOURCE_BYTES:
            raise ValueError("App source must be nonempty and at most 1 MiB")
        with target.open("xb") as output:
            output.write(content)
    finally:
        if fd >= 0:
            os.close(fd)


def build(image: str, output: Path, *, force: bool = False, app_source: Path | None = None) -> None:
    if os.name != "posix":
        raise ValueError("CodeZip building requires a POSIX host for symlink preservation")
    if _docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", image, capture=True) != "linux/arm64":
        raise ValueError("The image must be linux/arm64")
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not force:
        raise FileExistsError(f"Output already exists: {output}")
    with tempfile.TemporaryDirectory(prefix="kiro-codezip-build-") as directory:
        stage = Path(directory) / "stage"
        stage.mkdir()
        container = _docker("create", "--platform", "linux/arm64", image, capture=True)
        try:
            _docker("cp", f"{container}:/usr/local/lib/python3.12/site-packages/.", str(stage))
            if any((stage / name).exists() or (stage / name).is_symlink()
                   for name in ("kiro-bundle", "kiro-prewarm", "kiro_agentcore_app.py", "agentcore_entry.py")):
                raise ValueError("Image packages contain reserved bundle paths")
            _docker("cp", f"{container}:/opt/kiro-prewarm", str(stage / "kiro-prewarm"))
            bundle = stage / "kiro-bundle"
            bundle.mkdir()
            for name in ("kiro-cli", "kiro-cli-chat", "kiro-cli-term"):
                _docker("cp", f"{container}:/usr/local/bin/{name}", str(bundle / name))
        finally:
            _docker("rm", "-f", container, capture=True)
        _check_stage_packages(stage)
        _check_sdk_sources(stage)
        cache = stage / "kiro-prewarm"
        if not (cache / "node").is_file() or not (cache / "kas").is_dir():
            raise ValueError("Image does not contain the prepared Kiro V3 runtime")
        if any(path.name not in ("node", "node.sha256", "kas") for path in cache.iterdir()):
            raise ValueError("Runtime cache contains unexpected files")
        _check_arm64(cache / "node", required=True)
        _compress_runtime_node(cache)
        _prepare_binary_bundle(stage)
        if app_source is not None:
            _stage_custom_app(stage, app_source)
        shutil.copyfile(ROOT / "scripts" / "agentcore_code_entry.py", stage / "agentcore_entry.py")
        fd, temporary = tempfile.mkstemp(prefix=".kiro-codezip-", suffix=".zip", dir=str(output.parent))
        os.close(fd)
        try:
            _write_archive(stage, Path(temporary))
            if force:
                os.replace(temporary, output)
            else:
                os.link(temporary, output)
                os.unlink(temporary)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f"Wrote {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Trusted linux/arm64 image built from Dockerfile.agentcore")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force", action="store_true", help="Replace an existing output file")
    parser.add_argument("--app-source", type=Path,
                        help="Trusted Python file exposing create_app(config) for a custom AgentCore app")
    args = parser.parse_args()
    build(args.image, args.output, force=args.force, app_source=args.app_source)


if __name__ == "__main__":
    main()
