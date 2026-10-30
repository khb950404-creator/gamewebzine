"""실전 꿀팁 시리즈 — 저장하게 만드는 '바로 써먹는 정보' 릴스.

  free_games()          이번 주 무료로 받을 수 있는 게임 (에픽 무료 배포 + PS 플러스 기사)
  weekly_checklist()    이번 주 게이머 체크리스트 (기사 체크 포인트·정리 중 '언제까지 무엇을' 있는 것)

  python tips.py        # out/tips/ 에 두 릴스 샘플
"""
import json, re, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
WD = "월화수목금토일"


def kst(iso):
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST)
    ap = "오전" if t.hour < 12 else "오후"
    h = t.hour % 12 or 12
    return f"{t.month}월 {t.day}일({WD[t.weekday()]}) {ap} {h}시"


def epic_free():
    url = "https://store-site-backend-static.epicgames.com/freeGamesPromotions?locale=ko&country=KR&allowCountries=KR"
    j = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20).read())
    out = []
    for e in j["data"]["Catalog"]["searchStore"]["elements"]:
        offers = [o for g in ((e.get("promotions") or {}).get("promotionalOffers") or []) for o in g.get("promotionalOffers", [])]
        free = [o for o in offers if (o.get("discountSetting") or {}).get("discountPercentage") == 0]
        if not free or e["price"]["totalPrice"]["discountPrice"] != 0:
            continue
        imgs = {i["type"]: i["url"] for i in e.get("keyImages", [])}
        img = imgs.get("OfferImageTall") or imgs.get("DieselStoreFrontTall") or imgs.get("OfferImageWide") or imgs.get("Thumbnail")
        if not img:
            continue
        orig = e["price"]["totalPrice"].get("originalPrice") or 0
        out.append({"name": e["title"], "img": img, "chip": "무료",
                    "lines": [f"에픽게임즈 스토어 · {kst(free[0]['endDate'])}까지"]
                             + ([f"정가 ₩{orig:,} → 0원, 받으면 영구 소장"] if orig else ["받으면 영구 소장"])})
    return out


TAG_PROMPT = """게임 '{name}'이 무슨 게임인지 한국어 한 줄(18~32자)로. 광고 문구·감탄 금지, 장르와 핵심 플레이만 사실대로.
예: "LA 배경 1인칭 좀비 액션 RPG", "열차를 지키는 로그라이크 덱빌딩", "툼 레이더 4·5·6편 합본 리마스터"
스팀 소개: {desc}
장르: {genres}
JSON: {{"line": "..."}}"""


def steam_info(name):
    """스팀에서 게임 정보: 무슨 게임인지 한 줄 + 정가 + 세로 키아트."""
    import urllib.parse
    q = json.loads(urllib.request.urlopen("https://store.steampowered.com/api/storesearch/?cc=kr&l=korean&term="
                                          + urllib.parse.quote(name), timeout=20).read()).get("items") or []
    if not q:
        return None
    app = q[0]["id"]
    d = json.loads(urllib.request.urlopen(f"https://store.steampowered.com/api/appdetails?appids={app}&cc=kr&l=koreana",
                                          timeout=20).read())[str(app)].get("data") or {}
    desc = re.split(r"(?<=[.!?다])\s", re.sub(r"<[^>]+>", "", d.get("short_description") or ""))[0].strip()
    pr = d.get("price_overview") or {}
    cats = {c["description"] for c in d.get("categories", [])}
    mode = "협동 지원" if any("협동" in c for c in cats) else ("멀티플레이" if any("멀티" in c for c in cats) else "싱글 플레이")
    genres = [g["description"] for g in d.get("genres", []) if g["description"] not in ("인디", "앞서 해보기")][:2]
    fact = " · ".join(filter(None, [", ".join(genres), mode, "한국어 지원" if "한국어" in (d.get("supported_languages") or "") else ""]))
    try:  # 광고 문구 대신 '무슨 게임인지' 한 줄 (실패하면 장르·모드 사실만)
        import webzine
        r = webzine.llm(TAG_PROMPT.format(name=name, desc=d.get("short_description", ""), genres=", ".join(genres)), prefer_lite=True) or {}
        desc = (r.get("line") or "").strip() or fact
    except Exception:
        desc = fact
    return {"appid": app, "desc": desc[:60], "fact": fact, "price": pr.get("initial_formatted") or pr.get("final_formatted") or "",
            "genres": [g["description"] for g in d.get("genres", [])][:2],
            "img": f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{app}/library_600x900_2x.jpg",
            "img2": d.get("header_image")}


def ps_plus(arts):
    """최근 PS 플러스 월간 게임 기사 → 게임마다 한 장면 (이름·무슨 게임·정가·기간)."""
    for a in sorted(arts, key=lambda a: -a.get("written", a["ts"])):
        if time.time() - a.get("written", a["ts"]) > 20 * 86400:
            break
        c = {x["k"]: x["v"] for x in a.get("check") or []}
        if "PS 플러스" not in a["title"] or not c.get("라인업") or not c.get("기간"):
            continue
        out = []
        for g in [x.strip() for x in c["라인업"].split(",") if x.strip()]:
            info = None
            en = dict(zip(a.get("games") or [], a.get("games_en") or [])).get(g, g)  # 스팀 검색은 영문명으로
            try:
                info = steam_info(en) or steam_info(re.sub(r"4-6", "IV-VI", en))
            except Exception as e:
                print(f"  ! 스팀 정보 실패 {g}: {e}")
            lines = []
            if info:
                lines.append(info["desc"])
                if info["fact"] and info["fact"] != info["desc"]:
                    lines.append(info["fact"])
            lines.append(f"{c['기간'].replace('(화)', '').replace('(월)', '')} 무료" + (f" (정가 {info['price'].replace('₩ ', '₩')})" if info and info["price"] else ""))
            out.append({"name": g, "img": (info or {}).get("img") or a.get("img_hd") or a["img"], "img2": (info or {}).get("img2"),
                        "appid": (info or {}).get("appid"),
                        "chip": "PS 플러스", "lines": lines})
        return out
    return []


def free_games(arts):
    items = []
    try:
        items += epic_free()
    except Exception as e:
        print(f"  ! 에픽 무료 게임 조회 실패: {e}")
    items += ps_plus(arts)
    return items[:6]


def _dates(text):
    """'11월 12일' 들 → 올해(연말이면 내년) datetime 목록."""
    now = datetime.now(KST) if "KST" in globals() else datetime.now()
    out = []
    for mo, d in re.findall(r"(\d{1,2})월 (\d{1,2})일", text):
        try:
            y = now.year + (1 if int(mo) < now.month - 6 else 0)
            out.append(datetime(y, int(mo), int(d)).date())
        except ValueError:
            pass
    return out


def upcoming(a):
    """오늘 이후 챙길 날짜가 있는 기사만 '놓치면 손해' 대상 (이미 지난 출시·마감은 뺀다)."""
    today = datetime.now().date()
    text = " ".join(c["v"] for c in a.get("check") or []) + " " + a.get("takeaway", "")
    return any(d >= today for d in _dates(text))


def chip_of(a):
    """칩: 오늘 이후 가장 먼저 챙길 날짜 + 무엇 (예: '11/12 프리로드', '11/2 마감')."""
    today = datetime.now().date()
    keys = ("마감", "프리로드", "시작", "기간", "출시", "발매", "배포")
    hits = []
    for c in a.get("check") or []:
        ds = [d for d in _dates(c["v"]) if d >= today]
        if ds and any(k in c["k"] for k in keys):
            k = next(k for k in keys if k in c["k"])
            hits.append((ds[0], '시작' if k == '기간' else k))
    if hits:
        d, k = min(hits)  # 가장 먼저 오는 날 (출시보다 프리로드가 먼저)
        return f"{d.month}/{d.day} {k}"
    ds = [d for d in _dates(a.get("takeaway", "")) if d >= today]
    return f"{ds[0].month}/{ds[0].day}까지" if ds else "체크"


def lines_of(a, n=3):
    """알맹이: 체크 포인트 중 날짜·숫자·고유명사가 있는 행을 '항목 값'으로."""
    rows = [c for c in a.get("check") or [] if c["k"] not in ("플랫폼",)]
    rows.sort(key=lambda c: -len(re.findall(r"\d", c["v"])))
    out = [f"{c['k']}  {c['v']}" for c in rows[:n]]
    return out or [a["takeaway"]]


def weekly_checklist(arts, popularity, n=4):
    """이번 주 놓치면 손해인 것: 정리 문장(takeaway)이 있는 최근 기사 중 인기순, 게임당 하나."""
    cand = [a for a in arts if a.get("takeaway") and a.get("img") and time.time() - a.get("written", a["ts"]) < 10 * 86400
            and upcoming(a)]
    items, used = [], set()
    for a in sorted(cand, key=lambda a: -popularity(a)):
        keys = {g.lower() for g in (a.get("games") or []) + (a.get("games_en") or [])} | {str(a.get("appid") or a["id"]), a["img"]}
        if keys & used:
            continue
        used |= keys
        name = a.get("subject") or (a.get("games") or [re.split(r"[,…]", a["title"])[0]])[0]
        items.append({"name": name, "img": a.get("img_hd") or a["img"], "chip": chip_of(a), "appid": a.get("appid"), "art": a["id"],
                      "lines": lines_of(a), "id": a["id"]})
        if len(items) == n:
            break
    return items


def caption(title, items, tags):
    lines = [title, ""]
    for i, x in enumerate(items, 1):
        lines += [f"{i}. {x['name']}"] + [f"   {ln}" for ln in x["lines"]]
    lines += ["", "저장해두고 놓치지 마세요. 매주 이런 정보만 골라 올립니다.", "", " ".join("#" + t for t in tags)]
    return "\n".join(lines)[:2100]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    from pathlib import Path
    from reels import render_tips
    from ig_post import popularity
    arts = json.loads(Path("data/articles.json").read_text(encoding="utf-8"))
    out = Path("out/tips")
    out.mkdir(parents=True, exist_ok=True)
    f = free_games(arts)
    print(json.dumps(f, ensure_ascii=False, indent=1))
    if f:
        render_tips(f, out / "free.mp4", f"이번 주 공짜 게임 {len(f)}개", key="free-test")
    c = weekly_checklist(arts, popularity)
    print(json.dumps(c, ensure_ascii=False, indent=1))
    if c:
        render_tips(c, out / "check.mp4", "이번 주 놓치면 손해", key="chk-test")
