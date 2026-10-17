"""인스타그램 자동 게시 (Instagram API with Instagram Login).

  python ig_post.py prepare   # 올릴 기사 고르고 캐러셀 이미지를 site/ig/ 에 생성 (사이트 배포 전에)
  python ig_post.py publish   # 배포된 이미지 주소로 캐러셀 게시 (사이트 배포 후에)
  python ig_post.py check     # 토큰·계정 확인 (data/state.json 에 기록)

환경변수: IG_ACCESS_TOKEN, IG_USER_ID (GitHub Secrets)
          IG_ENABLED=1 일 때만 실제 게시 (기본은 준비·확인만)
          IG_PER_RUN(기본 1), IG_DAILY(기본 4)
"""
import hashlib, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
DATA, SITE = ROOT / "data", ROOT / "site"
API = "https://graph.instagram.com/v23.0"
QUEUE, POSTED, TOKEN_FILE = DATA / "ig_queue.json", DATA / "ig_posted.json", DATA / "ig_token.enc"
HASHTAG = {"신작": "신작게임", "업데이트": "게임업데이트", "업계": "게임업계", "하드웨어": "게임기",
           "할인·이벤트": "게임할인", "e스포츠": "e스포츠"}


def load(p, d):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else d


def save(p, o):
    p.write_text(json.dumps(o, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 토큰 (60일 → 주기적으로 연장, 바뀐 토큰은 암호화해 저장소에 보관) ----------
def _stream(key, n):
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(key + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:n]


def _crypt(data, key):
    return bytes(a ^ b for a, b in zip(data, _stream(key, len(data))))


def token():
    base = os.environ.get("IG_ACCESS_TOKEN", "")
    key = hashlib.sha256(("alttab-ig:" + base).encode()).digest()  # 처음 넣은 Secret 이 열쇠
    t = load(TOKEN_FILE, None)
    if t and t.get("for") == hashlib.sha256(base.encode()).hexdigest()[:12]:
        try:
            return _crypt(bytes.fromhex(t["v"]), key).decode(), t
        except Exception:
            pass
    return base, None


def save_token(new):
    base = os.environ.get("IG_ACCESS_TOKEN", "")
    key = hashlib.sha256(("alttab-ig:" + base).encode()).digest()
    save(TOKEN_FILE, {"for": hashlib.sha256(base.encode()).hexdigest()[:12],
                      "v": _crypt(new.encode(), key).hex(), "refreshed": time.time()})


def api(method, path, **params):
    tok, _ = token()
    params["access_token"] = tok
    url = f"{API}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=60).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read()[:300].decode('utf-8', 'ignore')}")


def refresh_if_needed():
    """마지막 연장 후 7일 지났으면 연장 (토큰은 최소 24시간 지나야 연장 가능)."""
    _, meta = token()
    if meta and time.time() - meta.get("refreshed", 0) < 7 * 86400:
        return
    tok, _ = token()
    try:
        r = json.loads(urllib.request.urlopen(
            "https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token="
            + urllib.parse.quote(tok), timeout=30).read())
        save_token(r["access_token"])
        print(f"토큰 연장 완료 (남은 기간 {r.get('expires_in', 0) // 86400}일)")
    except Exception as e:
        print(f"  ! 토큰 연장 실패: {e}")


# ---------- 준비: 기사 고르기 + 이미지 ----------
def pick(arts, posted, n, queued_ids=(), cap=True):
    today = datetime.now().strftime("%Y-%m-%d")
    done_today = sum(1 for p in posted.values() if p.get("date") == today and p.get("kind", "news") == "news")
    room = min(n, int(os.environ.get("IG_DAILY", 4)) - done_today) if cap else n
    if room <= 0:
        return []
    cand = [a for a in arts if a.get("status", "published") == "published" and a.get("img")
            and a["id"] not in posted and a.get("trust") != "루머"
            and time.time() - a.get("written", a["ts"]) < 24 * 3600 and len(a.get("body", [])) >= 2]
    # 화제성: 출처 수 > 공식 발표 > 최신
    cand.sort(key=lambda a: (-len(a.get("sources", [])), a.get("trust") != "공식", -a["ts"]))
    # 이틀 안에 올렸거나 대기 중인 게임과 겹치면 건너뜀 (같은 소식을 매체별로 따로 쓴 기사 중복 방지)
    byid = {a["id"]: a for a in arts}
    recent = [byid[i] for i, p in posted.items() if i in byid and time.time() - p.get("ts", 0) < 2 * 86400]
    recent += [byid[i] for i in queued_ids if i in byid]
    used = {g.lower() for a in recent for g in (a.get("games_en") or [])}
    out = []
    for a in cand:
        gs = {g.lower() for g in (a.get("games_en") or [])}
        if gs & used:
            continue
        out.append(a)
        used |= gs
        if len(out) >= room:
            break
    return out


# ---------- 할인 게시물: 주간 최저가 · 대형 세일 특집 · 오늘의 딜 ----------
MAJOR_SALE = [("summer", "여름 세일"), ("autumn", "가을 세일"), ("fall", "가을 세일"), ("winter", "겨울 세일"),
              ("spring", "봄 세일"), ("lunar", "설날 세일"), ("halloween", "핼러윈 세일")]


def detect_major_sale():
    """Steam 메인 스포트라이트에서 대형 시즌 세일을 찾는다 → (키, 한국어 이름) 또는 None."""
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            "https://store.steampowered.com/api/featuredcategories?cc=kr&l=english",
            headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read())
    except Exception:
        return None
    for v in d.values():
        if not isinstance(v, dict) or v.get("name") != "Spotlights":
            continue
        for it in v.get("items", []):
            text = f"{it.get('name', '')} {it.get('url', '')}".lower()
            if "/sale/" not in text and "sale" not in text:
                continue
            for k, ko in MAJOR_SALE:
                if k in text:
                    return f"{k}-{datetime.now().year}", ko
    return None


def save_slides(key, slides):
    (SITE / "ig" / key).mkdir(parents=True, exist_ok=True)
    names = []
    for i, im in enumerate(slides[:10], 1):
        im.convert("RGB").save(SITE / "ig" / key / f"{i:02d}.jpg", quality=88)
        names.append(f"{i:02d}.jpg")
    return names


def featured_recent(posted, days=7):
    return {a for p in posted.values() if time.time() - p.get("ts", 0) < days * 86400 for a in p.get("appids", [])}


def deal_caption(deals, head):
    lines = [head, ""] + [f"▪ {x['name'].replace('™', '')} -{x['discount']}% ₩{x['final']:,}" for x in deals]
    lines += ["", "할인은 게임별 마감일까지 진행된다. 가격은 게시 시점 Steam 한국 가격 기준이다.", "",
              "#스팀할인 #스팀세일 #게임할인 #역대최저가 #PC게임 #게임추천 #알트탭"]
    return "\n".join(lines)


def rebuild_deal(q):
    import deals_carousel as dc
    pool = {x["appid"]: x for x in dc.pick_deals(200)}
    deals = [pool[a] for a in q.get("appids", []) if a in pool]
    if not deals:
        return
    if q["kind"] == "story":
        save_slides(q["id"], [dc.story(deals[0])])
    else:
        total = len(deals) + 2
        cover = q.get("cover") or {}
        slides = [dc.slide_cover(deals, total, **cover)] + [dc.slide_deal(x, i + 2, total) for i, x in enumerate(deals)]             + [dc.slide_end(deals, total)]
        save_slides(q["id"], slides)


def prepare_deals(queue, posted):
    if not (DATA / "deals.json").exists():
        return
    import deals_carousel as dc
    now = datetime.now()
    keys = set(posted) | {q["id"] for q in queue}
    skip = featured_recent(posted)
    # ① 대형 세일 특집 (세일당 한 번)
    sale = detect_major_sale()
    if sale and f"sale-{sale[0]}" not in keys:
        cover = dict(line1=f"스팀 {sale[1]}", line2="지금 살 게임", sub="역대 최저가 · 한국어 지원 위주로 골랐습니다")
        slides, deals = dc.build(10, **cover)
        key = f"sale-{sale[0]}"
        queue.append({"id": key, "kind": "deal", "cover": cover, "files": save_slides(key, slides), "appids": [x["appid"] for x in deals],
                      "caption": deal_caption(deals, f"🔥 스팀 {sale[1]} 시작! 지금 사야 할 게임 {len(deals)}")})
        print(f"인스타 준비: 대형 세일 특집 {sale[1]}")
    # ② 주간 역대 최저가 (금요일 17시 이후, 주 1회)
    wk = f"deals-{now.isocalendar()[0]}-{now.isocalendar()[1]:02d}"
    force = os.environ.get("IG_FORCE") == "1"  # 수동 즉시 게시 (요일·시간 조건 무시)
    if (force or (now.weekday() == 4 and now.hour >= 17)) and wk not in keys:
        slides, deals = dc.build(7, skip=skip)
        queue.append({"id": wk, "kind": "deal", "files": save_slides(wk, slides), "appids": [x["appid"] for x in deals],
                      "caption": deal_caption(deals, f"💸 이번 주 Steam 역대 최저가 {len(deals)}")})
        print("인스타 준비: 주간 할인")
    # ③ 오늘의 딜 스토리 (매일 9시 이후 하루 한 번, 최근 7일 소개한 게임 제외)
    sk = f"story-{now.strftime('%Y-%m-%d')}"
    if (force or now.hour >= 9) and sk not in keys:
        cand = [x for x in dc.pick_deals(30) if x["appid"] not in skip]
        if cand:
            x = cand[0]
            queue.append({"id": sk, "kind": "story", "files": save_slides(sk, [dc.story(x)]), "appids": [x["appid"]]})
            print(f"인스타 준비: 오늘의 딜 {x['name']}")


def prepare():
    from carousel import build_carousel
    arts = load(DATA / "articles.json", [])
    posted = load(POSTED, {})
    queue = [q for q in load(QUEUE, []) if q["id"] not in posted]
    queued = {q["id"] for q in queue}
    today = datetime.now().strftime("%Y-%m-%d")
    reels_today = sum(1 for p in posted.values() if p.get("kind") == "reel" and p.get("date") == today)         + sum(1 for q in queue if q.get("kind") == "reel")
    # 릴스는 캐러셀 한도와 별도로 하루 IG_REELS_DAILY 건
    want_reel = reels_today < int(os.environ.get("IG_REELS_DAILY", 2))
    cands = pick([a for a in arts if a["id"] not in queued], posted, 1, queued, cap=False) if want_reel else []
    cands += [a for a in pick([a for a in arts if a["id"] not in queued], posted, int(os.environ.get("IG_PER_RUN", 1)), queued)
              if not cands or a["id"] != cands[0]["id"]]
    for a in cands:
        if want_reel and reels_today < int(os.environ.get("IG_REELS_DAILY", 2)):
            try:  # 릴스: 도달이 가장 넓은 형식 → 하루 상위 기사 몇 건은 영상으로
                from reels import render
                (SITE / "ig" / a["id"]).mkdir(parents=True, exist_ok=True)
                render(a, SITE / "ig" / a["id"] / "reel.mp4")
                queue.append({"id": a["id"], "kind": "reel", "files": ["reel.mp4"], "caption": caption(a)})
                reels_today += 1
                print(f"인스타 준비(릴스): {a['title']}")
                continue
            except Exception as e:
                print(f"  ! 릴스 생성 실패, 캐러셀로: {e}")
        slides = build_carousel(a)[:10]  # 캐러셀 최대 10장
        names = []
        for i, im in enumerate(slides, 1):
            name = f"{i:02d}.jpg"
            (SITE / "ig" / a["id"]).mkdir(parents=True, exist_ok=True)
            im.convert("RGB").save(SITE / "ig" / a["id"] / name, quality=88)
            names.append(name)
        queue.append({"id": a["id"], "files": names, "caption": caption(a)})
        print(f"인스타 준비: {a['title']} ({len(names)}장)")
    try:
        prepare_deals(queue, posted)
    except Exception as e:
        import traceback
        print(f"  ! 할인 게시물 준비 실패: {e}")
        st = load(DATA / "state.json", {})
        st["ig_deals_err"] = traceback.format_exc()[-600:]
        save(DATA / "state.json", st)
    # 이미 대기 중인 기사의 이미지도 다시 만들어 배포본에 포함 (사이트는 매번 새로 빌드되므로)
    for q in queue:
        if q.get("kind") == "reel" and not (SITE / "ig" / q["id"] / "reel.mp4").exists():
            a = next((x for x in arts if x["id"] == q["id"]), None)
            if a:
                from reels import render
                (SITE / "ig" / q["id"]).mkdir(parents=True, exist_ok=True)
                render(a, SITE / "ig" / q["id"] / "reel.mp4")
            continue
        if q.get("kind") in ("deal", "story") and not (SITE / "ig" / q["id"]).exists():
            try:
                rebuild_deal(q)
            except Exception as e:
                print(f"  ! 할인 이미지 재생성 실패 {q['id']}: {e}")
    for q in queue:
        if q.get("kind", "news") == "news" and not (SITE / "ig" / q["id"]).exists():
            a = next((x for x in arts if x["id"] == q["id"]), None)
            if a:
                for i, im in enumerate(build_carousel(a)[:10], 1):
                    (SITE / "ig" / a["id"]).mkdir(parents=True, exist_ok=True)
                    im.convert("RGB").save(SITE / "ig" / a["id"] / f"{i:02d}.jpg", quality=88)
    save(QUEUE, queue)


ENDINGS = [("됨", "됐다"), ("함", "했다"), ("임", "이다"), ("짐", "졌다"), ("음", "었다"), ("냄", "냈다"), ("둠", "뒀다"),
           ("줌", "줬다"), ("봄", "봤다"), ("옴", "왔다"), ("감", "갔다"), ("섬", "섰다")]


# 대상이 주어인 경우가 많은 명사는 '~됐다', 상태는 '~한다'
NOUN_END = {"출시": "됐다", "공개": "됐다", "탑재": "됐다", "추가": "됐다", "적용": "됐다", "연기": "됐다", "확정": "됐다",
            "인하": "됐다", "인상": "됐다", "종료": "됐다", "개시": "됐다", "도입": "됐다", "유출": "됐다", "지원": "한다", "제공": "된다"}
NOUN_IS = ("예정", "계획", "방침", "전망", "상황", "상태", "중", "가능성", "목표", "방향", "수준", "규모", "전략")


def to_news_tone(t):
    """'~시작됨', '~출시' 같은 개조식 끝맺음을 기사체('~시작됐다.', '~출시했다.')로."""
    t = t.strip().rstrip(".")
    if t.endswith("다"):
        return t + "."
    for a_, b_ in ENDINGS:
        if t.endswith(a_):
            return t[: -len(a_)] + b_ + "."
    if t.endswith(NOUN_IS):
        return t + "이다."
    for n_, e_ in NOUN_END.items():
        if t.endswith(n_):
            return t + e_ + "."
    if t and "가" <= t[-1] <= "힣":
        return t + "했다."
    return t + "."


# 이미지 속 문장은 개조식: '출시됐다.' → '출시', '이어졌다.' → '이어짐'
CARD_CUT = ["하였습니다", "되었습니다", "했습니다", "됐습니다", "합니다", "됩니다", "입니다", "하였다", "되었다",
            "했다", "됐다", "한다", "된다", "이다", "됨", "함", "임"]
CARD_MAP = [("졌다", "짐"), ("났다", "남"), ("왔다", "옴"), ("섰다", "섬"), ("냈다", "냄"), ("줬다", "줌"), ("봤다", "봄"),
            ("갔다", "감"), ("놨다", "놓음"), ("뒀다", "둠"), ("었다", "음"), ("았다", "음"), ("였다", "임"), ("있다", "있음"), ("없다", "없음"),
            ("는다", "음"), ("렸다", "림"), ("켰다", "킴"), ("췄다", "춤"), ("웠다", "움"), ("겼다", "김"), ("쳤다", "침")]


def to_card_tone(t):
    t = t.strip().rstrip(".").strip()
    for c in CARD_CUT:
        if t.endswith(c) and len(t) > len(c):
            return t[: -len(c)].rstrip()
    for a_, b_ in CARD_MAP:
        if t.endswith(a_):
            return t[: -len(a_)] + b_
    return t


def caption(a):
    tags = ["게임뉴스", "게임소식", "알트탭"]
    tags += [HASHTAG[a["category"]]] if a.get("category") in HASHTAG else []
    tags += [p.replace(" ", "") for p in a.get("platforms", [])]
    tags += ["".join(ch for ch in g if ch.isalnum()) for g in (a.get("games") or [])[:2]]
    srcs = [s_["name"] for s_ in a.get("sources", [])]
    lines = [a["title"], ""] + [to_news_tone(t) for t in a.get("tldr", [])[:3]]
    if srcs:
        lines += ["", "출처: " + srcs[0] + (f" 외 {len(srcs) - 1}곳" if len(srcs) > 1 else "")]
    lines += ["", " ".join("#" + t for t in dict.fromkeys(t for t in tags if t))]
    return "\n".join(lines)[:2100]


# ---------- 게시 ----------
def site_url():
    return os.environ.get("SITE_URL", "https://khb950404-creator.github.io/gamewebzine/").rstrip("/") + "/"


def wait_url(url, tries=24):
    """배포 직후엔 CDN 에 아직 없을 수 있다 → 이미지가 실제로 열릴 때까지 대기 (최대 약 4분)."""
    for _ in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url + f"?v={int(time.time())}", method="HEAD"), timeout=15)
            if r.status == 200 and any(k in r.headers.get("Content-Type", "") for k in ("image", "video")):
                return True
        except Exception:
            pass
        time.sleep(10)
    return False


def wait_ready(cid, tries=30):
    for _ in range(tries):
        st = api("GET", cid, fields="status_code").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"컨테이너 {cid} 상태 {st}")
        time.sleep(4)
    raise RuntimeError("컨테이너 준비 시간 초과")


def publish():
    if os.environ.get("IG_ENABLED") != "1":
        print("IG_ENABLED 가 1이 아니라 게시하지 않음 (준비만)")
        return
    refresh_if_needed()
    uid = os.environ["IG_USER_ID"]
    queue, posted = load(QUEUE, []), load(POSTED, {})
    for q in list(queue):
        if q["id"] in posted:
            queue.remove(q)
            continue
        try:
            kind = q.get("kind", "news")
            for f in q["files"]:
                if not wait_url(f"{site_url()}ig/{q['id']}/{f}"):
                    raise RuntimeError(f"이미지가 사이트에 없음: ig/{q['id']}/{f}")
            if kind == "reel":
                c = api("POST", f"{uid}/media", media_type="REELS", video_url=f"{site_url()}ig/{q['id']}/reel.mp4",
                        caption=q["caption"], share_to_feed="true")["id"]
                wait_ready(c, tries=60)  # 영상 처리는 1~3분 걸림
                mid = api("POST", f"{uid}/media_publish", creation_id=c)["id"]
                posted[q["id"]] = {"media": mid, "kind": kind, "date": datetime.now().strftime("%Y-%m-%d"), "ts": time.time()}
                queue.remove(q)
                print(f"인스타 릴스 게시: {q['id']}")
                continue
            if kind == "story":
                c = api("POST", f"{uid}/media", image_url=f"{site_url()}ig/{q['id']}/{q['files'][0]}", media_type="STORIES")["id"]
                wait_ready(c)
                mid = api("POST", f"{uid}/media_publish", creation_id=c)["id"]
                posted[q["id"]] = {"media": mid, "kind": kind, "appids": q.get("appids", []),
                                   "date": datetime.now().strftime("%Y-%m-%d"), "ts": time.time()}
                queue.remove(q)
                print(f"인스타 스토리 게시: {q['id']}")
                continue
            children = []
            for f in q["files"]:
                url = f"{site_url()}ig/{q['id']}/{f}"
                c = api("POST", f"{uid}/media", image_url=url, is_carousel_item="true")["id"]
                wait_ready(c)
                children.append(c)
            parent = api("POST", f"{uid}/media", media_type="CAROUSEL", children=",".join(children), caption=q["caption"])["id"]
            wait_ready(parent)
            mid = api("POST", f"{uid}/media_publish", creation_id=parent)["id"]
            posted[q["id"]] = {"media": mid, "kind": q.get("kind", "news"), "appids": q.get("appids", []),
                               "date": datetime.now().strftime("%Y-%m-%d"), "ts": time.time()}
            queue.remove(q)
            print(f"인스타 게시 완료: {q['id']} → {mid}")
        except Exception as e:
            print(f"  ! 인스타 게시 실패 {q['id']}: {e}")
            q["fail"] = q.get("fail", 0) + 1
            q["err"] = str(e)[:300]
            if q["fail"] >= 3:  # 세 번 실패하면 포기
                queue.remove(q)
        time.sleep(5)
    save(QUEUE, queue)
    save(POSTED, posted)


def check():
    state = load(DATA / "state.json", {})
    try:
        me = api("GET", "me", fields="user_id,username,account_type,media_count")
        state["ig"] = f"ok @{me.get('username')} ({me.get('account_type')}, 게시물 {me.get('media_count')})"
        state["ig_user_id_match"] = str(me.get("user_id")) == os.environ.get("IG_USER_ID", "")
    except Exception as e:
        state["ig"] = f"오류: {e}"
    save(DATA / "state.json", state)
    print("인스타:", state["ig"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    {"prepare": prepare, "publish": publish, "check": check}[sys.argv[1]]()
