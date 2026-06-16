"""
TFT Set 17 "best items" Discord bot  (tactics.tools / tft.tools data)

Slash commands (type "/" in Discord to see them all):
  /help                            - how to use the bot
  /item <unit> [rank] [item_type]  - best item combos + singles, best first
  /filters                         - list rank filters and item types

Key features:
  * Partial / fuzzy unit names: "Blitz" -> Blitzcrank, "chogath" -> Cho'Gath
  * AUTO-UPDATES each patch: the current patch id is discovered from the live
    site at startup (and refreshed), so no manual id changes are needed.
  * Correct item display names (Giant Slayer, Edge of Night, ...) via static data.

DATA SOURCE (undocumented private API; verified live 2026-06-15):
  Unit stats:  https://d3.tft.tools/stats3/unit/{QUEUE}/{unitId}/{PATCH}/{rankGroup}
     (stats2/unit returns the same payload; stats3 used here)
     QUEUE     = 1100 (ranked)
     rankGroup = 0 Master+ | 1 Diamond+ | 2 Emerald+ | 3 Platinum+ | 4 GM+
  Payload: base{place,top4,won,count}, items[], itemPairs[], itemTrios[]
     entry: {items:[ids...], count, place, top4, won, adjDelta}
     adjDelta = "Score" (more negative = stronger) -> sort ascending.
  Static names/tags: https://ap.tft.tools/static/s17/data.js (window.data17)
  Current patch id:  tactics.tools homepage -> buildId -> _next/data/.../units.json

Run:
    pip install "discord.py>=2.3" aiohttp
    export DISCORD_TOKEN="..."
    export TEST_GUILD_ID="..."     # optional: instant slash sync
    python tft_item_bot.py
"""

import os
import re
import json
import difflib
import asyncio
import logging
from typing import Optional

import aiohttp
import discord
from discord import app_commands

# Load token (and any other vars) from a local .env file if present.
# This is optional: Codespaces Secrets / plain env vars also work.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ASGbot")

SET = "17"
QUEUE = "1100"
STATIC_URL = f"https://ap.tft.tools/static/s{SET}/data.js"
UNIT_URL = "https://d3.tft.tools/stats3/unit/{q}/{unit}/{patch}/{rg}"
HOME_URL = "https://tactics.tools/"
FALLBACK_PATCH = "16121"          # used only if auto-discovery fails
PATCH_REFRESH_SECONDS = 6 * 3600  # re-check current patch every 6h

N_COMBOS, N_SINGLES = 5, 6
MIN_GAMES = int(os.getenv("TFT_MIN_GAMES", "50"))
HEADERS = {"User-Agent": "Mozilla/5.0 (tft-bot)"}

RANKS = {"Platinum+": 3, "Emerald+": 2, "Diamond+": 1, "Master+": 0, "GM+": 4}
RANK_BY_ID = {v: k for k, v in RANKS.items()}
DEFAULT_RANK = 2  # Emerald+

ITEM_TYPES = {
    "All": lambda tags: True,
    "Craftable": lambda tags: "Craftable" in tags,
    "Artifact (Ornn)": lambda tags: "Artifact" in tags,
    "Radiant": lambda tags: "Radiant" in tags,
    "Emblem": lambda tags: any("Emblem" in t for t in tags),
    "Mod": lambda tags: "Mod" in tags,
    "Component": lambda tags: "Component" in tags,
}

PATCH_LABEL = {
    "16121": "17.5b", "16120": "17.5", "16111": "17.4b", "16110": "17.4",
    "16100": "17.3", "16091": "17.2b", "16090": "17.2", "16081": "17.1b", "16080": "17.1",
}


def _norm(s: str) -> str:
    """Lowercase and strip non-alphanumerics so 'Bel'Veth' == 'belveth'."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ---------------------------------------------------------------- static data
class Static:
    def __init__(self):
        self.norm_to_id: dict[str, str] = {}   # 'blitzcrank' -> 'TFT17_Blitzcrank'
        self.id_to_name: dict[str, str] = {}
        self.item_name: dict[str, str] = {}
        self.item_tags: dict[str, list] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def ensure(self, session: aiohttp.ClientSession):
        async with self._lock:
            if self._loaded:
                return
            async with session.get(STATIC_URL, headers=HEADERS, timeout=20) as r:
                r.raise_for_status()
                js = await r.text()
            d = json.loads(re.search(r"JSON\.parse\(`(.*)`\)", js, re.S).group(1))
            for uid, u in d.get("units", {}).items():
                nm = u.get("name")
                if nm:
                    self.id_to_name[uid] = nm
                    self.norm_to_id[_norm(nm)] = uid
            for iid, meta in d.get("items", {}).items():
                self.item_name[iid.lower()] = meta.get("name", iid)
                self.item_tags[iid.lower()] = meta.get("tags", [])
            self._loaded = True
            log.info("static: %d units, %d items", len(self.id_to_name), len(self.item_name))

    def resolve_unit(self, q: str) -> Optional[str]:
        """Exact -> unique prefix -> substring -> fuzzy.  'Blitz' -> Blitzcrank."""
        nq = _norm(q)
        if not nq:
            return None
        if nq in self.norm_to_id:               # exact (ignoring punctuation)
            return self.norm_to_id[nq]
        keys = list(self.norm_to_id)
        pref = [k for k in keys if k.startswith(nq)]
        if pref:                                 # 'blitz' -> 'blitzcrank'
            return self.norm_to_id[min(pref, key=len)]
        sub = [k for k in keys if nq in k]
        if sub:
            return self.norm_to_id[min(sub, key=len)]
        m = difflib.get_close_matches(nq, keys, n=1, cutoff=0.6)
        return self.norm_to_id[m[0]] if m else None

    def iname(self, iid: str) -> str:
        return self.item_name.get(iid.lower(), iid)

    def itags(self, iid: str) -> list:
        return self.item_tags.get(iid.lower(), [])


static = Static()


# ---------------------------------------------------------------- patch discovery
class Patch:
    def __init__(self):
        self.id = FALLBACK_PATCH

    async def discover(self, session: aiohttp.ClientSession):
        # primary: homepage buildId -> units.json -> aperture.patch._0
        try:
            async with session.get(HOME_URL, headers=HEADERS, timeout=20) as r:
                html = await r.text()
            build = re.search(r'"buildId":"([^"]+)"', html).group(1)
            url = f"https://tactics.tools/_next/data/{build}/en/units.json"
            async with session.get(url, headers=HEADERS, timeout=20) as r:
                d = await r.json(content_type=None)
            pid = str(d["pageProps"]["aperture"]["patch"]["_0"])
            self.id = pid
            log.info("current patch id = %s (%s)", pid, PATCH_LABEL.get(pid, "?"))
            return
        except Exception as e:
            log.warning("patch discovery (primary) failed: %s", e)
        # fallback: app.js patch array, newest first
        try:
            async with session.get(HOME_URL, headers=HEADERS, timeout=20) as r:
                html = await r.text()
            app = re.search(r'/_next/static/chunks/pages/_app-[a-f0-9]+\.js', html).group(0)
            async with session.get("https://tactics.tools" + app, headers=HEADERS, timeout=20) as r:
                js = await r.text()
            arr = re.search(r'\[(16\d{3}(?:,16\d{3})+)\]', js).group(1)
            self.id = arr.split(",")[0]
            log.info("current patch id = %s (via app.js fallback)", self.id)
        except Exception as e:
            log.warning("patch discovery (fallback) failed, using %s: %s", self.id, e)


patch = Patch()


# ---------------------------------------------------------------- stats fetch
_cache: dict[tuple, dict] = {}


async def fetch_unit(session, unit_id: str, rg: int) -> dict:
    key = (unit_id, patch.id, rg)
    if key in _cache:
        return _cache[key]
    url = UNIT_URL.format(q=QUEUE, unit=unit_id, patch=patch.id, rg=rg)
    try:
        async with session.get(url, headers=HEADERS, timeout=20) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
    except aiohttp.ClientResponseError as e:
        if e.status in (400, 404, 500):          # likely stale patch -> rediscover once
            await patch.discover(session)
            url = UNIT_URL.format(q=QUEUE, unit=unit_id, patch=patch.id, rg=rg)
            async with session.get(url, headers=HEADERS, timeout=20) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
            key = (unit_id, patch.id, rg)
        else:
            raise
    _cache[key] = data
    return data


def _combo_ok(items, label):
    pred = ITEM_TYPES[label]
    return any(pred(static.itags(i)) for i in items)


def build_embed(unit_name, data, rg, label) -> discord.Embed:
    b = data.get("base", {})
    e = discord.Embed(title=f"Best Items for {unit_name}", color=0x4E9CD6)
    e.description = (
        f"Avg Place: **{b.get('place','?')}** | Top 4: **{b.get('top4','?')}%** | "
        f"Win Rate: **{b.get('won','?')}%**\n"
        f"Rank filter: **{RANK_BY_ID.get(rg, rg)}** | Patch **{PATCH_LABEL.get(patch.id, patch.id)}**"
        + (f" | Type: **{label}**" if label != "All" else "")
    )
    trios = [t for t in data.get("itemTrios", []) if t.get("count", 0) >= MIN_GAMES]
    if label != "All":
        trios = [t for t in trios if _combo_ok(t["items"], label)]
    trios.sort(key=lambda t: t.get("adjDelta", 0))
    if trios:
        lines = []
        for i, t in enumerate(trios[:N_COMBOS], 1):
            names = " + ".join(static.iname(x) for x in t["items"])
            lines.append(f"`{i}.` {names}\n\u2003Avg Place: `{t['place']:.2f}` | "
                         f"Top 4: `{t['top4']:.1f}%` | Score: `{t.get('adjDelta',0):.3f}`")
        e.add_field(name="Best 3-Item Combos (ascending by performance)",
                    value="\n".join(lines)[:1024], inline=False)
    singles = [s for s in data.get("items", []) if s.get("count", 0) >= MIN_GAMES]
    if label != "All":
        singles = [s for s in singles if ITEM_TYPES[label](static.itags(s["items"][0]))]
    singles.sort(key=lambda s: s.get("adjDelta", 0))
    if singles:
        lines = [f"\u2022 {static.iname(s['items'][0])} — Place: `{s['place']:.2f}` | "
                 f"Score: `{s.get('adjDelta',0):.3f}`" for s in singles[:N_SINGLES]]
        e.add_field(name="Best Individual Items", value="\n".join(lines)[:1024], inline=False)
    if not trios and not singles:
        e.add_field(name="No data", value="No items match that filter for this unit.", inline=False)
    e.set_footer(text="data: tft.tools")
    return e


# ---------------------------------------------------------------- discord
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await patch.discover(self.session)
        try:
            await static.ensure(self.session)
        except Exception as e:
            log.error("static preload failed: %s", e)
        self.loop.create_task(self._refresh_patch_loop())
        gid = os.getenv("TEST_GUILD_ID")
        if gid:
            g = discord.Object(id=int(gid))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    async def _refresh_patch_loop(self):
        while True:
            await asyncio.sleep(PATCH_REFRESH_SECONDS)
            old = patch.id
            await patch.discover(self.session)
            if patch.id != old:
                _cache.clear()
                log.info("patch changed %s -> %s, cache cleared", old, patch.id)

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()


client = Bot()


@client.event
async def on_ready():
    log.info("Logged in as %s", client.user)


@client.tree.command(name="help", description="How to use the TFT item bot.")
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(title="ASGbot \u2014 Help", color=0x4E9CD6)
    e.description = ("Find the strongest items and item combos for any Set 17 unit, "
                     "ranked by performance score (lower = better).")
    e.add_field(name="/item  <unit>  [rank]  [item_type]",
                value=("\u2022 `unit` \u2014 full or partial name; *Blitz* finds *Blitzcrank*\n"
                       "\u2022 `rank` \u2014 click to pick Platinum+ / Emerald+ / Diamond+ / Master+ / GM+\n"
                       "\u2022 `item_type` \u2014 click to pick Craftable / Artifact / Radiant / Emblem / ...\n"
                       "Example: `/item unit:Blitz rank:Diamond+ item_type:Craftable`"),
                inline=False)
    e.add_field(name="/filters", value="List every rank filter and item type.", inline=False)
    e.add_field(name="Score", value="`adjDelta` \u2014 placement impact; more negative = stronger.",
                inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@client.tree.command(name="filters", description="List available rank filters and item types.")
async def filters_cmd(interaction: discord.Interaction):
    e = discord.Embed(title="Available filters", color=0x4E9CD6)
    e.add_field(name="Ranks (rank:)", value="\n".join(f"\u2022 {k}" for k in RANKS), inline=True)
    e.add_field(name="Item types (item_type:)", value="\n".join(f"\u2022 {k}" for k in ITEM_TYPES), inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)


async def unit_autocomplete(interaction: discord.Interaction, current: str):
    cur = _norm(current)
    names = sorted(static.id_to_name.values())
    hits = [n for n in names if cur in _norm(n)][:25] or names[:25]
    return [app_commands.Choice(name=n, value=n) for n in hits]


@client.tree.command(name="item", description="Best items & combos for a TFT unit.")
@app_commands.describe(unit="Unit name or partial (e.g. Blitz)",
                       rank="Rank bracket", item_type="Restrict to an item category")
@app_commands.choices(
    rank=[app_commands.Choice(name=k, value=v) for k, v in RANKS.items()],
    item_type=[app_commands.Choice(name=k, value=k) for k in ITEM_TYPES],
)
@app_commands.autocomplete(unit=unit_autocomplete)
async def item_cmd(interaction: discord.Interaction, unit: str,
                   rank: Optional[app_commands.Choice[int]] = None,
                   item_type: Optional[app_commands.Choice[str]] = None):
    await interaction.response.defer(thinking=True)
    try:
        await static.ensure(client.session)
        unit_id = static.resolve_unit(unit)
        if not unit_id:
            sug = difflib.get_close_matches(_norm(unit),
                                            [_norm(n) for n in static.id_to_name.values()],
                                            n=3, cutoff=0.3)
            names = {_norm(n): n for n in static.id_to_name.values()}
            hint = f" Did you mean: {', '.join(names[s] for s in sug)}?" if sug else ""
            await interaction.followup.send(f"Unknown unit **{unit}**.{hint}")
            return
        rg = rank.value if rank else DEFAULT_RANK
        label = item_type.value if item_type else "All"
        data = await fetch_unit(client.session, unit_id, rg)
        await interaction.followup.send(embed=build_embed(static.id_to_name[unit_id], data, rg, label))
    except Exception as e:
        log.exception("item error")
        await interaction.followup.send(f"Something went wrong: `{e}`")


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "No DISCORD_TOKEN found. Put your token in a .env file like:\n"
            "    DISCORD_TOKEN=your-token-here\n"
            "or set it as a Codespaces Secret named DISCORD_TOKEN."
        )
    client.run(token)


if __name__ == "__main__":
    main()