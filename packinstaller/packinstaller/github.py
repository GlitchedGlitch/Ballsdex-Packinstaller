"""
GitHub utilities for PackInstaller.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
import tomllib
from packaging.version import Version, InvalidVersion

GH_API = "https://api.github.com"
UV_BIN = "uv"
VENV   = "/opt/venv"


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class PackageMeta:
    name: str
    version: str
    path: str
    repo_owner: str
    repo_name: str
    raw_url: str       # full git+ URL including @tag and #subdirectory if needed
    subdir: str        # subdirectory within the repo, or ""
    description: str = ""


@dataclass
class VersionInfo:
    path: str
    installed_tag: str
    latest_tag: str
    installed_version: Version
    latest_version: Version

    @property
    def has_update(self) -> bool:
        return self.latest_version > self.installed_version


# ── URL helpers ───────────────────────────────────────────────────────────────

def parse_github_url(url: str) -> tuple[str, str, str, str] | None:
    """
    Parse any Github URL (browser or git+) into (owner, repo, ref, subdir).
    """
    url = url.strip()

    # git+https form with possible @ref and #subdirectory
    if url.startswith("git+"):
        clean = url.removeprefix("git+")
        subdir = ""
        if "#subdirectory=" in clean:
            clean, subdir = clean.split("#subdirectory=", 1)
        ref = ""
        if "@" in clean.split("/")[-1]:
            clean, ref = clean.rsplit("@", 1)
        clean = re.sub(r"\.git$", "", clean)
        clean = re.sub(r"^https?://", "", clean)
        parts = clean.split("/")
        if len(parts) < 3 or parts[0] != "github.com":
            return None
        return parts[1], parts[2], ref, subdir

    # browser url
    clean = re.sub(r"^https?://", "", url).rstrip("/")
    clean = re.sub(r"^github\.com/", "", clean)
    parts = clean.split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    repo = re.sub(r"\.git$", "", repo)
    ref = ""
    subdir = ""
    # subdirectory
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
    """Extract @tag from a git+ URL (before any #subdirectory fragment)."""
    # strip subdirectory fragment first
    loc = location.split("#")[0]
    m = re.search(r"\.git@([^@\s]+)$", loc)
    return m.group(1) if m else None


def parse_version(tag: str) -> Version:
    clean = tag.lstrip("v")
    try:
        return Version(clean)
    except InvalidVersion:
        return Version("0")


# ── GitHub API calls ──────────────────────────────────────────────────────────

async def _gh_get(session: aiohttp.ClientSession, path: str) -> dict | list | None:
    async with session.get(f"{GH_API}{path}") as resp:
        if resp.status == 404:
            return None
        resp.raise_for_status()
        return await resp.json()


async def fetch_pyproject(
    session: aiohttp.ClientSession,
    owner: str,
    repo: str,
    ref: str = "HEAD",
    subdir: str = "",
) -> dict | None:
    """Fetch and parse pyproject.toml from a GitHub repo, optionally in a subdirectory."""
    path = f"{subdir}/pyproject.toml" if subdir else "pyproject.toml"
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    async with session.get(url) as resp:
        if resp.status != 200:
            return None
        raw = await resp.read()
    try:
        return tomllib.loads(raw.decode())
    except tomllib.TOMLDecodeError:
        return None


async def get_latest_tag(
    session: aiohttp.ClientSession, owner: str, repo: str
) -> str | None:
    releases = await _gh_get(session, f"/repos/{owner}/{repo}/releases")
    if releases:
        for release in releases:
            if not release.get("draft") and not release.get("prerelease"):
                return release["tag_name"]
    tags = await _gh_get(session, f"/repos/{owner}/{repo}/tags")
    if tags:
        versions: list[tuple[Version, str]] = []
        for tag in tags:
            name = tag["name"]
            versions.append((parse_version(name), name))
        if versions:
            versions.sort(key=lambda x: x[0], reverse=True)
            return versions[0][1]
    return None


# ── Package validation ────────────────────────────────────────────────────────

def _is_ballsdex_package(pyproject: dict) -> bool:
    deps = pyproject.get("project", {}).get("dependencies", [])
    if any("ballsdex" in d.lower() for d in deps):
        return True
    urls = pyproject.get("project", {}).get("urls", {})
    if any("ballsdex" in v.lower() for v in urls.values()):
        return True
    find = pyproject.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
    if find.get("include"):
        return True
    return False


def _determine_path(pyproject: dict) -> str | None:
    find = pyproject.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
    includes = find.get("include", [])
    if includes:
        return includes[0].rstrip("*").rstrip(".")
    return None


async def validate_and_fetch_meta(
    session: aiohttp.ClientSession, url: str
) -> tuple[PackageMeta | None, str]:
    """
    Full validation pipeline supporting both root and subdirectory packages.
    """
    parsed = parse_github_url(url)
    if not parsed:
        return None, "That doesn't look like a valid GitHub URL."

    owner, repo, ref, subdir = parsed

    repo_data = await _gh_get(session, f"/repos/{owner}/{repo}")
    if not repo_data:
        return None, f"Repository `{owner}/{repo}` not found or is private."

    # use provided ref or default branch
    default_ref = ref or repo_data.get("default_branch", "HEAD")

    # fetch pyproject.toml
    pyproject = await fetch_pyproject(session, owner, repo, default_ref, subdir)
    if not pyproject:
        if subdir:
            return None, (
                f"No `pyproject.toml` found in `{subdir}/` of `{owner}/{repo}`. "
                "Make sure the subdirectory path is correct."
            )
        return None, f"No `pyproject.toml` found in the root of `{owner}/{repo}`."

    project = pyproject.get("project")
    if not project:
        return None, "`pyproject.toml` is missing the `[project]` section."

    name = project.get("name")
    version = project.get("version")
    if not name:
        return None, "`pyproject.toml` is missing `project.name`."
    if not version:
        return None, "`pyproject.toml` is missing `project.version`."

    if not _is_ballsdex_package(pyproject):
        return None, (
            "This repository does not appear to be a BallsDex package. "
            "It must list packages via `[tool.setuptools.packages.find]`."
        )

    path = _determine_path(pyproject)
    if not path:
        return None, (
            "Could not determine the package `path` from `pyproject.toml`. "
            "Ensure `[tool.setuptools.packages.find]` has an `include` list."
        )

    latest_tag = await get_latest_tag(session, owner, repo)
    # for branch-pinned packages (like @master), keep the ref
    install_tag = latest_tag or ref or None
    raw_url = build_git_url(owner, repo, install_tag, subdir)

    return PackageMeta(
        name=name,
        version=install_tag or version,
        path=path,
        repo_owner=owner,
        repo_name=repo,
        raw_url=raw_url,
        subdir=subdir,
        description=project.get("description", ""),
    ), ""


# ── Version comparison ────────────────────────────────────────────────────────

async def check_versions(
    session: aiohttp.ClientSession,
    packages: list[dict],
) -> list[VersionInfo]:
    results: list[VersionInfo] = []
    for pkg in packages:
        location: str = pkg.get("location", "")
        path: str = pkg.get("path", "")
        if not location.startswith("git+https://github.com/"):
            continue
        parsed = parse_github_url(location)
        if not parsed:
            continue
        owner, repo, _, _ = parsed
        installed_tag = extract_tag_from_location(location) or "unknown"
        installed_ver = parse_version(installed_tag)
        async with aiohttp.ClientSession() as s:
            latest_tag = await get_latest_tag(s, owner, repo)
        if not latest_tag:
            continue
        latest_ver = parse_version(latest_tag)
        results.append(VersionInfo(
            path=path,
            installed_tag=installed_tag,
            latest_tag=latest_tag,
            installed_version=installed_ver,
            latest_version=latest_ver,
        ))
    return results


# ── uv install ────────────────────────────────────────────────────────────────

async def uv_install(git_url: str) -> tuple[bool, str]:
    """Run uv pip install for the given git URL. Returns (success, output)."""
    proc = await asyncio.create_subprocess_exec(
        UV_BIN, "pip", "install",
        "--python", VENV,
        git_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode == 0, stdout.decode(errors="replace")

 
