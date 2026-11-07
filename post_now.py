"""대기열의 특정 릴스를 지금 바로 게시 (사용자가 '지금 올려'라고 지시했을 때만).

post-now.yml 에서 id 를 받아 실행한다. 하루 1건 제한을 건너뛰므로 자동 경로에서는 쓰지 않는다.
영상은 jsDelivr(저장소 공개 파일) 주소로 넘긴다 — 사이트 배포를 기다리지 않는다.
  python post_now.py <queue id>
"""
import os, subprocess, sys
import ig_post as g
from zelda_watch import publish, push


def main(rid):
    q = g.load(g.QUEUE, [])
    item = next((x for x in q if x["id"] == rid), None)
    if not item or not item.get("prebuilt"):
        raise SystemExit(f"대기열에 미리 만든 릴스가 없음: {rid}")
    if rid in g.load(g.POSTED, {}):
        raise SystemExit(f"이미 게시됨: {rid}")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    publish(rid, item["caption"], item["prebuilt"], sha)
    g.save(g.QUEUE, [x for x in g.load(g.QUEUE, []) if x["id"] != rid])
    push(f"인스타 즉시 게시: {rid}")


if __name__ == "__main__":
    main(sys.argv[1])
