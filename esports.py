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
    """경기 블록(brkts-matchlist-match / brkts-match) 단위로 잘라서 두 팀·점수·시각·라운드를 읽는다."""
    h = api(action="parse", page=f"{EVENT}/{stage}", prop="text")["parse"]["text"]["*"]
    starts = [m.start() for m in re.finditer(r'class="brkts-(?:matchlist-match|match brkts-match-popup-wrapper)\b', h)]
    out = []
    for i, st in enumerate(starts):
        blk = h[st: starts[i + 1] if i + 1 < len(starts) else st + 20000]
        opp = re.findall(r'(<div class="brkts-(?:matchlist-cell brkts-matchlist-opponent|opponent-entry)[^>]*>.*?</span></div></div>)', blk, re.S)
        sc = re.findall(r'brkts-(?:matchlist-score|opponent-score-inner)[^>]*>(?:<div[^>]*>)?\s*(\d+|W|L|FF|-)?\s*<', blk)
        ts = re.search(r'data-timestamp="(\d+)"', blk)
        if len(opp) < 2 or not ts:
            continue
        t1, t2 = _team(opp[0]), _team(opp[1])
        if "TBD" in (t1["short"], t2["short"]) or t1["name"] == t2["name"]:
            continue
        # 라운드: 이 경기 앞에 나온 가장 가까운 'Round N ...' 제목
        prev = re.findall(r'(Round \d+(?: (?:High|Mid|Middle|Low))?)', re.sub(r"<[^>]+>", " ", h[max(0, st - 6000): st]))
        s1, s2 = (sc + ["", ""])[:2]
        fin = 'data-finished="finished"' in blk
        bo = re.search(r"\(Bo(\d)\)", re.sub(r"<[^>]+>", " ", blk))
        out.append({"round": prev[-1] if prev else "", "ts": int(ts.group(1)), "t1": t1, "t2": t2,
                    "s1": s1 or "", "s2": s2 or "", "done": fin and (s1 or "") not in ("", "-"),
                    "bo": int(bo.group(1)) if bo else None})
    last = ""
    for x in out:  # 라운드 제목은 묶음의 첫 경기에만 붙으므로 뒤로 이어 준다
        x["round"] = x["round"] or last
        last = x["round"]
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


GROUP_KO = {"High": "2승 0패 조", "Mid": "1승 1패 조", "Middle": "1승 1패 조", "Low": "0승 2패 조"}


def render_bracket(ms, title, out):
    """한 라운드 전체 대진표: 조별(2-0 / 1-1 / 0-2)로 묶고, 한국팀은 강조."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((76, 80), "LoL 월드 챔피언십 2026", font=NB(34), fill=RED)
    d.text((76, 130), title, font=BH(60), fill=WHITE)
    groups = {}
    for m in sorted(ms, key=lambda m: m["ts"]):
        g = m["round"].split()[-1] if m["round"] else ""
        groups.setdefault(g, []).append(m)
    y, wd = 250, "월화수목금토일"
    row = 84 if len(ms) <= 8 else 70
    order = ["High", "Mid", "Middle", "Low"]
    for g, lst in sorted(groups.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 9):
        bo = lst[0].get("bo")
        d.text((76, y), GROUP_KO.get(g, g) + (" · 단판" if bo == 1 else f" · {bo}판 2선승" if bo == 3 else ""),
               font=NB(30), fill=GOLD if g == "High" else SOFT if g in ("Mid", "Middle") else DIM)
        y += 50
        for m in lst:
            kr = m["t1"]["name"] in KR or m["t2"]["name"] in KR
            d.rounded_rectangle((76, y, W - 76, y + row - 12), 16, fill=CARD, outline=RED if kr else None, width=2)
            cy = y + (row - 12) // 2
            for side, t in ((0, m["t1"]), (1, m["t2"])):
                lg = logo(t["logo"], 48)
                f = NB(32) if t["name"] in KR else NM(30)
                col = WHITE if t["name"] in KR else SOFT
                if side == 0:
                    if lg:
                        img.paste(lg, (100, cy - lg.height // 2), lg)
                    d.text((164, cy), kname(t), font=f, fill=col, anchor="lm")
                else:
                    if lg:
                        img.paste(lg, (W - 100 - lg.width, cy - lg.height // 2), lg)
                    d.text((W - 164, cy), kname(t), font=f, fill=col, anchor="rm")
            dt = datetime.fromtimestamp(m["ts"], KST)
            if m["done"]:
                d.text((W // 2, cy), f"{m['s1']} : {m['s2']}", font=ANTON(42), fill=WHITE, anchor="mm")
            else:
                d.text((W // 2, cy - 14), f"{dt.month}/{dt.day}({wd[dt.weekday()]})", font=NR(24), fill=DIM, anchor="mm")
                d.text((W // 2, cy + 16), dt.strftime("%H:%M"), font=NB(32), fill=GOLD, anchor="mm")
            y += row
        y += 18
    d.text((76, H - 70), "자료: Liquipedia (CC BY-SA 3.0) · 시간은 한국 기준", font=NR(24), fill=DIM)
    d.text((W - 56, H - 70), "@webzinealttab", font=NB(28), fill=RED, anchor="ra")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out


DRAW_PROMPT = """너는 한국 게임 매체의 e스포츠 기자다. 오늘은 {today}.
LoL 월드 챔피언십 2026 스위스 스테이지 {rnd}라운드 대진이 확정됐다. 아래 데이터만으로 기사를 써라 (지어내기 금지).
스위스 스테이지는 3승이면 녹아웃 진출, 3패면 탈락. 2승 0패 조 경기는 이기면 바로 진출, 0승 2패 조는 지면 탈락이다.
한국팀(T1, 젠지, 한화생명e스포츠, 디플러스 기아) 대진과 일정을 중심으로 쓰고, 나머지 대진도 간단히 정리하라.
JSON 하나만: {{"title": "45자 이내", "tldr": ["서로 다른 정보 3줄, 기사체"], "why": "한 문장", "body": ["2~3문단"],
 "hook": "8~16자", "title_en": "...", "tldr_en": ["..."], "hook_en": "..."}}

대진(한국 시간): {matches}
현재 전적: {rec}"""


def make_draw_article(stage="Swiss_Stage"):
    """다음 라운드 대진이 확정되면 전체 대진표 + 기사."""
    import webzine
    webzine.load_env()
    ms = stage_matches(stage)
    up = [m for m in ms if not m["done"]]
    if not up:
        print("롤드컵: 확정된 다음 대진 없음")
        return None
    rnd = re.search(r"Round (\d+)", up[0]["round"] or "")
    rnd = rnd.group(1) if rnd else "?"
    cur = [m for m in up if re.search(rf"Round {rnd}\b", m["round"] or "")]
    state = webzine.load_json(DATA / "esports.json", {})
    key = f"draw-{stage}-{rnd}"
    if key in state.get("draws", []):
        print("롤드컵: 이미 올린 대진")
        return None
    aid = "worlds26-" + key.lower().replace("_", "")
    img = render_bracket(cur, f"스위스 {rnd}라운드 대진표", MEDIA / f"{aid}.jpg")
    cov = render_cover(cur, MEDIA / f"{aid}_cover.jpg")
    allrec = {}
    for m in ms:  # 전체 팀 전적 (한국팀만이 아니라)
        if not m["done"]:
            continue
        win1 = int(m["s1"]) > int(m["s2"])
        for t, w in ((m["t1"], win1), (m["t2"], not win1)):
            a_, b_ = allrec.get(kname(t), (0, 0))
            allrec[kname(t)] = (a_ + w, b_ + (not w))
    fmt = lambda m: f"{datetime.fromtimestamp(m['ts'], KST):%m/%d %H:%M} [{GROUP_KO.get((m['round'] or ' ').split()[-1], '')}, Bo{m.get('bo')}] {kname(m['t1'])} vs {kname(m['t2'])}"
    r = webzine.llm(DRAW_PROMPT.format(today=datetime.now(KST).strftime("%Y년 %m월 %d일"), rnd=rnd,
                                       matches="; ".join(fmt(m) for m in cur),
                                       rec=", ".join(f"{k} {w}승 {l}패" for k, (w, l) in allrec.items())))
    if not r or not r.get("title"):
        return None
    arts = webzine.load_json(DATA / "articles.json", [])
    a = {"id": aid, "title": r["title"], "tldr": r.get("tldr", [])[:3], "why": r.get("why", ""), "body": r.get("body", []),
         "hook": r.get("hook"), "title_en": r.get("title_en"), "tldr_en": r.get("tldr_en"), "hook_en": r.get("hook_en"),
         "category": "e스포츠", "platforms": ["PC"], "games": ["리그 오브 레전드"], "games_en": ["League of Legends"],
         "trust": "확인", "status": "published", "ts": time.time(), "written": time.time(),
         "img": f"media/esports/{cov.name}", "img_hd": f"media/esports/{cov.name}", "img_src": "알트탭 (로고: Liquipedia)",
         "gallery": [{"src": f"media/esports/{img.name}", "credit": "알트탭 (자료: Liquipedia)"}],
         "sources": [{"name": "Liquipedia", "title": f"2026 World Championship - {stage.replace('_', ' ')}",
                      "link": f"{BASE}/leagueoflegends/{EVENT}/{stage}", "official": False}],
         "esports": True}
    webzine.dedupe_summary(a)
    arts.append(a)
    webzine.save_json(DATA / "articles.json", arts)
    state.setdefault("draws", []).append(key)
    webzine.save_json(DATA / "esports.json", state)
    print(f"롤드컵 대진 기사: {a['title']}")
    return a


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


# ---------- 로스터 · 전적표 기사 ----------
POS = ["TOP", "JGL", "MID", "ADC", "SUP"]


def kr_rosters():
    """참가팀 섹션에서 한국팀 로스터 (선수 5명 + 감독)와 시드."""
    h = api(action="parse", page=EVENT, prop="text", section=2)["parse"]["text"]["*"]
    t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)))
    out = []
    for full in ["Gen.G Esports", "Hanwha Life Esports", "T1", "Dplus", "KT Rolster"]:
        m = re.search(re.escape(full) + r" Qualified (.+?) Player roster .+? Main Staff (.+?) Head Coach", t)
        if not m:
            continue
        toks = [p for p in re.sub(r"\bx\d+\b|\bCaptain\b", " ", m.group(2)).split() if p]
        # 'Main Staff' 뒤: 선수 5명 다음 이름이 감독 (이름 → 직함 순서로 적혀 있다)
        out.append({"name": full, "seed": m.group(1).strip(), "players": toks[:5], "coach": toks[5] if len(toks) > 5 else ""})
    return out


def render_rosters(rosters, logos, out):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((76, 80), "LoL 월드 챔피언십 2026", font=NB(34), fill=RED)
    d.text((76, 130), "LCK 대표팀 로스터", font=BH(64), fill=WHITE)
    card_h = (H - 300 - 110) // max(1, len(rosters)) - 16
    y = 260
    for r in rosters:
        d.rounded_rectangle((76, y, W - 76, y + card_h), 18, fill=CARD)
        lg = logo(logos.get(r["name"]), 84)
        if lg:
            img.paste(lg, (106, y + 24), lg)
        d.text((210, y + 30), KR.get(r["name"], r["name"]), font=BH(44), fill=WHITE)
        seed = r["seed"].replace("LCK 2026", "LCK").replace("MSI 2026", "MSI").replace("1st", "1위").replace("2nd", "2위") \
            .replace("3rd", "3위").replace("4th", "4위")
        d.text((W - 106, y + 44), seed + " 자격", font=NM(28), fill=DIM, anchor="ra")
        cw = (W - 152 - 60) / 5
        for i, (pos, pl) in enumerate(zip(POS, r["players"])):
            cx = 106 + cw * i + cw / 2
            d.text((cx, y + card_h - 74), pos, font=NB(22), fill=RED, anchor="mm")
            pf = NB(34) if len(pl) <= 7 else NB(28)
            d.text((cx, y + card_h - 36), pl, font=pf, fill=WHITE, anchor="mm")
        d.text((W - 106, y + 84), f"감독 {r['coach']}", font=NR(24), fill=DIM, anchor="ra")
        y += card_h + 16
    d.text((76, H - 70), "자료: Liquipedia (CC BY-SA 3.0)", font=NR(24), fill=DIM)
    d.text((W - 56, H - 70), "@webzinealttab", font=NB(28), fill=RED, anchor="ra")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out


def standings(ms):
    rec, logos = {}, {}
    for m in ms:
        for t in (m["t1"], m["t2"]):
            logos[t["name"]] = t
        if not m["done"]:
            continue
        w1 = int(m["s1"]) > int(m["s2"])
        for t, w in ((m["t1"], w1), (m["t2"], not w1)):
            a_, b_ = rec.get(t["name"], (0, 0))
            rec[t["name"]] = (a_ + w, b_ + (not w))
    return sorted(rec.items(), key=lambda r: (-r[1][0], r[1][1], r[0])), logos


def render_standings(table, logos, title, out):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((76, 80), "LoL 월드 챔피언십 2026", font=NB(34), fill=RED)
    d.text((76, 130), title, font=BH(60), fill=WHITE)
    groups = {}
    for name, (w, l) in table:
        groups.setdefault((w, l), []).append(name)
    y = 250
    colw = (W - 152 - 20) // 2
    for (w, l), names in groups.items():
        lab = {(2, 0): "2승 0패 · 다음 경기 이기면 8강", (0, 2): "0승 2패 · 다음 경기 지면 탈락"}.get((w, l), f"{w}승 {l}패")
        d.text((76, y), lab, font=NB(30), fill=GOLD if w > l else DIM if l > w else SOFT)
        y += 50
        for i, n in enumerate(names):
            x = 76 + (i % 2) * (colw + 20)
            if i % 2 == 0 and i:
                y += 84
            kr = n in KR
            d.rounded_rectangle((x, y, x + colw, y + 72), 14, fill=CARD, outline=RED if kr else None, width=2)
            t = logos.get(n, {})
            lg = logo(t.get("logo"), 46)
            if lg:
                img.paste(lg, (x + 20, y + 36 - lg.height // 2), lg)
            d.text((x + 84, y + 36), KR.get(n, t.get("short", n)), font=NB(30) if kr else NM(28),
                   fill=WHITE if kr else SOFT, anchor="lm")
            d.text((x + colw - 20, y + 36), f"{w}-{l}", font=ANTON(36), fill=WHITE, anchor="rm")
        y += 84 + 22
    d.text((76, H - 70), "자료: Liquipedia (CC BY-SA 3.0)", font=NR(24), fill=DIM)
    d.text((W - 56, H - 70), "@webzinealttab", font=NB(28), fill=RED, anchor="ra")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return out


INFO_PROMPT = """너는 한국 게임 매체의 e스포츠 기자다. 오늘은 {today}. 아래 데이터만으로 기사를 써라 (선수 발언·경기 내용 지어내기 금지).
주제: {topic}
팀 이름은 국내 표기(T1, 젠지, 한화생명e스포츠, 디플러스 기아), 선수는 영문 닉네임 그대로(Faker, Chovy 등) 쓴다.
JSON 하나만: {{"title": "45자 이내", "tldr": ["서로 다른 정보 3줄, 기사체"], "why": "한 문장", "body": ["2~3문단"],
 "hook": "8~16자", "title_en": "...", "tldr_en": ["..."], "hook_en": "..."}}

데이터: {data}"""


def _save_info_article(aid, r, cover, table_img, stage_link):
    import webzine
    arts = webzine.load_json(DATA / "articles.json", [])
    if any(a["id"] == aid for a in arts):
        return None
    a = {"id": aid, "title": r["title"], "tldr": r.get("tldr", [])[:3], "why": r.get("why", ""), "body": r.get("body", []),
         "hook": r.get("hook"), "title_en": r.get("title_en"), "tldr_en": r.get("tldr_en"), "hook_en": r.get("hook_en"),
         "category": "e스포츠", "platforms": ["PC"], "games": ["리그 오브 레전드"], "games_en": ["League of Legends"],
         "trust": "확인", "status": "published", "ts": time.time(), "written": time.time(),
         "img": f"media/esports/{cover.name}", "img_hd": f"media/esports/{cover.name}", "img_src": "알트탭 (로고: Liquipedia)",
         "gallery": [{"src": f"media/esports/{table_img.name}", "credit": "알트탭 (자료: Liquipedia)"}],
         "sources": [{"name": "Liquipedia", "title": "2026 World Championship", "link": stage_link, "official": False}],
         "esports": True}
    webzine.dedupe_summary(a)
    arts.append(a)
    webzine.save_json(DATA / "articles.json", arts)
    print(f"롤드컵 기사: {a['title']}")
    return a


def make_info_articles():
    import webzine
    webzine.load_env()
    today = datetime.now(KST).strftime("%Y년 %m월 %d일")
    ms = stage_matches("Swiss_Stage")
    done_rounds = sorted({re.search(r"Round (\d+)", m["round"]).group(1) for m in ms if m["done"] and m["round"]})
    made = []
    # ① 전적표 (마지막으로 끝난 라운드 기준)
    if done_rounds:
        rnd = done_rounds[-1]
        table, tl = standings(ms)
        aid = f"worlds26-standings-r{rnd}"
        img = render_standings(table, tl, f"스위스 {rnd}라운드 종료 전적", MEDIA / f"{aid}.jpg")
        cov = render_cover([m for m in ms if m["done"]], MEDIA / f"{aid}_cover.jpg")
        r = webzine.llm(INFO_PROMPT.format(today=today, topic=f"스위스 스테이지 {rnd}라운드 종료 시점 16팀 전적 정리 (한국팀 중심)",
                                           data=", ".join(f"{KR.get(n, tl[n]['short'])} {w}승 {l}패" for n, (w, l) in table)
                                           + " / 규칙: 3승 진출, 3패 탈락"))
        if r and r.get("title"):
            made.append(_save_info_article(aid, r, cov, img, f"{BASE}/leagueoflegends/{EVENT}/Swiss_Stage"))
    # ② 한국팀 로스터 — 필요 없다는 피드백으로 자동 생성은 끔 (함수는 남겨 둠)
    ros = kr_rosters() if False else []
    if ros:
        tl = {m[k]["name"]: m[k]["logo"] for m in ms for k in ("t1", "t2")}
        aid = "worlds26-lck-rosters"
        img = render_rosters(ros, tl, MEDIA / f"{aid}.jpg")
        cov = render_cover([m for m in ms if m["done"]], MEDIA / f"{aid}_cover.jpg")
        data = "; ".join(f"{KR.get(r_['name'], r_['name'])}({r_['seed']} 자격): " +
                         ", ".join(f"{p} {pl}" for p, pl in zip(POS, r_["players"])) + f", 감독 {r_['coach']}" for r_ in ros)
        r = webzine.llm(INFO_PROMPT.format(today=today, topic="롤드컵 2026에 나선 LCK 대표 4팀 로스터 총정리", data=data))
        if r and r.get("title"):
            made.append(_save_info_article(aid, r, cov, img, f"{BASE}/leagueoflegends/{EVENT}"))
    return [m for m in made if m]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1:] == ["table"]:
        ms = korean(stage_matches("Swiss_Stage"))
        for m in ms:
            print(datetime.fromtimestamp(m["ts"], KST).strftime("%m/%d %H:%M"), m["round"], kname(m["t1"]), m["s1"], ":", m["s2"], kname(m["t2"]), m["done"])
        print(render_table(ms, "스위스 스테이지", ROOT / "out" / "worlds_table.jpg"))
    else:
        make_article()
        make_draw_article()
        make_info_articles()  # 라운드 종료 전적표(라운드마다 1번)
