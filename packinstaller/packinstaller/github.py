"""
GitHub utilities for PackInstaller.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import tomllib
from packaging.version import Version, InvalidVersion

GH_API     = "https://api.github.com"
UV_BIN     = "uv"
VENV       = "/opt/venv"
CACHE_TTL  = 300        # 5 minutes
GH_SEM     = asyncio.Semaphore(3)   # max 3 concurrent GitHub requests

# ── In-memory cache ───────────────────────────────────────────────────────────

_cache: dict[str, dict[str, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry and time.monotonic() < entry["expires"]:
        return entry["value"]
    return None


def _cache_set(key: str, value: Any, ttl: int = CACHE_TTL) -> None:
    _cache[key] = {"value": value, "expires": time.monotonic() + ttl}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class PackageMeta:
    name: str
    version: str
    path: str               # path field
    extension: str          # extension path
    repo_owner: str
    repo_name: str
    raw_url: str            # git
    subdir: str             # subdirectory
    description: str = ""


@dataclass
class VersionInfo:
    path: str
    extension: str
    installed_tag: str
    latest_tag: str | None
    installed_version: Version
    latest_version: Version | None

    @property
    def has_update(self) -> bool:
        if self.latest_version is None:
            return False
        return self.latest_version > self.installed_version

    @property
    def github_available(self) -> bool:
        return self.latest_tag is not None


# ── URL helpers ───────────────────────────────────────────────────────────────

def parse_github_url(url: str) -> tuple[str, str, str, str] | None:
    """
    Parse any GitHub URL.
    """
    url = url.strip()

    if url.startswith("git+"):
        clean = url.removeprefix("git+")
        subdir = ""
        if "#subdirectory=" in clean:
            clean, subdir = clean.split("#subdirectory=", 1)
        ref = ""
        if ".git@" in clean:
            clean, ref = clean.rsplit("@", 1)
        clean = re.sub(r"\.git$", "", clean)
        clean = re.sub(r"^https?://", "", clean)
        parts = clean.split("/")
        if len(parts) < 3 or parts[0] != "github.com":
            return None
        return parts[1], parts[2], ref, subdir

    clean = re.sub(r"^https?://", "", url).rstrip("/")
    clean = re.sub(r"^github\.com/", "", clean)
    parts = clean.split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    ref, subdir = "", ""
    if len(parts) > 3 and parts[2] == "tree":
        ref = parts[3]
        if len(parts) > 4:
            subdir = "/".join(parts[4:])
    return owner, repo, ref, subdir


def build_git_url(owner: str, repo: str, tag: str | None, subdir: str) -> str:
    base = f"git+https://github.com/{owner}/{repo}.git"
    if tag:
        base = f"{base}@{tag}"
    if subdir:
        base = f"{base}#subdirectory={subdir}"
    return base


def extract_tag_from_location(location: str) -> str | None:
    """Extract tag from a git+ url, ignoring any subdirectory fragment"""
    loc = location.split("#")[0]
    m = re.search(r"\.git@([^@\s]+)$", loc)
    return m.group(1) if m else None


def parse_version(tag: str) -> Version:
    clean = tag.lstrip("v")
    try:
        return Version(clean)
    except InvalidVersion:
        return Version("0")


# ── GitHub API ──────────────────────────────────

class RateLimitError(Exception):
    """Raised when GitHub returns 403 rate limit"""


async def _gh_get(session: aiohttp.ClientSession, path: str) -> dict | list | None:
    """
    Make a GitHub API GET request.
    """
    cached = _cache_get(f"gh:{path}")
    if cached is not None:
        return cached

    async with GH_SEM:
        async with session.get(f"{GH_API}{path}") as resp:
            if resp.status == 404:
                return None
            if resp.status == 403:
                raise RateLimitError(
                    "GitHub API rate limit reached. Package information is temporarily "
                    "unavailable. Please try again in a few minutes."
                )
            resp.raise_for_status()
            data = await resp.json()

    _cache_set(f"gh:{path}", data)
    return data


async def fetch_pyproject(
    session: aiohttp.ClientSession,
    owner: str,
    repo: str,
    ref: str = "HEAD",
    subdir: str = "",
) -> dict | None:
    """Fetch and parse pyproject.toml from a GitHub repo, optionally in a subdirectory."""
    path = f"{subdir}/pyproject.toml" if subdir else "pyproject.toml"
    cache_key = f"pyproject:{owner}/{repo}/{ref}/{path}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    async with session.get(url) as resp:
        if resp.status != 200:
            return None
        raw = await resp.read()

    try:
        data = tomllib.loads(raw.decode())
        _cache_set(cache_key, data)
        return data
    except tomllib.TOMLDecodeError:
        return None


async def get_latest_tag(
    session: aiohttp.ClientSession, owner: str, repo: str
) -> str | None:
    """Return the latest release tag, or None if unavailable/rate-limited."""
    try:
        releases = await _gh_get(session, f"/repos/{owner}/{repo}/releases")
        if releases:
            for release in releases:
                if not release.get("draft") and not release.get("prerelease"):
                    return release["tag_name"]

        tags = await _gh_get(session, f"/repos/{owner}/{repo}/tags")
        if tags:
            versions: list[tuple[Version, str]] = []
            for tag in tags:
                versions.append((parse_version(tag["name"]), tag["name"]))
            if versions:
                versions.sort(key=lambda x: x[0], reverse=True)
                return versions[0][1]
    except RateLimitError:
        raise
    except Exception:
        pass
    return None


# ── Extension discovery ───────────────────────────────────────────────────────

def discover_extension(path: str) -> str:
    """
    Determine the actual discord.py extension module path for a given package path.
    """
    # read apps.py for dpy_package
    try:
        spec = importlib.util.find_spec(path)
        if spec and spec.submodule_search_locations:
            pkg_dir = list(spec.submodule_search_locations)[0]
            apps_path = f"{pkg_dir}/apps.py"
            apps_spec = importlib.util.spec_from_file_location(f"{path}.apps", apps_path)
            if apps_spec and apps_spec.loader:
                apps_mod = importlib.util.module_from_spec(apps_spec)
                apps_spec.loader.exec_module(apps_mod)  # type: ignore
                for name in dir(apps_mod):
                    obj = getattr(apps_mod, name)
                    try:
                        if (
                            isinstance(obj, type)
                            and hasattr(obj, "dpy_package")
                            and obj.dpy_package
                        ):
                            return obj.dpy_package
                    except Exception:
                        pass
    except Exception:
        pass

    # direct setup() on path
    try:
        spec = importlib.util.find_spec(path)
        if spec:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            if hasattr(mod, "setup"):
                return path
    except Exception:
        pass

    # nested path.path
    nested = f"{path}.{path}"
    try:
        spec = importlib.util.find_spec(nested)
        if spec:
            return nested
    except Exception:
        pass

    # fallback
    return path


# ── Package validation ────────────────────────────────────────────────────────

def _is_ballsdex_package(pyproject: dict) -> bool:
    """
    Check if it's a valid Ballsdex package.
    """
    project = pyproject.get("project", {})
    if not project.get("name"):
        return False

    # Accept if any Ballsdex hints exist
    deps = project.get("dependencies", [])
    if any("ballsdex" in d.lower() for d in deps):
        return True

    urls = project.get("urls", {})
    if any("ballsdex" in v.lower() for v in urls.values()):
        return True

    # Accept if it has a build-system
    if pyproject.get("build-system"):
        return True

    # Accept if it has setuptools find config
    find = pyproject.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
    if find:
        return True

    return False


def _determine_path(pyproject: dict, repo_name: str) -> str:
    """
    Determine the [[ballsdex.packages]] path.
    """
    find = pyproject.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
    includes = find.get("include", [])
    if includes:
        return includes[0].rstrip("*").rstrip(".")

    name = pyproject.get("project", {}).get("name", "")
    if name:
        normalized = name.lower().replace("-", "_")
        for prefix in ("ballsdex_", "bd_"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):]
        return normalized

    return repo_name.lower().replace("-", "_")


async def validate_and_fetch_meta(
    session: aiohttp.ClientSession, url: str
) -> tuple[PackageMeta | None, str]:
    """
    Package validation
    """
    parsed = parse_github_url(url)
    if not parsed:
        return None, "That doesn't look like a valid GitHub URL..."

    owner, repo, ref, subdir = parsed

    try:
        repo_data = await _gh_get(session, f"/repos/{owner}/{repo}")
    except RateLimitError as e:
        return None, f"{e}"

    if not repo_data:
        return None, f"Repository `{owner}/{repo}` not found or is private."

    default_ref = ref or repo_data.get("default_branch", "HEAD")

    pyproject = await fetch_pyproject(session, owner, repo, default_ref, subdir)
    if not pyproject:
        location = f"`{subdir}/`" if subdir else "the repository root"
        return None, f"No `pyproject.toml` found in {location} of `{owner}/{repo}`."

    project = pyproject.get("project")
    if not project:
        return None, "`pyproject.toml` is missing the `[project]` section."

    name = project.get("name")
    version = project.get("version", "unknown")
    if not name:
        return None, "`pyproject.toml` is missing `project.name`."

    if not _is_ballsdex_package(pyproject):
        return None, (
            "This repository does not appear to be a BallsDex package. "
            "It must have a valid `[project]` section and a build-system definition."
        )

    path = _determine_path(pyproject, repo)

    try:
        latest_tag = await get_latest_tag(session, owner, repo)
    except RateLimitError:
        latest_tag = ref or None

    install_tag = latest_tag or ref or None
    raw_url = build_git_url(owner, repo, install_tag, subdir)
    extension = discover_extension(path)

    return PackageMeta(
        name=name,
        version=install_tag or version,
        path=path,
        extension=extension,
        repo_owner=owner,
        repo_name=repo,
        raw_url=raw_url,
        subdir=subdir,
        description=project.get("description", ""),
    ), ""


# ── Version checking ──────────────────────────────────────────────────────────

async def check_one_version(
    session: aiohttp.ClientSession,
    pkg: dict,
) -> VersionInfo:
    """
    Check version for a single package.
    """
    path = pkg.get("path", "")
    location = pkg.get("location", "")
    installed_tag = extract_tag_from_location(location) or "unknown"
    installed_ver = parse_version(installed_tag)
    extension = discover_extension(path)

    parsed = parse_github_url(location)
    if not parsed:
        return VersionInfo(path, extension, installed_tag, None, installed_ver, None)

    owner, repo, _, _ = parsed
    try:
        latest_tag = await get_latest_tag(session, owner, repo)
        latest_ver = parse_version(latest_tag) if latest_tag else None
    except RateLimitError:
        latest_tag = None
        latest_ver = None
    except Exception:
        latest_tag = None
        latest_ver = None

    return VersionInfo(path, extension, installed_tag, latest_tag, installed_ver, latest_ver)


async def check_versions(
    session: aiohttp.ClientSession,
    packages: list[dict],
) -> list[VersionInfo]:
    """
    Check versions for all packages concurrently.
    """
    tasks = [
        check_one_version(session, pkg)
        for pkg in packages
        if pkg.get("location", "").startswith("git+https://github.com/")
    ]
    return list(await asyncio.gather(*tasks))


# ── uv operations ─────────────────────────────────────────────────────────────

async def uv_install(git_url: str) -> tuple[bool, str]:
    """Run `uv pip install --python /opt/venv <git_url>`."""
    proc = await asyncio.create_subprocess_exec(
        UV_BIN, "pip", "install", "--python", VENV, git_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode == 0, stdout.decode(errors="replace")


async def uv_reinstall(git_url: str, package_name: str) -> tuple[bool, str]:
    """
    Run `uv pip install --reinstall-package <name> <git_url>`.
    """
    proc = await asyncio.create_subprocess_exec(
        UV_BIN, "pip", "install",
        "--python", VENV,
        "--reinstall-package", package_name,
        git_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode == 0, stdout.decode(errors="replace")
