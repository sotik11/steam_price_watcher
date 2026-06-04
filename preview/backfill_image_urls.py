"""One-shot: re-run fetch_card_metadata for any watchlist entry missing
`image_url`, saving the result. Run once after the sendPhoto change."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from steam import fetch_card_metadata  # noqa: E402

WATCHLIST = Path(__file__).resolve().parent.parent / "watchlist.json"

items = json.loads(WATCHLIST.read_text(encoding="utf-8"))
for it in items:
    if it.get("image_url"):
        continue
    meta = fetch_card_metadata(it["appid"], it["market_hash_name"])
    it["image_url"] = meta.get("image_url")
    name = it["market_hash_name"][:35]
    url = meta.get("image_url") or "(none)"
    print(f"{name:35s} -> {url[:80]}")

WATCHLIST.write_text(
    json.dumps(items, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print("saved", WATCHLIST)
