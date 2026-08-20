"""Card images, cached locally.

Art comes from the HearthstoneJSON art CDN -- the same public project that
supplies our card metadata:

    https://art.hearthstonejson.com/v1/render/latest/enUS/256x/<cardId>.png
                                                             rendered card,
                                                             frame + name + stats
    https://art.hearthstonejson.com/v1/tiles/<cardId>.png    wide name strip

Images are fetched once and kept on disk. Nothing is fetched during a match: the
comp card lists are known up front, so everything is prefetched.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

# /v1/256x/ is the bare artwork with no frame, name or stats -- use the render
# endpoint, which produces the card as the player sees it.
RENDER = "https://art.hearthstonejson.com/v1/render/latest/enUS/256x/{cid}.png"
# The render endpoint only covers a minority of Battlegrounds cards, so fall
# back to bare artwork -- we draw the name ourselves in either case.
ART = "https://art.hearthstonejson.com/v1/256x/{cid}.jpg"
TILE = "https://art.hearthstonejson.com/v1/tiles/{cid}.png"
UA = "hs-bg-agent/0.1 (personal Battlegrounds advisor)"
CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "art"


class ArtCache:
    def __init__(self, cache_dir: Path | None = None):
        self.dir = cache_dir or CACHE
        (self.dir / "render").mkdir(parents=True, exist_ok=True)
        (self.dir / "tile").mkdir(parents=True, exist_ok=True)
        self._missing: set[str] = set()      # remember 404s, do not retry them

    _URL = {"render": RENDER, "art": ART, "tile": TILE}
    _EXT = {"render": "png", "art": "jpg", "tile": "png"}

    def path(self, card_id: str, kind: str = "render") -> Path:
        (self.dir / kind).mkdir(parents=True, exist_ok=True)
        return self.dir / kind / f"{card_id}.{self._EXT[kind]}"

    def get(self, card_id: str, kind: str = "render", fetch: bool = True) -> Path | None:
        """Local path to the image, downloading it once if needed."""
        p = self.path(card_id, kind)
        if p.exists() and p.stat().st_size > 0:
            return p
        key = f"{kind}/{card_id}"
        if not fetch or key in self._missing:
            return None
        url = self._URL[kind].format(cid=card_id)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            self._missing.add(key)
            return None
        if not data:
            self._missing.add(key)
            return None
        p.write_bytes(data)
        return p

    def best(self, card_id: str, fetch: bool = True) -> Path | None:
        """The nicest image available: full render if it exists, else artwork."""
        return (self.get(card_id, "render", fetch)
                or self.get(card_id, "art", fetch))

    def prefetch(self, card_ids, kind: str = "render") -> tuple[int, int]:
        """Returns (available, missing)."""
        ok = bad = 0
        for cid in dict.fromkeys(card_ids):
            if self.get(cid, kind):
                ok += 1
            else:
                bad += 1
        return ok, bad
