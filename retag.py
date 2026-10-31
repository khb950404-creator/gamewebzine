"""기존 게시물의 해시태그 댓글 재작성 (1회성).
  python retag.py list   게시물과 해시태그 댓글을 출력만 한다
  python retag.py apply  data/retag.json({media: "태그들"})대로 새 댓글을 달고, 옛 해시태그 댓글은 숨김을 시도한다
"""
import json
import os
import sys
import time
from pathlib import Path

import ig_post as g

ROOT = Path(__file__).parent
PLAN = ROOT / "data" / "retag.json"
GAP = 25  # 한꺼번에 몰아 달면 스팸으로 잡힐 수 있어 간격을 둔다


def medias():
    out, after = [], None
    while True:
        p = {"fields": "id,caption,media_type,timestamp", "limit": 50}
        if after:
            p["after"] = after
        r = g.api("GET", f"{os.environ[g.UID_ENV]}/media", **p)
        out += r.get("data", [])
        after = r.get("paging", {}).get("cursors", {}).get("after") if r.get("paging", {}).get("next") else None
        if not after:
            return out


def tag_comments(mid):
    r = g.api("GET", f"{mid}/comments", fields="id,text,username,hidden", limit=50)
    return [c for c in r.get("data", []) if c.get("text", "").lstrip().startswith("#")]


def main(mode):
    if mode == "list":
        ms = medias()
        print("게시물", len(ms))
        for m in ms:
            cs = tag_comments(m["id"])
            head = " / ".join(x for x in (m.get("caption") or "").split("\n") if x.strip())[:230]
            print(json.dumps({"id": m["id"], "t": m["media_type"], "at": m["timestamp"][:10], "cap": head,
                              "tags": [c["text"] for c in cs]}, ensure_ascii=False))
        return
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    ok = fail = hidden = 0
    for mid, tags in plan.items():
        try:
            old = tag_comments(mid)
            if any(c["text"].strip() == tags.strip() for c in old):
                print(mid, "이미 반영됨")
                continue
            g.api("POST", f"{mid}/comments", message=tags)
            ok += 1
            for c in old:
                try:
                    g.api("POST", c["id"], hide="true")
                    hidden += 1
                except Exception as e:
                    print(mid, "옛 댓글 숨김 실패", str(e)[:200])
            print(mid, "완료", tags)
        except Exception as e:
            fail += 1
            print(mid, "실패", str(e)[:300])
        time.sleep(GAP)
    print(f"새 댓글 {ok}, 실패 {fail}, 옛 댓글 숨김 {hidden}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1] if len(sys.argv) > 1 else "list")
