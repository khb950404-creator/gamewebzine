"""시간의 오카리나 리메이크(2026-11-05 출시) 리뷰 엠바고 감지기.

zelda-watch.yml 이 5분마다 메타크리틱 점수를 확인하고, 점수가 뜨면 바로 릴스를 만들어 올린다.
- 11월 4일 이전에 뜨고 그날 피드를 아직 안 올렸으면: '점수 나왔다' 릴스를 즉시 게시 (그날의 1건)
- 그날 피드를 이미 올렸으면: 11월 5일 출시일 릴스에 점수를 넣어 두기만 한다 (12시 정기 게시)
- 11월 5일에 뜨면: 출시일 릴스에 점수를 넣어 즉시 게시
점수는 메타크리틱 화면에 표시된 값만 쓴다 (리뷰 REVIEW_MIN 개 이상일 때부터).
영상은 jsDelivr(저장소 공개 파일) 주소로 인스타에 넘긴다 — 사이트 배포(10분 이상)를 기다리지 않는다.

  python zelda_watch.py            # 확인하고 뜨면 게시
  python zelda_watch.py dry        # 확인만 (게시·커밋 없음)
"""
import json, os, shutil, subprocess, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATE = DATA / "zelda_watch.json"
SLUG = "the-legend-of-zelda-ocarina-of-time-2026"
PAGE = f"https://www.metacritic.com/game/{SLUG}/"
API = f"https://backend.metacritic.com/composer/metacritic/pages/games/{SLUG}/web?filter=all&sort=date"
REVIEW_MIN = int(os.environ.get("REVIEW_MIN", 10))
LAST_DAY = "2026-11-05"
D = "media/features/zelda-oot"
LAUNCH_ID, REVIEW_ID = "zelda-oot-launch-reel", "zelda-oot-review-reel"
REPO = os.environ.get("GITHUB_REPOSITORY", "khb950404-creator/gamewebzine")


def score():
    req = urllib.request.Request(API, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=30).read())
    for c in d["components"]:
        if (c.get("meta") or {}).get("componentName") == "critic-score-summary":
            x = c.get("data", {}).get("item") or {}
            return x.get("score"), x.get("reviewCount") or 0, x.get("positiveCount") or 0
    return None, 0, 0


def items_and_caption(mode, sc, n, pos, at):
    d_ = lambda k: f"{D}/{k}.jpg"
    # 배경은 40주년 Direct 공식 영상(한국닌텐도 fdSsHJC_5-4)의 컷 사이 구간만 잘라 둔 조각: 장면 전환이 영상 컷과 맞는다
    v_ = lambda k: f"{D}/clips/{k}.mp4"
    roll = {"to": sc, "unit": "점", "max": 100, "ref": [99, "1998 원작"]}  # 점수가 0에서 굴러 오르고 원작 눈금과 나란히
    rev = {"img": d_("ganon"), "appid": v_("dawn"), "chip": "메타크리틱", "name": "평론가 점수 공개", "roll": roll, "secs": 3.6,
           "lines": [f"리뷰  {n}개 중 호평 {pos}개", f"기준  {at} 집계, 계속 바뀜"]}
    keep = {"img": d_("ganon"), "appid": v_("goron"), "chip": "그대로인 것", "name": "뼈대는 1998년 그대로", "lines": ["구조  선형 모험", "로딩  지역 간 로딩 유지"]}
    price = "가격  다운로드 79,800원, 패키지 88,000원"
    src = "\n\n출처: 메타크리틱, 한국닌텐도\n영상: 한국닌텐도 공식 영상"
    tags = "\n\n#젤다의전설 #시간의오카리나 #닌텐도스위치2 #젤다 #알트탭"
    if mode == "review":
        hook = "역대 최고점 게임의 리메이크, 점수 나왔다"
        items = [
            rev,
            {"img": d_("temple"), "appid": v_("lake"), "chip": "비교", "name": "원작은 99점", "lines": ["1998년판  99점, 지금도 최고점", "2011년 3DS판  94점"]},
            keep,
            {"img": d_("ocarina"), "appid": v_("ride"), "chip": "11월 5일", "name": "스위치 2 전용 출시", "lines": [price, "특전  다운로드 11월 8일 23:59까지"]},
        ]
        cap = (f"역대 최고점 게임의 리메이크, 점수가 나왔습니다.\n\n"
               f"'젤다의 전설 시간의 오카리나' 스위치 2 리메이크 리뷰가 공개됐습니다. 메타크리틱 평론가 점수는 {at} 기준 {sc}점(리뷰 {n}개 중 호평 {pos}개)입니다. "
               f"리뷰가 더 붙으면서 점수는 바뀔 수 있습니다.\n\n"
               f"1998년 원작은 99점, 2011년 3DS판은 94점이었습니다. 선형 구조와 지역 간 로딩은 1998년 그대로입니다.\n\n"
               f"11월 5일 스위치 2 전용 출시, 다운로드 79,800원, 패키지 88,000원입니다." + src + tags)
        return hook, items, cap
    hook = "역대 최고 점수의 게임, 오늘 다시 나왔다"
    items = [
        {"img": d_("boy"), "appid": v_("saria"), "chip": "오늘 출시", "name": "젤다의 전설 시간의 오카리나", "lines": ["기종  스위치 2 전용, 12세 이용가", price]},
        {"img": d_("temple"), "appid": v_("lake"), "chip": "1998", "name": "역대 최고 점수의 원작", "lines": ["메타크리틱  99점, 지금도 최고점", "파미통  사상 첫 40점 만점"]},
        rev,
        {"img": d_("ocarina"), "appid": v_("ride"), "chip": "특전", "name": "다운로드 특전은 8일까지", "lines": ["기한  11월 8일 23:59", "내용  디지털 월페이퍼"]},
    ]
    cap = (f"역대 최고 점수의 게임이 오늘 다시 나왔습니다.\n\n"
           f"'젤다의 전설 시간의 오카리나' 스위치 2 리메이크가 11월 5일 출시됐습니다. 다운로드 79,800원, 패키지 88,000원, 12세 이용가입니다.\n\n"
           f"리메이크의 메타크리틱 평론가 점수는 {at} 기준 {sc}점(리뷰 {n}개 중 호평 {pos}개)입니다. 1998년 원작은 99점으로 지금도 최고점 기록을 갖고 있습니다.\n\n"
           f"다운로드 구입 특전(디지털 월페이퍼)은 11월 8일 23:59까지입니다." + src + tags)
    return hook, items, cap


def build(mode, sc, n, pos, at):
    sys.path.insert(0, str(ROOT))
    from reels import render_tips
    hook, items, cap = items_and_caption(mode, sc, n, pos, at)
    rid = REVIEW_ID if mode == "review" else LAUNCH_ID
    tmp = ROOT / "out" / rid
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "reel.mp4").unlink(missing_ok=True)
    render_tips(items, tmp / "reel.mp4", hook, key=rid)
    out = ROOT / D / ("review" if mode == "review" else "launch") / "reel"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(tmp / "reel.mp4", out / "reel.mp4")
    return rid, hook, cap, out.relative_to(ROOT).as_posix()


def git(*a):
    subprocess.run(["git", *a], check=True, cwd=ROOT)


def push(msg):
    git("add", "data", "media")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode:
        git("commit", "-m", msg)
    for _ in range(4):
        if subprocess.run(["git", "pull", "--rebase"], cwd=ROOT).returncode == 0 and \
                subprocess.run(["git", "push"], cwd=ROOT).returncode == 0:
            return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        time.sleep(5)
    raise RuntimeError("push 실패")


def publish(rid, cap, path, sha):
    import ig_post as g
    g.refresh_if_needed()
    uid = os.environ[g.UID_ENV]
    url = f"https://cdn.jsdelivr.net/gh/{REPO}@{sha}/{path}/reel.mp4"
    if not g.wait_url(url, tries=12):
        raise RuntimeError(f"영상 주소 준비 안 됨: {url}")
    from bgm import credit
    body, tg = g.split_tags(cap)
    full = body + ("\n" + credit(rid) if credit(rid) else "") + ("\n\n" + tg if tg else "")
    c = g.api("POST", f"{uid}/media", media_type="REELS", video_url=url, caption=g.clean_caption(full),
              share_to_feed="true", thumb_offset="800")["id"]
    g.wait_ready(c, tries=30)
    mid = g.api("POST", f"{uid}/media_publish", creation_id=c)["id"]
    g.comment_tags(mid, cap)
    posted = g.load(g.POSTED, {})
    posted[rid] = {"media": mid, "kind": "reel", "date": datetime.now().strftime("%Y-%m-%d"), "ts": time.time(), "forced": False}
    g.save(g.POSTED, posted)
    print(f"인스타 릴스 게시: {rid} → {mid}")
    return mid


def main(dry=False):
    today = datetime.now().strftime("%Y-%m-%d")
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if today > LAST_DAY or state.get("done"):
        print("감지 종료됨", state.get("done", ""))
        return
    sc, n, pos = score()
    print(f"메타크리틱: 점수 {sc}, 리뷰 {n}개 (호평 {pos})")
    if sc is None or n < REVIEW_MIN or dry:
        return
    import ig_post as g
    posted = g.load(g.POSTED, {})
    fed = any(v.get("date") == today and v.get("kind") != "story" for v in posted.values())
    at = datetime.now().strftime("%m월 %d일 %H:%M").lstrip("0").replace("월 0", "월 ")
    force = os.environ.get("ZELDA_FORCE_REVIEW") == "1"  # 수동 실행: 그날 피드가 차 있어도 리뷰 릴스를 바로 (2026-11-04 지시)
    if today < LAST_DAY and (not fed or force):
        mode, now = "review", True
    elif today < LAST_DAY:
        mode, now = "launch", False   # 오늘 몫은 이미 올렸다 → 출시일 릴스에 점수만 넣어 둔다
    else:
        mode, now = "launch", LAUNCH_ID not in posted and not fed
    rid, hook, cap, path = build(mode, sc, n, pos, at)
    q = g.load(g.QUEUE, [])
    for x in q:
        if x["id"] == LAUNCH_ID and mode == "launch":
            x.update(caption=cap, hook=hook, prebuilt=path)
    state = {"seen": time.time(), "score": sc, "reviews": n, "positive": pos, "mode": mode, "post_now": now}
    if mode == "launch" and not now:
        state["done"] = time.time()  # 출시일 릴스는 정기 게시가 올린다
    g.save(g.QUEUE, [x for x in q if not (now and x["id"] == rid)])
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    sha = push(f"시간의 오카리나 리뷰 공개 감지: 메타크리틱 {sc}점 ({n}개)")
    if now:
        publish(rid, cap, path, sha)
        state["done"] = time.time()
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        push("시간의 오카리나 리뷰 릴스 게시 기록")


if __name__ == "__main__":
    main(dry="dry" in sys.argv[1:])
