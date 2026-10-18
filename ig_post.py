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
PROFILE = os.environ.get("IG_PROFILE", "ko")  # ko | en
SUF = "" if PROFILE == "ko" else "_" + PROFILE
QUEUE, POSTED, TOKEN_FILE = DATA / f"ig_queue{SUF}.json", DATA / f"ig_posted{SUF}.json", DATA / f"ig_token{SUF}.enc"
TOKEN_ENV = "IG_ACCESS_TOKEN" if PROFILE == "ko" else "IG_EN_ACCESS_TOKEN"
UID_ENV = "IG_USER_ID" if PROFILE == "ko" else "IG_EN_USER_ID"
ENABLED_ENV = "IG_ENABLED" if PROFILE == "ko" else "IG_EN_ENABLED"
HANDLE_P = "@webzinealttab" if PROFILE == "ko" else os.environ.get("IG_EN_HANDLE", "@alttab.en")
IGDIR = "ig" if PROFILE == "ko" else "ig_" + PROFILE
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
    base = os.environ.get(TOKEN_ENV, "")
    key = hashlib.sha256(("alttab-ig:" + base).encode()).digest()  # 처음 넣은 Secret 이 열쇠
    t = load(TOKEN_FILE, None)
    if t and t.get("for") == hashlib.sha256(base.encode()).hexdigest()[:12]:
        try:
            return _crypt(bytes.fromhex(t["v"]), key).decode(), t
        except Exception:
            pass
    return base, None


def save_token(new):
    base = os.environ.get(TOKEN_ENV, "")
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
# 이름만으로 클릭되는 대형 IP (정규화한 영문/한글 일부)
BIG_IP = ["gta", "grandtheftauto", "그랜드테프트오토", "callofduty", "콜오브듀티", "zelda", "젤다", "pokemon", "포켓몬",
          "mario", "마리오", "eldenring", "엘든링", "fromsoftware", "marvel", "마블", "starwars", "스타워즈", "battlefield",
          "배틀필드", "assassinscreed", "어쌔신크리드", "residentevil", "레지던트이블", "바이오하자드", "monsterhunter",
          "몬스터헌터", "finalfantasy", "파이널판타지", "cyberpunk", "사이버펑크", "witcher", "위쳐", "halo", "헤일로",
          "godofwar", "갓오브워", "spiderman", "스파이더맨", "minecraft", "마인크래프트", "fortnite", "포트나이트",
          "valorant", "발로란트", "leagueoflegends", "리그오브레전드", "overwatch", "오버워치", "diablo", "디아블로",
          "starcraft", "스타크래프트", "pubg", "배틀그라운드", "lostark", "로스트아크", "streetfighter", "스트리트파이터",
          "tekken", "철권", "silenthill", "사일런트힐", "metalgear", "메탈기어", "nintendo", "닌텐도", "switch2", "스위치2",
          "playstation", "플레이스테이션", "ps6", "xbox", "steamdeck", "스팀덱", "valve", "밸브", "halflife", "하프라이프"]
_TOP_PLAYED = None


def top_played():
    """Steam 동시접속 상위 100 appid (한 번만 조회)."""
    global _TOP_PLAYED
    if _TOP_PLAYED is None:
        try:
            r = json.loads(urllib.request.urlopen(
                "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/", timeout=20).read())
            _TOP_PLAYED = {x["appid"] for x in r["response"]["ranks"][:100]}
        except Exception:
            _TOP_PLAYED = set()
    return _TOP_PLAYED


def popularity(a):
    import re as _re
    text = _re.sub(r"[^0-9a-z가-힣]", "", " ".join([a["title"]] + (a.get("games_en") or []) + (a.get("games") or [])).lower())
    s_ = 3 * len(a.get("sources", []))
    s_ += 5 if a.get("appid") in top_played() else 0
    s_ += 5 if any(k in text for k in BIG_IP) else 0
    s_ += 2 if a.get("trust") == "공식" else 0
    s_ += 2 if a.get("category") == "신작" or "트레일러" in a["title"] or "공개" in a["title"] else 0
    s_ += 2 if a.get("video") else 0
    s_ -= (time.time() - a.get("written", a["ts"])) / 3600 * 0.3  # 오래될수록 감점
    return s_


def pick(arts, posted, n, queued_ids=(), cap=True, by_pop=False):
    today = datetime.now().strftime("%Y-%m-%d")
    done_today = sum(1 for p in posted.values() if p.get("date") == today and p.get("kind", "news") == "news")
    room = min(n, int(os.environ.get("IG_DAILY", 4)) - done_today) if cap else n
    if room <= 0:
        return []
    cand = [a for a in arts if a.get("status", "published") == "published" and a.get("img")
            and a["id"] not in posted and a.get("trust") != "루머"
            and time.time() - a.get("written", a["ts"]) < 24 * 3600 and len(a.get("body", [])) >= 2]
    # 화제성: 출처 수 > 공식 발표 > 최신
    if by_pop:  # 릴스: 가장 화제가 될 만한 순
        cand.sort(key=lambda a: -popularity(a))
    else:
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
    (SITE / IGDIR / key).mkdir(parents=True, exist_ok=True)
    names = []
    for i, im in enumerate(slides[:10], 1):
        im.convert("RGB").save(SITE / IGDIR / key / f"{i:02d}.jpg", quality=88)
        names.append(f"{i:02d}.jpg")
    return names


def featured_recent(posted, days=7):
    return {a for p in posted.values() if time.time() - p.get("ts", 0) < days * 86400 for a in p.get("appids", [])}


def deal_caption(deals, head):
    lines = [head, ""] + [f"▪ {x['name'].replace('™', '')} -{x['discount']}% ₩{x['final']:,}" for x in deals]
    lines += ["", "할인은 게임별 마감일까지 진행된다. 가격은 게시 시점 Steam 한국 가격 기준이다.", "",
              "이번에 장바구니에 담을 게임은?", "",
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
    elif q["kind"] == "reel":
        from reels import render_deals
        (SITE / IGDIR / q["id"]).mkdir(parents=True, exist_ok=True)
        render_deals(deals, SITE / IGDIR / q["id"] / "reel.mp4", q.get("hook") or "이번 주 역대 최저가", key=q["id"])
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
        # 같은 날 할인 릴스도 (도달용)
        from reels import render_deals
        rk = wk + "-reel"
        (SITE / IGDIR / rk).mkdir(parents=True, exist_ok=True)
        hook = f"이번 주 역대 최저가 {min(5, len(deals))}"
        render_deals(deals[:5], SITE / IGDIR / rk / "reel.mp4", hook, key=rk)
        queue.append({"id": rk, "kind": "reel", "files": ["reel.mp4"], "appids": [x["appid"] for x in deals[:5]],
                      "hook": hook, "caption": deal_caption(deals[:5], f"💸 {hook}")})
    # ④ 일요일 시리즈: 이번 주 게임 뉴스 TOP5 (일요일 18시 이후, 주 1회)
    tk = f"top-{now.isocalendar()[0]}-{now.isocalendar()[1]:02d}"
    if (force or (now.weekday() == 6 and now.hour >= 18)) and tk not in keys:
        from carousel import build_weekly_top
        arts = [a for a in load(DATA / "articles.json", []) if a.get("img") and a.get("trust") != "루머"
                and time.time() - a.get("written", a["ts"]) < 7 * 86400]
        top, used = [], set()
        for a in sorted(arts, key=lambda a: -popularity(a)):
            gs = {g.lower() for g in (a.get("games_en") or [])} or {a["id"]}
            if gs & used:
                continue
            top.append(a)
            used |= gs
            if len(top) == 5:
                break
        if len(top) >= 3:
            queue.append({"id": tk, "kind": "series", "files": save_slides(tk, build_weekly_top(top)),
                          "arts": [a["id"] for a in top],
                          "caption": "\n".join(["📰 이번 주 게임 뉴스 TOP" + str(len(top)), ""]
                                               + [f"{i}. {a['title']}" for i, a in enumerate(top, 1)]
                                               + ["", "가장 놀라운 소식은 몇 번인가요?", "",
                                                  "#게임뉴스 #주간게임뉴스 #게임소식 #알트탭"])})
            print("인스타 준비: 주간 TOP5")
    # ③ 오늘의 딜 스토리 (매일 9시 이후 하루 한 번, 최근 7일 소개한 게임 제외)
    sk = f"story-{now.strftime('%Y-%m-%d')}"
    if (force or now.hour >= 9) and sk not in keys:
        cand = [x for x in dc.pick_deals(30) if x["appid"] not in skip]
        if cand:
            x = cand[0]
            queue.append({"id": sk, "kind": "story", "files": save_slides(sk, [dc.story(x)]), "appids": [x["appid"]]})
            print(f"인스타 준비: 오늘의 딜 {x['name']}")


def ensure_hook(a, lang="ko"):
    """릴스 첫 화면 훅 문구가 없으면 AI 로 짧게 만든다 (가벼운 모델 1회)."""
    k = "hook" if lang == "ko" else "hook_en"
    if a.get(k):
        return
    try:
        import webzine
        webzine.load_env()
        if lang == "ko":
            q = ("다음 게임 기사로 인스타 릴스 첫 화면에 크게 띄울 훅 문구를 8~16자로 만들어라. "
                 '궁금증·숫자·반전을 살리고 과장·낚시는 금지. JSON: {"hook": "..."}\n제목: ' + a["title"])
        else:
            q = ('Write a 3-6 word Instagram Reel hook for this game news. No clickbait lies. JSON: {"hook": "..."}\n'
                 "Title: " + (a.get("title_en") or a["title"]))
        r = webzine.llm(q, prefer_lite=True) or {}
        if isinstance(r.get("hook"), str) and r["hook"].strip():
            a[k] = r["hook"].strip()
    except Exception as e:
        print(f"  ! 훅 생성 실패: {e}")


def regen_one(q, arts):
    from carousel import build_carousel
    kind = q.get("kind", "news")
    folder = SITE / IGDIR / q["id"]
    a = next((x for x in arts if x["id"] == q["id"]), None)
    if kind == "reel":
        if (folder / "reel.mp4").exists():
            return
        folder.mkdir(parents=True, exist_ok=True)
        if a:
            from reels import render
            a = {**a, "hook": a.get("hook") or q.get("hook"), "hook_en": a.get("hook_en") or q.get("hook_en")}
            render(a, folder / "reel.mp4", lang=PROFILE, handle=HANDLE_P)
        elif q.get("appids"):  # 할인 릴스
            rebuild_deal(q)
    elif kind == "series":
        if not folder.exists() and q.get("arts"):
            from carousel import build_weekly_top
            top = [x for i in q["arts"] for x in arts if x["id"] == i]
            if top:
                save_slides(q["id"], build_weekly_top(top))
    elif kind in ("deal", "story"):
        if not folder.exists():
            rebuild_deal(q)
    elif not folder.exists() and a:
        for i, im in enumerate(build_carousel(a, lang=PROFILE, handle=HANDLE_P)[:10], 1):
            folder.mkdir(parents=True, exist_ok=True)
            im.convert("RGB").save(folder / f"{i:02d}.jpg", quality=88)


def prepare():
    from carousel import build_carousel
    arts = load(DATA / "articles.json", [])
    if PROFILE == "en":  # 영문 계정: 영문 제목·요약이 있는 기사만
        arts = [a for a in arts if a.get("title_en") and a.get("tldr_en")]
    posted = load(POSTED, {})
    queue = [q for q in load(QUEUE, []) if q["id"] not in posted]
    queued = {q["id"] for q in queue}
    today = datetime.now().strftime("%Y-%m-%d")
    reels_today = sum(1 for p in posted.values() if p.get("kind") == "reel" and p.get("date") == today)         + sum(1 for q in queue if q.get("kind") == "reel")
    # 릴스는 캐러셀 한도와 별도로 하루 IG_REELS_DAILY 건
    want_reel = reels_today < int(os.environ.get("IG_REELS_DAILY", 3))
    cands = pick([a for a in arts if a["id"] not in queued], posted, 1, queued, cap=False, by_pop=True) if want_reel else []
    cands += [a for a in pick([a for a in arts if a["id"] not in queued], posted, int(os.environ.get("IG_PER_RUN", 1)), queued)
              if not cands or a["id"] != cands[0]["id"]]
    for a in cands:
        if want_reel and reels_today < int(os.environ.get("IG_REELS_DAILY", 3)):
            try:  # 릴스: 도달이 가장 넓은 형식 → 하루 상위 기사 몇 건은 영상으로
                from reels import render
                ensure_hook(a, PROFILE)
                (SITE / IGDIR / a["id"]).mkdir(parents=True, exist_ok=True)
                render(a, SITE / IGDIR / a["id"] / "reel.mp4", lang=PROFILE, handle=HANDLE_P)
                queue.append({"id": a["id"], "kind": "reel", "files": ["reel.mp4"],
                              "caption": caption(a) if PROFILE == "ko" else caption_en(a),
                              "hook": a.get("hook"), "hook_en": a.get("hook_en")})
                reels_today += 1
                print(f"인스타 준비(릴스): {a['title']}")
                continue
            except Exception as e:
                print(f"  ! 릴스 생성 실패, 캐러셀로: {e}")
        try:
            slides = build_carousel(a, lang=PROFILE, handle=HANDLE_P)[:10]  # 캐러셀 최대 10장
        except Exception as e:
            print(f"  ! 캐러셀 생성 실패 {a['id']}: {e}")
            continue
        names = []
        for i, im in enumerate(slides, 1):
            name = f"{i:02d}.jpg"
            (SITE / IGDIR / a["id"]).mkdir(parents=True, exist_ok=True)
            im.convert("RGB").save(SITE / IGDIR / a["id"] / name, quality=88)
            names.append(name)
        queue.append({"id": a["id"], "files": names, "caption": caption(a) if PROFILE == "ko" else caption_en(a)})
        print(f"인스타 준비: {a['title']} ({len(names)}장)")
    try:
        if PROFILE == "ko":  # 할인·시리즈는 한국어 계정만 (원화 가격)
            prepare_deals(queue, posted)
    except Exception as e:
        import traceback
        print(f"  ! 할인 게시물 준비 실패: {e}")
        st = load(DATA / "state.json", {})
        st["ig_deals_err"] = traceback.format_exc()[-600:]
        save(DATA / "state.json", st)
    # 이미 대기 중인 게시물의 이미지·영상도 다시 만들어 배포본에 포함 (사이트는 매번 새로 빌드되므로)
    for q in queue:
        try:
            regen_one(q, arts)
        except Exception as e:  # 하나가 실패해도 나머지는 준비
            print(f"  ! 재생성 실패 {q['id']}: {e}")
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
    """기사체 → 개조식. '공개했다'→'공개', '전했다'→'전함', '중심이 됐다'→'중심이 됨', '이어졌다'→'이어짐'."""
    t = t.strip().rstrip(".").strip()
    head, _, last = t.rpartition(" ")
    pre = (head + " ") if head else ""
    rules = [  # (어미, 명사 뒤면 떼고, 짧은 동사면 붙일 말)
        (("하였습니다", "했습니다", "합니다", "하였다", "했다", "한다"), "함"),
        (("되었습니다", "됐습니다", "됩니다", "되었다", "됐다", "된다"), "됨"),
        (("입니다", "이다"), "임"),
    ]
    for ends, short in rules:
        for e in ends:
            if last.endswith(e):
                stem = last[: -len(e)]
                if not stem:            # '중심이 됐다' 처럼 띄어 쓴 경우
                    return pre + short
                if len(stem) >= 2:      # '공개했다' → '공개', '출시됐다' → '출시'
                    return pre + stem
                return pre + stem + short  # '전했다' → '전함'
    for a_, b_ in CARD_MAP:
        if last.endswith(a_):
            return pre + last[: -len(a_)] + b_
    return t


QUESTIONS = {"신작": ["출시되면 바로 해볼 생각 있으신가요?", "이 게임 기대되시나요?"],
             "업데이트": ["이번 업데이트 어떻게 보시나요?", "가장 반가운 변화는 무엇인가요?"],
             "업계": ["이 소식 어떻게 보시나요?", "업계에 어떤 영향이 있을까요?"],
             "하드웨어": ["구매 의향 있으신가요?", "이 가격 어떻게 보시나요?"],
             "할인·이벤트": ["이번에 장바구니에 담을 게임은?"], "e스포츠": ["누가 이길 것 같나요?"]}


def question(a):
    qs = QUESTIONS.get(a.get("category"), ["이 소식 어떻게 보시나요?"])
    return qs[int(hashlib.md5(a["id"].encode()).hexdigest(), 16) % len(qs)]


def caption(a):
    tags = ["게임뉴스", "게임소식", "알트탭"]
    tags += [HASHTAG[a["category"]]] if a.get("category") in HASHTAG else []
    tags += [p.replace(" ", "") for p in a.get("platforms", [])]
    tags += ["".join(ch for ch in g if ch.isalnum()) for g in (a.get("games") or [])[:2]]
    srcs = [s_["name"] for s_ in a.get("sources", [])]
    lines = [a["title"], ""] + [to_news_tone(t) for t in a.get("tldr", [])[:3]]
    if srcs:
        lines += ["", "출처: " + srcs[0] + (f" 외 {len(srcs) - 1}곳" if len(srcs) > 1 else "")]
    lines += ["", question(a), "", " ".join("#" + t for t in dict.fromkeys(t for t in tags if t))]
    return "\n".join(lines)[:2100]


TAG_EN = {"신작": "newgame", "업데이트": "gameupdate", "업계": "gamingindustry", "하드웨어": "gaminghardware",
          "할인·이벤트": "gamedeals", "e스포츠": "esports"}


def caption_en(a):
    tags = ["gamingnews", "gamenews", "gaming", "videogames", "alttab"]
    tags += [TAG_EN[a["category"]]] if a.get("category") in TAG_EN else []
    tags += [p.replace(" ", "").lower() for p in a.get("platforms", [])]
    tags += ["".join(ch for ch in g if ch.isalnum()).lower() for g in (a.get("games_en") or [])[:2]]
    srcs = [s_["name"] for s_ in a.get("sources", [])]
    lines = [a.get("title_en") or a["title"], ""] + [f"▪ {t.rstrip('.')}." for t in (a.get("tldr_en") or [])[:3]]
    if srcs:
        lines += ["", "Source: " + srcs[0] + (f" +{len(srcs) - 1} more" if len(srcs) > 1 else "")]
    lines += ["", "Which game are you watching? 👇", "", " ".join("#" + t for t in dict.fromkeys(t for t in tags if t))]
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
    if os.environ.get(ENABLED_ENV) != "1" or not os.environ.get(UID_ENV):
        print(f"{ENABLED_ENV} 가 1이 아니거나 계정 정보가 없어 게시하지 않음 ({PROFILE})")
        return
    # 동시에 두 번 실행돼 같은 게시물이 중복으로 올라가는 것을 막는 잠금 (20분 지나면 무효)
    lock = DATA / f".ig_publish{SUF}.lock"
    if lock.exists() and time.time() - lock.stat().st_mtime < 20 * 60:
        print("다른 게시 작업이 진행 중이라 건너뜀")
        return
    lock.write_text(str(os.getpid()))
    try:
        _publish()
    finally:
        lock.unlink(missing_ok=True)


def _publish():
    # 게시 시간대 (한국 시간): 기본 10~23시. 밤·새벽에는 쌓아두기만
    lo, hi = (int(x) for x in os.environ.get("IG_HOURS", "10-23").split("-"))
    if not (lo <= datetime.now().hour < hi) and os.environ.get("IG_FORCE") != "1":
        print(f"게시 시간대({lo}~{hi}시)가 아니라 건너뜀")
        return
    refresh_if_needed()
    refresh_threads()
    uid = os.environ[UID_ENV]
    queue, posted = load(QUEUE, []), load(POSTED, {})
    # 오래된 대기열 정리: 뉴스·릴스는 기사 작성 후 30시간, 스토리는 그날만
    arts = {a["id"]: a for a in load(DATA / "articles.json", [])}
    today = datetime.now().strftime("%Y-%m-%d")
    def fresh(q):
        if q.get("kind") == "story":
            return q["id"].endswith(today)
        a = arts.get(q["id"])
        return a is None or time.time() - a.get("written", a["ts"]) < 30 * 3600
    dropped = [q["id"] for q in queue if not fresh(q)]
    queue = [q for q in queue if fresh(q)]
    if dropped:
        print(f"오래된 대기 {len(dropped)}건 정리")
    order = {"reel": 0, "series": 1, "deal": 2, "news": 3}
    queue.sort(key=lambda q: order.get(q.get("kind", "news"), 4))
    budget = int(os.environ.get("IG_PUBLISH_MAX", 99))  # 시간대 실행에서는 2건씩
    for q in list(queue):
        if q.get("kind") != "story" and budget <= 0:
            continue
        if q["id"] in posted:
            queue.remove(q)
            continue
        try:
            kind = q.get("kind", "news")
            if not wait_url(f"{site_url()}{IGDIR}/{q['id']}/{q['files'][0]}", tries=6):
                print(f"  ! 사이트에 아직 파일 없음, 다음 기회에: {q['id']}")  # 오래 기다리지 않음 (시간 초과 방지)
                continue
            if kind == "reel":
                c = api("POST", f"{uid}/media", media_type="REELS", video_url=f"{site_url()}{IGDIR}/{q['id']}/reel.mp4",
                        caption=q["caption"], share_to_feed="true", thumb_offset="800")["id"]  # 훅 문구가 꽉 찬 순간을 썸네일로
                wait_ready(c, tries=60)  # 영상 처리는 1~3분 걸림
                mid = api("POST", f"{uid}/media_publish", creation_id=c)["id"]
                posted[q["id"]] = {"media": mid, "kind": kind, "date": datetime.now().strftime("%Y-%m-%d"), "ts": time.time()}
                queue.remove(q)
                budget -= 1
                print(f"인스타 릴스 게시: {q['id']}")
                post_threads(q)
                continue
            if kind == "story":
                c = api("POST", f"{uid}/media", image_url=f"{site_url()}{IGDIR}/{q['id']}/{q['files'][0]}", media_type="STORIES")["id"]
                wait_ready(c)
                mid = api("POST", f"{uid}/media_publish", creation_id=c)["id"]
                posted[q["id"]] = {"media": mid, "kind": kind, "appids": q.get("appids", []),
                                   "date": datetime.now().strftime("%Y-%m-%d"), "ts": time.time()}
                queue.remove(q)
                print(f"인스타 스토리 게시: {q['id']}")
                continue
            children = []
            for f in q["files"]:
                url = f"{site_url()}{IGDIR}/{q['id']}/{f}"
                c = api("POST", f"{uid}/media", image_url=url, is_carousel_item="true")["id"]
                wait_ready(c)
                children.append(c)
            parent = api("POST", f"{uid}/media", media_type="CAROUSEL", children=",".join(children), caption=q["caption"])["id"]
            wait_ready(parent)
            mid = api("POST", f"{uid}/media_publish", creation_id=parent)["id"]
            posted[q["id"]] = {"media": mid, "kind": q.get("kind", "news"), "appids": q.get("appids", []),
                               "date": datetime.now().strftime("%Y-%m-%d"), "ts": time.time()}
            queue.remove(q)
            budget -= 1
            print(f"인스타 게시 완료: {q['id']} → {mid}")
            post_threads(q)
        except Exception as e:
            print(f"  ! 인스타 게시 실패 {q['id']}: {e}")
            q["fail"] = q.get("fail", 0) + 1
            q["err"] = str(e)[:300]
            if q["fail"] >= 3:  # 세 번 실패하면 포기
                queue.remove(q)
        time.sleep(5)
    save(QUEUE, queue)
    save(POSTED, posted)


# ---------- 스레드 (Threads API) ----------
TH_API = "https://graph.threads.net/v1.0"


def th_api(method, path, **params):
    params["access_token"] = os.environ["THREADS_ACCESS_TOKEN"]
    url = f"{TH_API}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=60).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"threads {e.code} {e.read()[:300].decode('utf-8', 'ignore')}")


def th_wait(cid, tries=40):
    for _ in range(tries):
        st = th_api("GET", cid, fields="status").get("status")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"threads 컨테이너 {st}")
        time.sleep(4)


def post_threads(q):
    """인스타에 올린 것과 같은 내용을 스레드에도 (한국어 계정만, 500자 제한)."""
    if PROFILE != "ko" or not (os.environ.get("THREADS_ACCESS_TOKEN") and os.environ.get("THREADS_USER_ID")):
        return
    done = load(DATA / "threads_posted.json", {})
    if q["id"] in done:
        return
    uid = os.environ["THREADS_USER_ID"]
    text = q.get("caption", "")
    if len(text) > 500:  # 해시태그를 줄여 500자 안으로
        body, _, tags = text.rpartition("\n\n")
        text = (body[:480] + "…") if len(body) > 480 else body
    base = f"{site_url()}{IGDIR}/{q['id']}/"
    try:
        if q.get("kind") == "reel":
            c = th_api("POST", f"{uid}/threads", media_type="VIDEO", video_url=base + "reel.mp4", text=text)["id"]
        else:
            kids = [th_api("POST", f"{uid}/threads", media_type="IMAGE", image_url=base + f, is_carousel_item="true")["id"]
                    for f in q["files"][:20]]
            for k in kids:
                th_wait(k)
            c = th_api("POST", f"{uid}/threads", media_type="CAROUSEL", children=",".join(kids), text=text)["id"]
        th_wait(c)
        mid = th_api("POST", f"{uid}/threads_publish", creation_id=c)["id"]
        done[q["id"]] = {"media": mid, "ts": time.time()}
        save(DATA / "threads_posted.json", done)
        print(f"스레드 게시: {q['id']}")
    except Exception as e:
        print(f"  ! 스레드 게시 실패 {q['id']}: {e}")


def refresh_threads():
    """스레드 토큰도 60일 → 주 1회 연장 (연장된 토큰은 같은 값이 유지되는 경우가 많아 기록만)."""
    if not os.environ.get("THREADS_ACCESS_TOKEN"):
        return
    st = load(DATA / "state.json", {})
    if time.time() - st.get("threads_refreshed", 0) < 7 * 86400:
        return
    try:
        urllib.request.urlopen("https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token="
                               + urllib.parse.quote(os.environ["THREADS_ACCESS_TOKEN"]), timeout=30).read()
        st["threads_refreshed"] = time.time()
        save(DATA / "state.json", st)
    except Exception as e:
        print(f"  ! 스레드 토큰 연장 실패: {e}")


def check():
    state = load(DATA / "state.json", {})
    try:
        me = api("GET", "me", fields="user_id,username,account_type,media_count")
        state["ig" + SUF] = f"ok @{me.get('username')} ({me.get('account_type')}, 게시물 {me.get('media_count')})"
        state["ig_user_id_match"] = str(me.get("user_id")) == os.environ.get(UID_ENV, "")
    except Exception as e:
        state["ig" + SUF] = f"오류: {e}"
    save(DATA / "state.json", state)
    print("인스타:", state["ig" + SUF])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        {"prepare": prepare, "publish": publish, "check": check}[sys.argv[1]]()
    except Exception:  # GitHub 로그 없이도 원인을 볼 수 있게 기록
        import traceback
        st = load(DATA / "state.json", {})
        st[f"ig_{sys.argv[1]}_error{SUF}"] = traceback.format_exc()[-1500:]
        save(DATA / "state.json", st)
        raise
