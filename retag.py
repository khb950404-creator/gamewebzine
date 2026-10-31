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
    ok = streak = 0
    for mid, tags in plan.items():
        try:
            print(mid, "완료", g.api("POST", f"{mid}/comments", message=tags).get("id"), tags)
            ok += 1
            streak = 0
        except Exception as e:
            print(mid, "실패", str(e)[:300])
            streak += 1
            if streak >= 3:
                print("연속 실패로 중단")
                break
        time.sleep(GAP)
    print(f"새 댓글 {ok} / {len(plan)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
