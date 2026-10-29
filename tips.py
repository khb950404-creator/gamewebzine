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


def ps_plus(arts):
    """최근 PS 플러스 월간 게임 기사의 체크 포인트에서 기간·라인업."""
    for a in sorted(arts, key=lambda a: -a.get("written", a["ts"])):
        if time.time() - a.get("written", a["ts"]) > 20 * 86400:
            break
        if "PS 플러스" in a["title"] and a.get("check") and a.get("img"):
            c = {x["k"]: x["v"] for x in a["check"]}
            if c.get("라인업") and c.get("기간"):
                return {"name": "PS 플러스 " + c["기간"].split("~")[0].split("월")[0] + "월 게임", "img": a.get("img_hd") or a["img"],
                        "chip": "구독자 무료", "lines": [c["라인업"], c["기간"] + " 라이브러리 추가"]}


def free_games(arts):
    items = []
    try:
        items += epic_free()
    except Exception as e:
        print(f"  ! 에픽 무료 게임 조회 실패: {e}")
    p = ps_plus(arts)
    if p:
        items.append(p)
    return items[:5]


def chip_of(a):
    """칩: 기사에서 가장 먼저 챙길 날짜 + 무엇 (예: '11/12 프리로드', '11/2 마감')."""
    for c in a.get("check") or []:
        m = re.search(r"(\d{1,2})월 (\d{1,2})일", c["v"])
        if m and any(k in c["k"] for k in ("마감", "프리로드", "시작", "기간", "출시")):
            k = next(k for k in ("마감", "프리로드", "시작", "기간", "출시") if k in c["k"])
            return f"{m[1]}/{m[2]} {'시작' if k == '기간' else k}"
    m = re.search(r"(\d{1,2})월 (\d{1,2})일", a.get("takeaway", ""))
    return f"{m[1]}/{m[2]}까지" if m else "체크"


def weekly_checklist(arts, popularity, n=4):
    """이번 주 놓치면 손해인 것: 정리 문장(takeaway)이 있는 최근 기사 중 인기순, 게임당 하나."""
    cand = [a for a in arts if a.get("takeaway") and a.get("img") and time.time() - a.get("written", a["ts"]) < 7 * 86400]
    items, used = [], set()
    for a in sorted(cand, key=lambda a: -popularity(a)):
        keys = {g.lower() for g in (a.get("games") or []) + (a.get("games_en") or [])} | {str(a.get("appid") or a["id"]), a["img"]}
        if keys & used:
            continue
        used |= keys
        name = a.get("subject") or (a.get("games") or [re.split(r"[,…]", a["title"])[0]])[0]
        items.append({"name": name, "img": a.get("img_hd") or a["img"], "chip": chip_of(a),
                      "lines": [a["takeaway"]], "id": a["id"]})
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
