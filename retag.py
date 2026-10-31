"""기존 게시물에 새 해시태그 댓글 달기 (1회성). data/retag.json = {media: "태그들"}.
API 는 계정 본인 댓글을 목록으로 주지 않아 옛 댓글은 손대지 못한다."""
import json
import sys
import time
from pathlib import Path

import ig_post as g

GAP = 25  # 한꺼번에 몰아 달면 스팸으로 잡힐 수 있어 간격을 둔다


def main():
    plan = json.loads((Path(__file__).parent / "data" / "retag.json").read_text(encoding="utf-8"))
    count = lambda m: g.api("GET", m, fields="comments_count")["comments_count"]
    for mid, tags in plan.items():
        n = count(mid)
        if n >= 2:
            continue
        print(mid, "재시도, 현재 댓글", n)
        try:
            print(mid, "완료", g.api("POST", f"{mid}/comments", message=tags).get("id"))
        except Exception as e:
            print(mid, "실패", str(e)[:200])
        time.sleep(60)
    time.sleep(5)
    print("최종 댓글 수", {m: count(m) for m in plan})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
