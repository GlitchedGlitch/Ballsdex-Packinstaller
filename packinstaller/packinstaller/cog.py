"""
PackInstaller cog :3
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord.ext import commands

from settings.models import settings

from .github import (
    RateLimitError,
    VersionInfo,
    check_versions,
    discover_extension,
    uv_install,
    uv_reinstall,
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

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.packinstaller")

BAR_FILLED, BAR_EMPTY, BAR_LEN = "█", "░", 12


def _bar(current: int, total: int) -> str:
    if total == 0:
        return f"`{'░' * BAR_LEN}` 0%"
    filled = round(BAR_LEN * current / total)
    return f"`{BAR_FILLED * filled}{BAR_EMPTY * (BAR_LEN - filled)}` {round(100 * current / total)}%"


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


def _status_icon(loaded: bool, enabled: bool, gh_available: bool | None = True) -> str:
    if not enabled:
        return "⏸️"
    if gh_available is False:
        return "⚠️"
    if loaded:
        return "✅"
    return "🔴"


# ── Interactive update views ──────────────────────────────────────────────────

class UpdateSelect(discord.ui.Select):
    def __init__(self, updates: list[VersionInfo]):
        options = [
            discord.SelectOption(
                label=v.path,
                description=f"{v.installed_tag} -> {v.latest_tag}",
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


class UpdateConfirmView(discord.ui.View):
    def __init__(self, selected: list[str], owner_id: int):
        super().__init__(timeout=60)
        self.selected = selected
        self.owner_id = owner_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm Update", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()


class UpdateSelectView(discord.ui.View):
    def __init__(self, updates: list[VersionInfo], owner_id: int):
        super().__init__(timeout=120)
        self.selected: list[str] = []
        self.owner_id = owner_id
        self.proceed = False
        self.add_item(UpdateSelect(updates))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Next ->", style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected:
            await interaction.response.send_message(
                "Select at least one package first.", ephemeral=True
            )
            return
        self.proceed = True
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

    @commands.group(name="package", invoke_without_command=True)
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
        Install a BallsDex package from GitHub.

        Parameters
        ----------
        url: str
            GitHub URL, git+ or subdirectories.
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
            await ctx.send(
                f"`{meta.path}` is already installed. "
                f"Use `{settings.prefix}package update` instead."
            )
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
        toml_written = False
        try:
            add_package(
                {"location": meta.raw_url, "path": meta.path, "enabled": True},
                comment=meta.name,
            )
            toml_written = True
        except OSError:
            pass

        await upd(2, success=toml_written)

        # load extension
        extension = meta.extension or discover_extension(meta.path)
        try:
            await self.bot.load_extension(extension)
        except Exception as e:
            steps[3] = (steps[3][0], False)
            await msg.edit(embed=_progress_embed("Partial install", steps, discord.Color.orange()))
            await ctx.send(
                f"Package installed but failed to load `{extension}`:\n```\n{e}\n```\n"
                "It will load on the next restart."
            )
            log.warning(
                f"{ctx.author} installed {meta.name} ({extension}) but load failed: {e}",
                extra={"webhook": True},
            )
            return

        await upd(3)

        toml_note = (
            "" if toml_written
            else "\nCould not write to `extra.toml` (read-only). "
                 "This install is **runtime-only** and will not survive a rebuild."
        )

        await msg.edit(
            embed=discord.Embed(
                title="Package Installed",
                description=(
                    f"**{meta.name}** `{meta.version}`\n"
                    f"{meta.description}\n\n"
                    f"• Path: `{meta.path}`\n"
                    f"• Extension: `{extension}`\n"
                    f"• Source: `{meta.raw_url}`"
                    f"{toml_note}"
                ),
                color=discord.Color.green(),
            )
        )
        log.info(
            f"{ctx.author} ({ctx.author.id}) installed {meta.name} "
            f"({extension}) v{meta.version}",
            extra={"webhook": True},
        )

    # ── uninstall ─────────────────────────────────────────────────────────────

    @package.command(name="uninstall")
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
        extension = discover_extension(package)
        for candidate in [extension, package]:
            try:
                await self.bot.unload_extension(candidate)
                break
            except Exception:
                pass
        await upd(0)

        # remove
        try:
            remove_package(package)
            await upd(1)
        except OSError as e:
            steps[1] = (steps[1][0], False)
            await msg.edit(embed=_progress_embed("Removal failed", steps, discord.Color.red()))
            await ctx.send(f"Could not write to `extra.toml`: `{e.strerror}`")
            return

        await msg.edit(
            embed=discord.Embed(
                title="Package Removed",
                description=(f"`{package}` unloaded and removed"),
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
        Interactive menu to select and update installed packages.
        """
        packages = [p for p in read_packages() if p.get("location", "").startswith("git+https://github.com/")]
        if not packages:
            await ctx.send("No GitHub-sourced packages registered in `extra.toml`.")
            return

        checking = await ctx.send("Checking for updates…")
        try:
            async with aiohttp.ClientSession() as session:
                version_infos = await check_versions(session, packages)
        except Exception as e:
            await checking.edit(content=f"Failed to check versions: {e}")
            return

        await checking.delete()

        updates = [v for v in version_infos if v.has_update]
        if not updates:
            gh_unavailable = [v for v in version_infos if not v.github_available]
            note = (
                f"\n{len(gh_unavailable)} package(s) could not be checked (GitHub unavailable)."
                if gh_unavailable else ""
            )
            await ctx.send(f"All packages are up to date!{note}")
            return

        # Selection step
        lines = [f"• **{v.path}** `{v.installed_tag}` -> `{v.latest_tag}`" for v in updates]
        select_embed = discord.Embed(
            title=f"{len(updates)} Update(s) Available!",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        select_view = UpdateSelectView(updates, ctx.author.id)
        select_msg = await ctx.send(embed=select_embed, view=select_view)
        await select_view.wait()

        if not select_view.proceed or not select_view.selected:
            await select_msg.edit(content="Update cancelled.", embed=None, view=None)
            return

        # Confirmation step
        selected_infos = [v for v in updates if v.path in select_view.selected]
        conf_lines = [f"• **{v.path}** `{v.installed_tag}` -> `{v.latest_tag}`" for v in selected_infos]
        conf_embed = discord.Embed(
            title="Confirm Update",
            description="Update the following packages?\n\n" + "\n".join(conf_lines),
            color=discord.Color.orange(),
        )
        conf_view = UpdateConfirmView(select_view.selected, ctx.author.id)
        await select_msg.edit(embed=conf_embed, view=conf_view)
        await conf_view.wait()

        if not conf_view.confirmed:
            await select_msg.edit(content="Update cancelled.", embed=None, view=None)
            return

        await select_msg.edit(embed=None, view=None, content="Updating packages…")

        # Update 
        results: list[tuple[str, bool, str]] = []

        for info in selected_infos:
            path = info.path
            current_location = get_package_location(path) or ""

            steps: list[tuple[str, bool | None]] = [
                (f"Installing {path} {info.latest_tag} (--reinstall-package)", None),
                ("Verifying installation", None),
                ("Updating extra.toml", None),
                ("Reloading extension", None),
            ]
            pkg_msg = await ctx.send(
                embed=_progress_embed(f"Updating {path}…", steps, discord.Color.blurple())
            )

            async def upd(i: int, success: bool = True, m=pkg_msg, s=steps, p=path):
                s[i] = (s[i][0], success)
                await m.edit(embed=_progress_embed(f"Updating {p}…", s, discord.Color.blurple()))

            # Re-validate url
            async with aiohttp.ClientSession() as session:
                meta, err = await validate_and_fetch_meta(session, current_location)

            if meta is None:
                steps[0] = (steps[0][0], False)
                await pkg_msg.edit(embed=_progress_embed(f"Update failed - {path}", steps, discord.Color.red()))
                results.append((path, False, err))
                continue

            # Install
            ok, output = await uv_reinstall(meta.raw_url, meta.name)
            if not ok:
                steps[0] = (steps[0][0], False)
                await pkg_msg.edit(embed=_progress_embed(f"Update failed - {path}", steps, discord.Color.red()))
                truncated = output[-800:] if len(output) > 800 else output
                await ctx.send(f"`{path}` uv failed:\n```\n{truncated}\n```")
                results.append((path, False, "uv pip install failed"))
                continue

            await upd(0)

            # Verify
            extension = meta.extension or discover_extension(path)
            try:
                import importlib
                importlib.import_module(extension)
                await upd(1)
            except Exception as e:
                steps[1] = (steps[1][0], False)
                await pkg_msg.edit(embed=_progress_embed(f"Update failed - {path}", steps, discord.Color.red()))
                results.append((path, False, f"Verification failed: {e}"))
                continue

            # Update file
            toml_updated = False
            try:
                if meta.raw_url != current_location:
                    update_package_location(path, meta.raw_url)
                toml_updated = True
            except OSError:
                pass
            await upd(2, success=toml_updated)

            # Reload
            try:
                if extension in self.bot.extensions:
                    await self.bot.reload_extension(extension)
                else:
                    await self.bot.load_extension(extension)
                await upd(3)
            except Exception as e:
                steps[3] = (steps[3][0], False)
                await pkg_msg.edit(
                    embed=_progress_embed(f"Partial update - {path}", steps, discord.Color.orange())
                )
                results.append((path, False, f"Reload failed: {e}"))
                log.warning(
                    f"{ctx.author} updated {path} to {info.latest_tag} but reload failed: {e}",
                    extra={"webhook": True},
                )
                continue

            not_persistent = "" if toml_updated else " *(runtime-only - extra.toml not writable)*"
            await pkg_msg.edit(
                embed=discord.Embed(
                    title=f"{path} Updated",
                    description=f"`{info.installed_tag}` -> `{info.latest_tag}`{not_persistent}",
                    color=discord.Color.green(),
                )
            )
            results.append((path, True, f"{info.installed_tag} -> {info.latest_tag}"))
            log.info(
                f"{ctx.author} ({ctx.author.id}) updated {path} "
                f"from {info.installed_tag} to {info.latest_tag}",
                extra={"webhook": True},
            )

        # Summary
        success_count = sum(1 for _, ok, _ in results if ok)
        fail_count = len(results) - success_count
        summary_lines = [
            f"{'✅' if ok else '❌'} **{p}** - {msg}"
            for p, ok, msg in results
        ]
        await ctx.send(
            embed=discord.Embed(
                title=f"Update complete - {success_count} succeeded, {fail_count} failed",
                description="\n".join(summary_lines),
                color=discord.Color.green() if not fail_count else discord.Color.orange(),
            )
        )

    # ── list ──────────────────────────────────────────────────────────────────

    @package.command(name="list")
    @commands.is_owner()
    async def list_packages(self, ctx: commands.Context):
        """
        List all installed packages with status.
        """
        packages = read_packages()
        if not packages:
            await ctx.send("No packages registered in `extra.toml`.")
            return

        msg = await ctx.send("Checking versions (may be cached)…")

        # Check
        gh_packages = [p for p in packages if p.get("location", "").startswith("git+https://github.com/")]
        try:
            async with aiohttp.ClientSession() as session:
                version_infos = await check_versions(session, gh_packages)
        except Exception:
            version_infos = []

        version_map = {v.path: v for v in version_infos}
        lines: list[str] = []

        for pkg in packages:
            path = pkg["path"]
            enabled = pkg.get("enabled", True)
            location = pkg.get("location", "")
            info = version_map.get(path)

            extension = discover_extension(path)
            loaded = extension in self.bot.extensions or path in self.bot.extensions

            # Status
            if not enabled:
                status = "⏸️"
            elif info and not info.github_available:
                status = "⚠️"
            elif loaded:
                status = "✅"
            else:
                status = "🔴"

            # Version string
            if info:
                if info.has_update:
                    ver = f"`{info.installed_tag}` -> `{info.latest_tag}`"
                elif not info.github_available:
                    ver = f"`{info.installed_tag}` *(GitHub unavailable)*"
                else:
                    ver = f"`{info.installed_tag}`"
            elif location:
                from .github import extract_tag_from_location
                tag = extract_tag_from_location(location)
                ver = f"`{tag}`" if tag else "`unknown`"
            else:
                ver = "`local`"

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
            footer += f" • {updates_available} update(s)! - run `{settings.prefix}package update`"
        embed.set_footer(text=footer)
        await msg.edit(content=None, embed=embed)

    # ── version ───────────────────────────────────────────────────────────────

    @package.command(name="version")
    @commands.is_owner()
    async def version(self, ctx: commands.Context, package: str | None = None):
        """
        Check version(s) of installed packages against GitHub.

        Parameters
        ----------
        package: str | None
            Package path to check. Omit to check all.
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
        try:
            async with aiohttp.ClientSession() as session:
                version_infos = await check_versions(session, packages)
        except RateLimitError as e:
            await msg.edit(content=f"{e}")
            return

        if not version_infos:
            await msg.edit(content="No version information available for these packages.")
            return

        lines = []
        for v in version_infos:
            if not v.github_available:
                lines.append(f"**{v.path}**: `{v.installed_tag}` *(GitHub unavailable)*")
            elif v.has_update:
                lines.append(f"**{v.path}**: ~~`{v.installed_tag}`~~ `{v.latest_tag}` - update available!")
            else:
                lines.append(f"**{v.path}**: `{v.installed_tag}` - up to date")

        await msg.edit(
            content=None,
            embed=discord.Embed(
                title="Package Versions",
                description="\n".join(lines),
                color=discord.Color.blurple(),
            ),
        )

    # ── load ──────────────────────────────────────────────────────────────────

    @package.command(name="load")
    @commands.is_owner()
    async def load(self, ctx: commands.Context, package: str | None = None):
        """
        Load an installed but unloaded package.

        Parameters
        ----------
        package: str | None
            Package path to load. Omit to load all unloaded packages.
        """
        packages = [p for p in read_packages() if p.get("enabled", True)]
        if not packages:
            await ctx.send("No enabled packages in `extra.toml`.")
            return

        if package:
            packages = [p for p in packages if p["path"] == package]
            if not packages:
                await ctx.send(f"No enabled package with path `{package}` found.")
                return

        loaded, already, failed = [], [], []

        for pkg in packages:
            path = pkg["path"]
            extension = discover_extension(path)

            if extension in self.bot.extensions or path in self.bot.extensions:
                already.append(path)
                continue

            try:
                await self.bot.load_extension(extension)
                loaded.append(f"`{path}` (ext: `{extension}`)")
            except Exception as e:
                failed.append(f"`{path}` (`{extension}`): {e}")
                log.warning(f"Failed to load {extension}: {e}", exc_info=True)

        lines = []
        if loaded:
            lines.append("**Loaded:**\n" + "\n".join(f"{p}" for p in loaded))
        if already:
            lines.append("**Already loaded:**\n" + "\n".join(f"`{p}`" for p in already))
        if failed:
            lines.append("**Failed:**\n" + "\n".join(f"{f}" for f in failed))
        if not loaded and not already and not failed:
            lines.append("Nothing to load.")

        await ctx.send("\n\n".join(lines))

    # ── info ──────────────────────────────────────────────────────────────────

    @package.command(name="info")
    @commands.is_owner()
    async def info(self, ctx: commands.Context, *, url: str):
        """
        Show metadata about a package from GitHub without installing.

        Parameters
        ----------
        url: str
            GitHub URL, git+ oe subdirectories.
        """
        msg = await ctx.send("Fetching package info…")

        async with aiohttp.ClientSession() as session:
            meta, err = await validate_and_fetch_meta(session, url)

        if meta is None:
            await msg.edit(content=f"{err}")
            return

        already = package_exists(meta.path)
        extension = meta.extension or discover_extension(meta.path)
        loaded = extension in self.bot.extensions or meta.path in self.bot.extensions

        status_parts = []
        if already:
            status_parts.append("registered in `extra.toml`")
        if loaded:
            status_parts.append("currently loaded")
        status = " • ".join(status_parts) if status_parts else "not installed"

        subdir_note = f"\n• Subdirectory: `{meta.subdir}`" if meta.subdir else ""
        reload_cmd = f"`{settings.prefix}reload {extension}`"

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
                f"• Extension: `{extension}`\n"
                f"• Repo: `{meta.repo_owner}/{meta.repo_name}`"
                f"{subdir_note}\n"
                f"• Reload: {reload_cmd}\n"
                f"• Status: {status}"
            ),
            inline=False,
        )
        embed.add_field(name="Install URL", value=f"`{meta.raw_url}`", inline=False)
        if not already:
            embed.set_footer(text=f"Run: {settings.prefix}package install {url}")

        await msg.edit(content=None, embed=embed)
