"""롤드컵(LoL 월드 챔피언십) 한국팀 결과 → 결과표 이미지 + 기사.

데이터: Liquipedia (CC BY-SA 3.0, API 이용 규칙: gzip·User-Agent·요청 간격 2초 이상).
  python esports.py          # 새 한국팀 경기 결과가 있으면 기사 1건 + 결과표 이미지 생성
  python esports.py table    # 결과표 이미지만 out/ 에 생성 (확인용)
"""
import gzip, hashlib, html, io, json, re, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from PIL import Image, ImageDraw

from cards import W, H
from card_themes import BH, NB, NM, NR, ANTON

ROOT = Path(__file__).parent
DATA = ROOT / "data"
MEDIA = ROOT / "media" / "esports"
UA = "AltTabWebzine/1.0 (khb950404@gmail.com)"
API = "https://liquipedia.net/leagueoflegends/api.php"
BASE = "https://liquipedia.net"
EVENT = "World_Championship/2026"
STAGES = [("Swiss_Stage", "스위스 스테이지"), ("Knockout_Stage", "녹아웃 스테이지")]
KR = {"T1": "T1", "Gen.G Esports": "젠지", "Hanwha Life Esports": "한화생명e스포츠", "Dplus": "디플러스 기아",
      "KT Rolster": "kt 롤스터", "Nongshim RedForce": "농심 레드포스", "DRX": "DRX", "BNK FEARX": "BNK 피어엑스"}
CHIP = {"Hanwha Life Esports": "한화생명", "Dplus": "디플러스", "Nongshim RedForce": "농심", "BNK FEARX": "BNK"}
KST = timezone(timedelta(hours=9))
BG, CARD, LINE = (12, 12, 15), (24, 24, 29), (44, 44, 52)
WHITE, SOFT, DIM, RED, GOLD = (245, 245, 248), (200, 200, 208), (120, 120, 130), (229, 72, 77), (245, 178, 51)
_last = [0.0]


def api(**params):
    """Liquipedia API (요청 간격 2초 이상, gzip 필수)."""
    wait = 2.2 - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    q = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in {**params, "format": "json"}.items())
    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    raw = urllib.request.urlopen(req, timeout=30).read()
    _last[0] = time.time()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw)


import urllib.parse  # noqa: E402


def _team(cell):
    short = re.search(r'class="team-shortname">(.*?)</div>', cell)
    name = re.search(r'class="team-name">(.*?)</div>', cell)
    imgs = re.findall(r'<img[^>]+src="([^"]+)"', cell)
    dark = [i for i in imgs if "darkmode" in i] or [i for i in imgs if "allmode" in i] or imgs
    return {"short": html.unescape(short.group(1)) if short else "?", "name": html.unescape(name.group(1)) if name else "?",
            "logo": BASE + re.sub(r"/\d+px-", "/120px-", dark[0]) if dark else None}


def stage_matches(stage):
    h = api(action="parse", page=f"{EVENT}/{stage}", prop="text")["parse"]["text"]["*"]
    out, rnd = [], ""
    # 라운드 제목과 경기 블록을 문서 순서대로 훑는다
    for m in re.finditer(r'<span class="mw-headline" id="([^"]+)"|class="brkts-(?:matchlist-match|match)\b[^"]*"', h):
        if m.group(1):
            rnd = html.unescape(m.group(1)).replace("_", " ")
            continue
        blk = h[m.start(): m.start() + 9000]
        opp = re.findall(r'(<div class="brkts-(?:matchlist-cell brkts-matchlist-opponent|opponent-entry)[^>]*>.*?</span></div></div>)', blk, re.S)
        sc = re.findall(r'brkts-(?:matchlist-score|opponent-score-inner)[^>]*>(?:<div[^>]*>)?\s*(\d+|W|L|FF|-)?\s*<', blk)
        ts = re.search(r'data-timestamp="(\d+)"', blk)
        fin = 'data-finished="finished"' in blk[:9000]
        if len(opp) < 2 or not ts:
            continue
        t1, t2 = _team(opp[0]), _team(opp[1])
        s1, s2 = (sc + ["", ""])[:2]
        out.append({"round": rnd, "ts": int(ts.group(1)), "t1": t1, "t2": t2, "s1": s1 or "", "s2": s2 or "",
                    "done": fin and s1 not in ("", "-")})
    # 같은 경기가 팝업에 한 번 더 나오는 경우 제거
    seen, uniq = set(), []
    for x in out:
        k = (x["ts"], x["t1"]["name"], x["t2"]["name"])
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def korean(ms):
    return [m for m in ms if m["t1"]["name"] in KR or m["t2"]["name"] in KR]


def kname(t):
    return KR.get(t["name"], t["short"])


_logo_cache = {}


def logo(url, size):
    if not url:
        return None
    if url not in _logo_cache:
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20).read()
            _logo_cache[url] = Image.open(io.BytesIO(raw)).convert("RGBA")
            time.sleep(1)
        except Exception:
            _logo_cache[url] = None
    im = _logo_cache[url]
    if not im:
        return None
    im = im.copy()
    im.thumbnail((size, size), Image.LANCZOS)
    return im


def records(ms):
    """한국팀별 승-패 (끝난 경기 기준)."""
    rec = {}
    for m in ms:
        if not m["done"]:
            continue
        for me, op, a, b in ((m["t1"], m["t2"], m["s1"], m["s2"]), (m["t2"], m["t1"], m["s2"], m["s1"])):
            if me["name"] in KR:
                w, l = rec.get(me["name"], (0, 0))
                win = a.isdigit() and b.isdigit() and int(a) > int(b)
                rec[me["name"]] = (w + win, l + (not win))
    return rec


def render_table(ms, stage_ko, out):
    """일자별 한국팀 경기 결과표 (1080x1350)."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((76, 80), "LoL 월드 챔피언십 2026", font=NB(34), fill=RED)
    d.text((76, 130), f"{stage_ko} 한국팀 결과", font=BH(64), fill=WHITE)
    # 승패 요약
    rec = records(ms)
    x, cy = 76, 238
    for name, (w, l) in sorted(rec.items(), key=lambda r: (-r[1][0], r[1][1])):
        lab = f"{CHIP.get(name, KR[name])} {w}승 {l}패"
        tw = d.textlength(lab, font=NB(28)) + 36
        if x + tw > W - 76:  # 넘치면 다음 줄
            x, cy = 76, cy + 60
        d.rounded_rectangle((x, cy, x + tw, cy + 48), 24, fill=CARD, outline=LINE)
        d.text((x + 18, cy + 24), lab, font=NB(28), fill=WHITE, anchor="lm")
        x += tw + 12
    y = cy + 92
    ms = [m for m in ms if m["done"]]  # 예정 경기는 대진이 바뀔 수 있어 넣지 않는다
    days = {}
    for m in sorted(ms, key=lambda m: m["ts"]):
        days.setdefault(datetime.fromtimestamp(m["ts"], KST).date(), []).append(m)
    wd = "월화수목금토일"
    rows_left = (H - 120 - y) // 96
    for day, lst in days.items():
        if rows_left <= 1:
            break
        d.text((76, y), f"{day.month}월 {day.day}일 ({wd[day.weekday()]})", font=NB(30), fill=SOFT)
        y += 52
        rows_left -= 0.5
        for m in lst:
            if rows_left <= 0:
                break
            d.rounded_rectangle((76, y, W - 76, y + 80), 16, fill=CARD)
            win1 = m["done"] and m["s1"].isdigit() and m["s2"].isdigit() and int(m["s1"]) > int(m["s2"])
            win2 = m["done"] and not win1
            for side, t, win in ((0, m["t1"], win1), (1, m["t2"], win2)):
                lg = logo(t["logo"], 54)
                col = WHITE if (win or not m["done"]) else DIM
                f = NB(34) if t["name"] in KR else NM(32)
                if side == 0:
                    if lg:
                        img.paste(lg, (100, y + 40 - lg.height // 2), lg)
                    d.text((170, y + 40), kname(t), font=f, fill=col, anchor="lm")
                else:
                    if lg:
                        img.paste(lg, (W - 100 - lg.width, y + 40 - lg.height // 2), lg)
                    d.text((W - 170, y + 40), kname(t), font=f, fill=col, anchor="rm")
            if m["done"]:
                d.text((W // 2 - 30, y + 40), m["s1"], font=ANTON(48), fill=WHITE if win1 else DIM, anchor="mm")
                d.text((W // 2, y + 40), ":", font=ANTON(40), fill=DIM, anchor="mm")
                d.text((W // 2 + 30, y + 40), m["s2"], font=ANTON(48), fill=WHITE if win2 else DIM, anchor="mm")
            else:
                t = datetime.fromtimestamp(m["ts"], KST).strftime("%H:%M")
                d.text((W // 2, y + 40), t, font=NB(32), fill=GOLD, anchor="mm")
            y += 96
            rows_left -= 1
        y += 14
    d.text((76, H - 70), "자료: Liquipedia (CC BY-SA 3.0) · 시간은 한국 기준", font=NR(24), fill=DIM)
    d.text((W - 56, H - 70), "@webzinealttab", font=NB(28), fill=RED, anchor="ra")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out


def render_cover(ms, out):
    """표지: 이번 결과에 나온 한국팀 로고를 크게 (위쪽 절반, 아래는 제목이 들어갈 자리)."""
    teams, seen = [], set()
    for m in sorted(ms, key=lambda m: -m["ts"]):
        for t in (m["t1"], m["t2"]):
            if t["name"] in KR and t["name"] not in seen:
                seen.add(t["name"])
                teams.append(t)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for i in range(H):  # 위에서 아래로 은은한 붉은빛 → 검정
        k = max(0, 1 - i / (H * 0.75))
        d.line([(0, i), (W, i)], fill=(int(12 + 60 * k), int(12 + 8 * k), int(15 + 10 * k)))
    n = len(teams[:4]) or 1
    size = {1: 340, 2: 300, 3: 250}.get(n, 200)
    gap = 50  # 4팀이면 200x4 + 50x3 = 950px (화면 안에 다 들어가게)
    total_w = n * size + (n - 1) * gap
    x0, y0 = (W - total_w) // 2, 260
    for i, t in enumerate(teams[:4]):
        lg = logo(t["logo"].replace("/120px-", "/600px-") if t["logo"] else None, size)
        if lg:
            img.paste(lg, (x0 + i * (size + gap) + (size - lg.width) // 2, y0 + (size - lg.height) // 2), lg)
    d.text((W // 2, y0 + size + 70), "LoL 월드 챔피언십 2026", font=NB(36), fill=SOFT, anchor="mm")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out


PROMPT = """너는 한국 게임 매체의 e스포츠 기자다. 오늘은 {today}.
아래는 LoL 월드 챔피언십 2026 {stage} 경기 결과 데이터(한국 시간)다. 한국팀 소식 위주로 기사를 써라.
데이터에 없는 내용(선수 발언, 경기 내용 묘사, 밴픽)은 절대 지어내지 마라. 결과·전적·다음 일정만으로 쓴다.
팀 이름은 국내 표기(T1, 젠지, 한화생명e스포츠, 디플러스 기아 등), 해외팀은 통용 표기(빌리빌리 게이밍=BLG 등)를 쓴다.
스위스 스테이지는 3승이면 녹아웃 진출, 3패면 탈락이다.

JSON 하나만: {{"title": "제목(핵심 결과가 보이게, 45자 이내)", "tldr": ["서로 다른 정보 3줄, 기사체"],
 "why": "한 문장", "body": ["2~4문단"], "hook": "8~16자", "title_en": "...", "tldr_en": ["..."], "hook_en": "..."}}

최근 경기 결과: {recent}
한국팀 전적: {rec}
다음 경기: {upcoming} (미정이면 다음 경기 상대·일정은 쓰지 마라)"""


def make_article():
    import webzine
    webzine.load_env()
    arts = webzine.load_json(DATA / "articles.json", [])
    state = webzine.load_json(DATA / "esports.json", {})
    stage, stage_ko, ms = None, None, []
    for st, ko in STAGES:
        try:
            x = korean(stage_matches(st))
        except Exception as e:
            print(f"  ! {st}: {e}")
            continue
        if any(m["done"] for m in x):
            stage, stage_ko, ms = st, ko, x
    if not ms:
        print("롤드컵: 한국팀 경기 결과 없음")
        return None
    done = [m for m in ms if m["done"]]
    key = hashlib.md5(json.dumps([(m["ts"], m["s1"], m["s2"]) for m in done]).encode()).hexdigest()[:10]
    if state.get("last") == key:
        print("롤드컵: 새 결과 없음")
        return None
    last_day = max(datetime.fromtimestamp(m["ts"], KST).date() for m in done)
    recent = [m for m in done if datetime.fromtimestamp(m["ts"], KST).date() == last_day]
    fmt = lambda m: f"{datetime.fromtimestamp(m['ts'], KST):%m/%d %H:%M} [{m['round']}] {kname(m['t1'])}({m['t1']['name']}) {m['s1']}:{m['s2']} {kname(m['t2'])}({m['t2']['name']})"
    upcoming = []  # 스위스 대진은 결과에 따라 바뀌어 미리 쓰지 않는다
    r = webzine.llm(PROMPT.format(today=datetime.now(KST).strftime("%Y년 %m월 %d일"), stage=stage_ko,
                                  recent="; ".join(fmt(m) for m in recent),
                                  rec=", ".join(f"{KR[k]} {w}승 {l}패" for k, (w, l) in records(ms).items()),
                                  upcoming="; ".join(fmt(m) for m in upcoming) or "미정"))
    if not r or not r.get("title"):
        return None
    aid = "worlds26-" + key
    img = render_table(ms, stage_ko, MEDIA / f"{aid}.jpg")
    cov = render_cover([m for m in ms if m["done"]], MEDIA / f"{aid}_cover.jpg")
    now = time.time()
    a = {"id": aid, "title": r["title"], "tldr": r.get("tldr", [])[:3], "why": r.get("why", ""), "body": r.get("body", []),
         "hook": r.get("hook"), "title_en": r.get("title_en"), "tldr_en": r.get("tldr_en"), "hook_en": r.get("hook_en"),
         "category": "e스포츠", "platforms": ["PC"], "games": ["리그 오브 레전드"], "games_en": ["League of Legends"],
         "trust": "확인", "status": "published", "ts": max(m["ts"] for m in recent), "written": now,
         "img": f"media/esports/{cov.name}", "img_hd": f"media/esports/{cov.name}", "img_src": "알트탭 (로고: Liquipedia)",
         "gallery": [{"src": f"media/esports/{img.name}", "credit": "알트탭 (자료: Liquipedia)"}],
         "sources": [{"name": "Liquipedia", "title": f"2026 World Championship - {stage.replace('_', ' ')}",
                      "link": f"{BASE}/leagueoflegends/{EVENT}/{stage}", "official": False}],
         "esports": True}
    webzine.dedupe_summary(a)
    arts.append(a)
    webzine.save_json(DATA / "articles.json", arts)
    state["last"] = key
    webzine.save_json(DATA / "esports.json", state)
    print(f"롤드컵 기사: {a['title']}")
    return a


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["table"]:
        ms = korean(stage_matches("Swiss_Stage"))
        for m in ms:
            print(datetime.fromtimestamp(m["ts"], KST).strftime("%m/%d %H:%M"), m["round"], kname(m["t1"]), m["s1"], ":", m["s2"], kname(m["t2"]), m["done"])
        print(render_table(ms, "스위스 스테이지", ROOT / "out" / "worlds_table.jpg"))
    else:
        make_article()
