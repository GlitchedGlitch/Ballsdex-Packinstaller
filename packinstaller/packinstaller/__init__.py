from __future__ import annotations
from typing import TYPE_CHECKING
from .cog import PackagesCog

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

async def setup(bot: "BallsDexBot") -> None:
    await bot.add_cog(PackagesCog(bot))

async def teardown(bot: "BallsDexBot") -> None:
    pass
 
