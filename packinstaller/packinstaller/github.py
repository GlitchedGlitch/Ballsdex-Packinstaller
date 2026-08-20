"""
GitHub utilities for PackInstaller.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
import tomllib
from packaging.version import InvalidVersion, Version

GH_API    = "https://api.github.com"
UV_BIN    = "uv"
VENV      = "/opt/venv"
CACHE_TTL = 300
GH_SEM    = asyncio.Semaphore(3)

# ── Cache ─────────────────────────────────────────────────────────────────────

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
    path: str        # [[ballsdex.packages]] path
    extension: str   # discord.py load_extension() target
    repo_owner: str
    repo_name: str
    raw_url: str     # full git+ URL including @tag and fragment
    subdir: str      # empty string if not a subdirectory package
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

def parse_github_url(url: str) -> tuple[str, str, str, str, str] | None:
    """
    Parse any GitHub URL into
    """
    url = url.strip()

    if url.startswith("git+"):
        clean = url.removeprefix("git+")
        fragment = ""
        subdir = ""

        # Split off everything after # first
        if "#" in clean:
            clean, raw_fragment = clean.split("#", 1)
            if raw_fragment.startswith("subdirectory="):
                subdir = raw_fragment.removeprefix("subdirectory=")
            else:
                fragment = raw_fragment  # e.g. "master"

        # Split off @ref
        ref = ""
        if ".git@" in clean:
            clean, ref = clean.rsplit("@", 1)

        clean = re.sub(r"\.git$", "", clean)
        clean = re.sub(r"^https?://", "", clean)
        parts = clean.split("/")
        if len(parts) < 3 or parts[0] != "github.com":
            return None
        return parts[1], parts[2], ref, fragment, subdir

    # Browser URL
    clean = re.sub(r"^https?://", "", url).rstrip("/")
    clean = re.sub(r"^github\.com/", "", clean)
    parts = clean.split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    ref, fragment, subdir = "", "", ""
    if len(parts) > 3 and parts[2] == "tree":
        ref = parts[3]
        if len(parts) > 4:
            subdir = "/".join(parts[4:])
    return owner, repo, ref, fragment, subdir


def build_git_url(
    owner: str,
    repo: str,
    tag: str | None,
    subdir: str,
    fragment: str = "",
) -> str:
    """
    Build a git+ URL, preserving the #branch fragment or #subdirectory= as needed.
    """
    base = f"git+https://github.com/{owner}/{repo}.git"
    if tag:
        base = f"{base}@{tag}"
    if subdir:
        base = f"{base}#subdirectory={subdir}"
    elif fragment:
        base = f"{base}#{fragment}"
    return base


def extract_tag_from_location(location: str) -> str | None:
    """Extract @tag from a git+ URL, ignoring any #fragment."""
    loc = location.split("#")[0]
    m = re.search(r"\.git@([^@\s]+)$", loc)
    return m.group(1) if m else None


def parse_version(tag: str) -> Version:
    clean = tag.lstrip("v")
    try:
        return Version(clean)
    except InvalidVersion:
        return Version("0")


# ── GitHub API ────────────────────────────────────────────────────────────────

class RateLimitError(Exception):
    """Raised when GitHub returns 403."""

async def _gh_get(session: aiohttp.ClientSession, path: str) -> dict | list | None:
    cached = _cache_get(f"gh:{path}")
    if cached is not None:
        return cached

    async with GH_SEM:
        async with session.get(f"{GH_API}{path}") as resp:
            if resp.status == 404:
                return None
            if resp.status == 403:
                raise RateLimitError(
                    "GitHub API rate limit reached. Please try again in a few minutes."
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
    """Fetch and parse pyproject.toml, optionally from a subdirectory."""
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
    """
    Return the highest-version release tag.
    """
    try:
        releases = await _gh_get(session, f"/repos/{owner}/{repo}/releases")
        candidates: list[tuple[Version, str]] = []

        if releases:
            for release in releases:
                if not release.get("draft") and not release.get("prerelease"):
                    candidates.append((parse_version(release["tag_name"]), release["tag_name"]))

        if candidates:
            return max(candidates, key=lambda x: x[0])[1]

        # Fallback to tags
        tags = await _gh_get(session, f"/repos/{owner}/{repo}/tags")
        if tags:
            tag_candidates: list[tuple[Version, str]] = [
                (parse_version(t["name"]), t["name"]) for t in tags
            ]
            if tag_candidates:
                return max(tag_candidates, key=lambda x: x[0])[1]

    except RateLimitError:
        raise
    except Exception:
        pass

    return None


# ── Extension discovery ───────────────────────────────────────────────────────

def discover_extension(path: str) -> str:
    """
    Determine the discord.py extension module path for a package.
    """
    # apps.py 
    try:
        spec = importlib.util.find_spec(path)
        if spec and spec.submodule_search_locations:
            pkg_dir = list(spec.submodule_search_locations)[0]
            apps_spec = importlib.util.spec_from_file_location(
                f"{path}.apps", f"{pkg_dir}/apps.py"
            )
            if apps_spec and apps_spec.loader:
                apps_mod = importlib.util.module_from_spec(apps_spec)
                apps_spec.loader.exec_module(apps_mod)  # type: ignore
                for attr in dir(apps_mod):
                    obj = getattr(apps_mod, attr)
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
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            if hasattr(mod, "setup"):
                return path
    except Exception:
        pass

    # walk immediate submodules for one that has setup()
    try:
        spec = importlib.util.find_spec(path)
        if spec and spec.submodule_search_locations:
            import pkgutil
            pkg_dir = list(spec.submodule_search_locations)[0]
            for finder, modname, ispkg in pkgutil.iter_modules([pkg_dir]):
                candidate = f"{path}.{modname}"
                try:
                    cspec = importlib.util.find_spec(candidate)
                    if cspec and cspec.loader:
                        cmod = importlib.util.module_from_spec(cspec)
                        cspec.loader.exec_module(cmod)  # type: ignore
                        if hasattr(cmod, "setup"):
                            return candidate
                except Exception:
                    pass
    except Exception:
        pass

    # common path.path pattern
    nested = f"{path}.{path}"
    try:
        if importlib.util.find_spec(nested):
            return nested
    except Exception:
        pass

    # Fallback
    return path


# ── Package validation ────────────────────────────────────────────────────────

def _determine_path(pyproject: dict, repo_name: str) -> str:
    """
    Determine [[ballsdex.packages]] path from pyproject.toml.
    """
    find = (
        pyproject.get("tool", {})
        .get("setuptools", {})
        .get("packages", {})
        .get("find", {})
    )
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
    Validate a GitHub URL and return package metadata.
    """
    parsed = parse_github_url(url)
    if not parsed:
        return None, "That doesn't look like a valid GitHub URL."

    owner, repo, ref, fragment, subdir = parsed

    try:
        repo_data = await _gh_get(session, f"/repos/{owner}/{repo}")
    except RateLimitError as e:
        return None, str(e)

    if not repo_data:
        return None, f"Repository `{owner}/{repo}` not found or is private."

    # Fix #6: use parsed ref (tag/branch) to fetch the correct pyproject.toml.
    # If no ref in URL, use the repo's default branch.
    # For fragment-only URLs like @0.0.5#master, ref=0.0.5 is the tag and
    # pyproject.toml is at the default branch root (or ref if it's a branch).
    fetch_ref = ref or fragment or repo_data.get("default_branch", "HEAD")

    pyproject = await fetch_pyproject(session, owner, repo, fetch_ref, subdir)
    if not pyproject:
        location = f"`{subdir}/`" if subdir else "the repository root"
        return None, f"No `pyproject.toml` found in {location} of `{owner}/{repo}`."

    # Fix #9: distinct error messages for each failure mode
    project = pyproject.get("project")
    if not project:
        return None, "`pyproject.toml` is missing the `[project]` section."

    name = project.get("name")
    if not name:
        return None, "`pyproject.toml` is missing `project.name`."

    version = project.get("version", "unknown")
    path = _determine_path(pyproject, repo)

    try:
        latest_tag = await get_latest_tag(session, owner, repo)
    except RateLimitError:
        latest_tag = ref or None

    install_tag = latest_tag or ref or None
    # Fix #3: preserve fragment when building install URL
    raw_url = build_git_url(owner, repo, install_tag, subdir, fragment)
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
    Check the latest version for a single installed package.
    """
    path = pkg.get("path", "")
    location = pkg.get("location", "")
    installed_tag = extract_tag_from_location(location) or "unknown"
    installed_ver = parse_version(installed_tag)
    extension = discover_extension(path)

    parsed = parse_github_url(location)
    if not parsed:
        return VersionInfo(path, extension, installed_tag, None, installed_ver, None)

    owner, repo, _, _, _ = parsed
    try:
        latest_tag = await get_latest_tag(session, owner, repo)
        latest_ver = parse_version(latest_tag) if latest_tag else None
    except (RateLimitError, Exception):
        latest_tag = None
        latest_ver = None

    return VersionInfo(path, extension, installed_tag, latest_tag, installed_ver, latest_ver)


async def check_versions(
    session: aiohttp.ClientSession,
    packages: list[dict],
) -> list[VersionInfo]:
    """Check versions for all GitHub-sourced packages concurrently."""
    tasks = [
        check_one_version(session, pkg)
        for pkg in packages
        if pkg.get("location", "").startswith("git+https://github.com/")
    ]
    return list(await asyncio.gather(*tasks))


# ── Update URL construction ───────────────────────────────────────────────────

def build_update_url(current_location: str, new_tag: str) -> str:
    """
    Build an updated git+ URL by replacing the @tag while preserving
    any #fragment (branch) or #subdirectory= suffix.
    """
    parsed = parse_github_url(current_location)
    if not parsed:
        return current_location
    owner, repo, _old_tag, fragment, subdir = parsed
    return build_git_url(owner, repo, new_tag, subdir, fragment)


# ── uv operations ─────────────────────────────────────────────────────────────

async def uv_install(git_url: str) -> tuple[bool, str]:
    """Run uv pip install for a git URL."""
    proc = await asyncio.create_subprocess_exec(
        UV_BIN, "pip", "install", "--python", VENV, git_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode == 0, stdout.decode(errors="replace")


async def uv_reinstall(git_url: str, package_name: str) -> tuple[bool, str]:
    """
    Run uv pip install --reinstall-package <name> to bypass the Git commit cache.
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
