"""
PackInstaller cog :3
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord.ext import commands

from .github import (
    VersionInfo,
    check_versions,
    uv_install,
    validate_and_fetch_meta,
)
from .toml_utils import (
    add_package,
    get_package_location,
    package_exists,
    read_packages,
    remove_package,
    update_package_location,
)

from settings.models import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.packinstaller")

BAR_FILLED, BAR_EMPTY, BAR_LEN = "█", "░", 12


def _bar(current: int, total: int) -> str:
    if total == 0:
        return f"`{'░' * BAR_LEN}` 0%"
    filled = round(BAR_LEN * current / total)
    pct = round(100 * current / total)
    return f"`{BAR_FILLED * filled}{BAR_EMPTY * (BAR_LEN - filled)}` {pct}%"


def _progress_embed(
    title: str,
    steps: list[tuple[str, bool | None]],
    color: discord.Color,
) -> discord.Embed:
    icons = {None: "⬜", True: "✅", False: "❌"}
    done = sum(1 for _, s in steps if s is True)
    lines = [f"{icons[s]} {label}" for label, s in steps]
    return discord.Embed(
        title=title,
        description="\n".join(lines) + f"\n\n{_bar(done, len(steps))}",
        color=color,
    )


# ── Interactive update select view ────────────────────────────────────────────

class UpdateSelect(discord.ui.Select):
    def __init__(self, updates: list[VersionInfo]):
        options = [
            discord.SelectOption(
                label=v.path,
                description=f"{v.installed_tag} → {v.latest_tag}",
                value=v.path,
            )
            for v in updates
        ]
        super().__init__(
            placeholder="Select packages to update…",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected = self.values  # type: ignore
        await interaction.response.defer()


class UpdateView(discord.ui.View):
    def __init__(self, updates: list[VersionInfo], owner_id: int):
        super().__init__(timeout=120)
        self.selected: list[str] = []
        self.owner_id = owner_id
        self.confirmed = False
        self.add_item(UpdateSelect(updates))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Update Selected", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()


# ── Cog ───────────────────────────────────────────────────────────────────────

class PackagesCog(commands.Cog, name="PackInstaller"):
    """PackInstaller - manage BallsDex v3 packages from Discord."""

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    @commands.group(
        name="package",
        invoke_without_command=True,
    )
    @commands.is_owner()
    async def package(self, ctx: commands.Context):
        """
        PackInstaller management.
        """
        await ctx.send_help(ctx.command)

    # ── install ───────────────────────────────────────────────────────────────

    @package.command(name="install")
    @commands.is_owner()
    async def install(self, ctx: commands.Context, *, url: str):
        """
        Install a BallsDex package from a GitHub repository.

        Parameters
        ----------
        url: str
            The GitHub repository URL or git+ URL of the package to install.
        """
        steps: list[tuple[str, bool | None]] = [
            ("Validating package", None),
            ("Installing via uv pip", None),
            ("Writing to extra.toml", None),
            ("Loading extension", None),
        ]
        msg = await ctx.send(embed=_progress_embed("Installing package…", steps, discord.Color.blurple()))

        async def upd(i: int, success: bool = True):
            steps[i] = (steps[i][0], success)
            await msg.edit(embed=_progress_embed("Installing package…", steps, discord.Color.blurple()))

        # Validation
        async with aiohttp.ClientSession() as session:
            meta, err = await validate_and_fetch_meta(session, url)

        if meta is None:
            steps[0] = (steps[0][0], False)
            await msg.edit(embed=_progress_embed("Installation failed", steps, discord.Color.red()))
            await ctx.send(f"{err}")
            return

        if package_exists(meta.path):
            steps[0] = (steps[0][0], False)
            await msg.edit(embed=_progress_embed("Installation failed", steps, discord.Color.red()))
            await ctx.send(f"`{meta.path}` is already installed. Use `{settings.prefix}package update` instead.")
            return

        await upd(0)

        # uv install
        ok, output = await uv_install(meta.raw_url)
        if not ok:
            steps[1] = (steps[1][0], False)
            await msg.edit(embed=_progress_embed("Installation failed", steps, discord.Color.red()))
            truncated = output[-1500:] if len(output) > 1500 else output
            await ctx.send(f"`uv pip install` failed:\n```\n{truncated}\n```")
            return

        await upd(1)

        # extra.toml
        try:
            add_package(
                {"location": meta.raw_url, "path": meta.path, "enabled": True},
                comment=meta.name,
            )
        except OSError as e:
            steps[2] = (steps[2][0], False)
            await msg.edit(embed=_progress_embed("Installation failed", steps, discord.Color.red()))
            await ctx.send(f"Could not write to `extra.toml`: `{e.strerror}`")
            return

        await upd(2)

        # load extension
        try:
            await self.bot.load_extension(meta.path)
        except Exception as e:
            steps[3] = (steps[3][0], False)
            await msg.edit(embed=_progress_embed("Partial install", steps, discord.Color.orange()))
            await ctx.send(
                f"Written to `extra.toml` but failed to load live:\n```\n{e}\n```\n"
                "It will load automatically on the next rebuild and restart."
            )
            log.warning(
                f"{ctx.author} installed {meta.name} ({meta.path}) but load failed: {e}",
                extra={"webhook": True},
            )
            return

        await upd(3)

        await msg.edit(
            embed=discord.Embed(
                title="Package Installed",
                description=(
                    f"**{meta.name}** `{meta.version}`\n"
                    f"{meta.description}\n\n"
                    f"• Path: `{meta.path}`\n"
                    f"• Source: `{meta.raw_url}`"
                ),
                color=discord.Color.green(),
            )
        )
        log.info(
            f"{ctx.author} ({ctx.author.id}) installed {meta.name} "
            f"({meta.path}) v{meta.version} from {meta.raw_url}",
            extra={"webhook": True},
        )

    # ── uninstall ─────────────────────────────────────────────────────────────

    @package.command(name="uninstall", aliases=["remove"])
    @commands.is_owner()
    async def uninstall(self, ctx: commands.Context, package: str):
        """
        Remove a package from extra.toml and unload it.

        Parameters
        ----------
        package: str
            The package path to remove.
        """
        if not package_exists(package):
            await ctx.send(f"No package with path `{package}` found in `extra.toml`.")
            return

        steps: list[tuple[str, bool | None]] = [
            ("Unloading extension", None),
            ("Removing from extra.toml", None),
        ]
        msg = await ctx.send(embed=_progress_embed("Removing package…", steps, discord.Color.blurple()))

        async def upd(i: int, success: bool = True):
            steps[i] = (steps[i][0], success)
            await msg.edit(embed=_progress_embed("Removing package…", steps, discord.Color.blurple()))

        # unload
        try:
            await self.bot.unload_extension(package)
        except Exception:
            pass  # may not be loaded
        await upd(0)

        # rmeove from extra.toml
        try:
            remove_package(package)
        except OSError as e:
            steps[1] = (steps[1][0], False)
            await msg.edit(embed=_progress_embed("Removal failed", steps, discord.Color.red()))
            await ctx.send(f"Could not write to `extra.toml`: `{e.strerror}`")
            return

        await upd(1)

        await msg.edit(
            embed=discord.Embed(
                title="Package Removed",
                description=(
                    f"Package `{package}` unloaded and removed from `extra.toml`.\n\n"
                ),
                color=discord.Color.red(),
            )
        )
        log.info(
            f"{ctx.author} ({ctx.author.id}) removed package {package}",
            extra={"webhook": True},
        )

    # ── update ────────────────────────────────────────────────────────────────

    @package.command(name="update")
    @commands.is_owner()
    async def update(self, ctx: commands.Context):
        """
        Select and update installed BallsDex packages.
        """
        packages = read_packages()
        if not packages:
            await ctx.send("No packages registered in `extra.toml`.")
            return

        checking = await ctx.send("Checking for updates…")
        async with aiohttp.ClientSession() as session:
            version_infos = await check_versions(session, packages)
        await checking.delete()

        updates = [v for v in version_infos if v.has_update]
        if not updates:
            await ctx.send("All packages are up to date.")
            return

        lines = [f"• **{v.path}** `{v.installed_tag}` → `{v.latest_tag}`" for v in updates]
        embed = discord.Embed(
            title=f"{len(updates)} Update(s) Available!",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        view = UpdateView(updates, ctx.author.id)
        msg = await ctx.send(embed=embed, view=view)
        await view.wait()

        if not view.confirmed or not view.selected:
            await msg.edit(content="Update cancelled.", embed=None, view=None)
            return

        await msg.edit(content=None, embed=None, view=None)
        selected_map = {v.path: v for v in updates}

        for path in view.selected:
            info = selected_map[path]
            current_location = get_package_location(path) or ""

            steps: list[tuple[str, bool | None]] = [
                (f"Installing {path} {info.latest_tag}", None),
                ("Updating extra.toml", None),
                ("Reloading extension", None),
            ]
            pkg_msg = await ctx.send(embed=_progress_embed(f"Updating {path}…", steps, discord.Color.blurple()))

            async def upd(i: int, success: bool = True, m=pkg_msg, s=steps, p=path):
                s[i] = (s[i][0], success)
                await m.edit(embed=_progress_embed(f"Updating {p}…", s, discord.Color.blurple()))

            async with aiohttp.ClientSession() as session:
                meta, err = await validate_and_fetch_meta(session, current_location)

            if meta is None:
                steps[0] = (steps[0][0], False)
                await pkg_msg.edit(embed=_progress_embed(f"Update failed - {path}", steps, discord.Color.red()))
                await ctx.send(f"{path}: {err}")
                continue

            ok, output = await uv_install(meta.raw_url)
            if not ok:
                steps[0] = (steps[0][0], False)
                await pkg_msg.edit(embed=_progress_embed(f"Update failed - {path}", steps, discord.Color.red()))
                truncated = output[-800:] if len(output) > 800 else output
                await ctx.send(f"`{path}` uv failed:\n```\n{truncated}\n```")
                continue

            await upd(0)

            try:
                if meta.raw_url != current_location:
                    update_package_location(path, meta.raw_url)
            except OSError as e:
                steps[1] = (steps[1][0], False)
                await pkg_msg.edit(embed=_progress_embed(f"Update failed - {path}", steps, discord.Color.red()))
                await ctx.send(f"`{path}` extra.toml write failed: `{e.strerror}`")
                continue

            await upd(1)

            try:
                if path in self.bot.extensions:
                    await self.bot.reload_extension(path)
                else:
                    await self.bot.load_extension(path)
            except Exception as e:
                steps[2] = (steps[2][0], False)
                await pkg_msg.edit(embed=_progress_embed(f"Partial update - {path}", steps, discord.Color.orange()))
                await ctx.send(f"`{path}` updated but reload failed:\n```\n{e}\n```")
                log.warning(
                    f"{ctx.author} updated {path} to {info.latest_tag} but reload failed: {e}",
                    extra={"webhook": True},
                )
                continue

            await upd(2)
            await pkg_msg.edit(
                embed=discord.Embed(
                    title=f"{path} Updated",
                    description=f"`{info.installed_tag}` -> `{info.latest_tag}`",
                    color=discord.Color.green(),
                )
            )
            log.info(
                f"{ctx.author} ({ctx.author.id}) updated {path} "
                f"from {info.installed_tag} to {info.latest_tag}",
                extra={"webhook": True},
            )

    # ── list ──────────────────────────────────────────────────────────────────

    @package.command(name="list")
    @commands.is_owner()
    async def list_packages(self, ctx: commands.Context):
        """
        List all installed BallsDex packages..
        """
        packages = read_packages()
        if not packages:
            await ctx.send("No packages registered in `extra.toml`.")
            return

        msg = await ctx.send("Checking versions…")

        async with aiohttp.ClientSession() as session:
            version_infos = await check_versions(session, packages)

        version_map = {v.path: v for v in version_infos}
        lines: list[str] = []

        for pkg in packages:
            path = pkg["path"]
            loaded = path in self.bot.extensions
            enabled = pkg.get("enabled", True)

            status = "✅" if loaded else ("⏸️" if not enabled else "⚠️")
            info = version_map.get(path)

            if info:
                ver = f"`{info.installed_tag}`"
                if info.has_update:
                    ver += f" -> `{info.latest_tag}`"
            else:
                ver = "`unknown`"

            line = f"{status} **{path}** {ver}"
            if not loaded:
                line += " *(not loaded)*"
            lines.append(line)

        updates_available = sum(1 for v in version_infos if v.has_update)
        embed = discord.Embed(
            title="Installed Packages",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        footer = f"{len(packages)} package(s)"
        if updates_available:
            footer += f" • {updates_available} update(s) available - run `{settings.prefix}package update`"
        embed.set_footer(text=footer)
        await msg.edit(content=None, embed=embed)

    # ── version ───────────────────────────────────────────────────────────────

    @package.command(name="version")
    @commands.is_owner()
    async def version(self, ctx: commands.Context, package: str | None = None):
        """
        View installed and latest versions of BallsDex packages.

        Parameters
        ----------
        package: str | None
            The package path to check.
            If omitted, all installed packages are checked.
        """
        packages = read_packages()
        if not packages:
            await ctx.send("No packages registered in `extra.toml`.")
            return

        if package:
            packages = [p for p in packages if p["path"] == package]
            if not packages:
                await ctx.send(f"No package with path `{package}` found.")
                return

        msg = await ctx.send("Checking versions…")
        async with aiohttp.ClientSession() as session:
            version_infos = await check_versions(session, packages)

        if not version_infos:
            await msg.edit(content="No version information available (only GitHub packages can be checked).")
            return

        lines = []
        for v in version_infos:
            if v.has_update:
                lines.append(f"**{v.path}**: `{v.installed_tag}` -> `{v.latest_tag}` (update available)")
            else:
                lines.append(f"**{v.path}**: `{v.installed_tag}` (up to date)")

        embed = discord.Embed(
            title="Package Versions",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await msg.edit(content=None, embed=embed)

    # ── load ──────────────────────────────────────────────────────────────────

    @package.command(name="load")
    @commands.is_owner()
    async def load(self, ctx: commands.Context, package: str | None = None):
        """
        Load an installed but unloaded package.

        Parameters
        ----------
        package: str | None
            The package path to load.
            If omitted, all unloaded packages in extra.toml are loaded.
        """
        packages = read_packages()
        if not packages:
            await ctx.send("No packages registered in `extra.toml`.")
            return

        targets = [p for p in packages if p.get("enabled", True)]
        if package:
            targets = [p for p in targets if p["path"] == package]
            if not targets:
                await ctx.send(f"No enabled package with path `{package}` found.")
                return

        loaded, failed = [], []
        for pkg in targets:
            path = pkg["path"]
            if path in self.bot.extensions:
                continue
            try:
                await self.bot.load_extension(path)
                loaded.append(path)
            except Exception as e:
                failed.append(f"`{path}`: {e}")
                log.warning(f"Failed to load {path}: {e}", exc_info=True)

        lines = []
        if loaded:
            lines.append("**Loaded:**\n" + "\n".join(f"`{p}`" for p in loaded))
        if failed:
            lines.append("**Failed:**\n" + "\n".join(f"{f}" for f in failed))
        if not loaded and not failed:
            lines.append("All packages are already loaded.")

        await ctx.send("\n\n".join(lines) or "Nothing to load.")

    # ── info ──────────────────────────────────────────────────────────────────

    @package.command(name="info")
    @commands.is_owner()
    async def info(self, ctx: commands.Context, *, url: str):
        """
        Show metadata about a package from GitHub without installing it.
				
        Parameters
        ----------
        url: str
            The GitHub repository URL or git+ URL of the package.
        """
        msg = await ctx.send("Fetching package info…")

        async with aiohttp.ClientSession() as session:
            meta, err = await validate_and_fetch_meta(session, url)

        if meta is None:
            await msg.edit(content=f"{err}")
            return

        already = package_exists(meta.path)
        loaded = meta.path in self.bot.extensions
        status_parts = []
        if already:
            status_parts.append("registered in `extra.toml`")
        if loaded:
            status_parts.append("currently loaded")
        status = " • ".join(status_parts) if status_parts else "not installed"

        subdir_note = f"\n• Subdirectory: `{meta.subdir}`" if meta.subdir else ""

        embed = discord.Embed(
            title=meta.name,
            description=meta.description or "*No description provided.*",
            color=discord.Color.gold() if already else discord.Color.greyple(),
        )
        embed.add_field(
            name="Details",
            value=(
                f"• Version: `{meta.version}`\n"
                f"• Path: `{meta.path}`\n"
                f"• Repo: `{meta.repo_owner}/{meta.repo_name}`"
                f"{subdir_note}\n"
                f"• Status: {status}"
            ),
            inline=False,
        )
        embed.add_field(name="Install URL", value=f"`{meta.raw_url}`", inline=False)
        if not already:
            embed.set_footer(text=f"Run: {setting.prefix}package install {url}")

        await msg.edit(content=None, embed=embed)
 
