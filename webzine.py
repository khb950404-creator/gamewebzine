"""게임 웹진 자동 파이프라인: 수집 → 이슈 묶기 → 기사 작성(LLM) → 정적 사이트 빌드.

사용법:
  python webzine.py            # 전체 실행
  python webzine.py build      # 사이트만 다시 빌드
  python webzine.py review     # 검수 대기 기사 목록
  python webzine.py approve ID [ID...] / reject ID [ID...]

LLM 선택 (.env 또는 환경변수):
  LLM=gemini  GEMINI_API_KEY=...   # 무료 티어 (aistudio.google.com 에서 키 발급, 카드 불필요)
  LLM=ollama  OLLAMA_MODEL=qwen2.5:7b  # 로컬 무료
  LLM=none                         # LLM 없이 원문 제목/요약만 모음
"""
import json, os, re, sys, html, hashlib, time, urllib.request, urllib.error, urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "site"
UA = {"User-Agent": "Mozilla/5.0 (GameWebzine RSS reader)"}

# official=True 는 1차 출처(플랫폼/퍼블리셔 공식)
FEEDS = [
    ("PlayStation Blog", "https://blog.playstation.com/feed/", True),
    ("Xbox Wire", "https://news.xbox.com/en-us/feed/", True),
    ("IGN", "https://feeds.ign.com/ign/news", False),
    ("GameSpot", "https://www.gamespot.com/feeds/news/", False),
    ("VGC", "https://www.videogameschronicle.com/feed/", False),
    ("PC Gamer", "https://www.pcgamer.com/rss/", False),
    ("Eurogamer", "https://www.eurogamer.net/feed", False),
    ("Nintendo Life", "https://www.nintendolife.com/feeds/latest", False),
]
MAX_ARTICLES_PER_RUN = int(os.environ.get("MAX_ARTICLES", 8))  # 무료 쿼터 보호
CATEGORIES = ["신작", "업데이트", "업계", "하드웨어", "할인·이벤트", "e스포츠", "기타"]


def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# ---------- 수집 ----------
def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def fetch_feed(name, url, official):
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read()
        root = ET.fromstring(raw)
    except Exception as e:
        print(f"  ! {name}: {e}")
        return []
    items = []
    for it in root.iter("item"):
        title = strip_html(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            ts = time.time()
        items.append({
            "id": hashlib.md5(link.encode()).hexdigest()[:12],
            "source": name, "official": official, "title": title, "link": link,
            "summary": strip_html(it.findtext("description"))[:600], "ts": ts,
        })
    print(f"  {name}: {len(items)}건")
    return items


# ---------- 이슈 묶기 ----------
STOP = set("the a an of to in and for on with is at by from new now this how why what your you it its are be as all".split())


def tokens(t):
    return {w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOP and len(w) > 2}


def cluster(items):
    clusters = []
    for it in sorted(items, key=lambda x: -x["ts"]):
        tk = tokens(it["title"])
        for c in clusters:
            inter = len(tk & c["tokens"])  # 대표(첫) 제목과만 비교해 묶음이 번지지 않게
            if tk and (inter >= 4 or (inter >= 2 and inter / len(tk | c["tokens"]) >= 0.4)):
                c["items"].append(it)
                break
        else:
            clusters.append({"items": [it], "tokens": tk})
    for c in clusters:
        srcs = {i["source"] for i in c["items"]}
        age_h = (time.time() - max(i["ts"] for i in c["items"])) / 3600
        c["score"] = len(srcs) * 2 + (3 if any(i["official"] for i in c["items"]) else 0) - age_h / 12
    return sorted(clusters, key=lambda c: -c["score"])


# ---------- LLM ----------
# Gemini 무료 한도는 "모델별 하루 요청 수"(flash 는 하루 20회)라서 기사 여러 건을 한 번의 호출로 묶어 쓴다.
# 실행 1회 = 작성 1회 + 팩트체크 1회.
exhausted = set()  # 이번 실행에서 하루 한도가 끝난 모델


def llm(prompt, prefer_lite=False):
    mode = os.environ.get("LLM", "gemini" if os.environ.get("GEMINI_API_KEY") else "none")
    if mode == "gemini":
        models = [m for m in (os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-flash-lite-latest") if m]
        if prefer_lite:
            models = ["gemini-flash-lite-latest", "gemini-flash-latest"]
        for model in models:
            if model in exhausted:
                continue
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                data=json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                                 "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
            for attempt in range(3):
                try:
                    t = time.time()
                    r = json.loads(urllib.request.urlopen(req, timeout=300).read())
                    print(f"    ({model}, {time.time() - t:.0f}초)")
                    return json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
                except urllib.error.HTTPError as e:
                    msg = e.read().decode("utf-8", "ignore")
                    if e.code == 429 and "PerDay" in msg:  # 하루 한도 소진 → 재시도 무의미, 다음 모델로
                        print(f"    … {model} 오늘 무료 한도 소진, 다음 모델로")
                        exhausted.add(model)
                        break
                    if e.code not in (429, 500, 503):
                        raise RuntimeError(f"{model} {e.code}: {msg[:300]}")
                    print(f"    … {model} {e.code}, {10 * (attempt + 1)}초 후 재시도")
                    time.sleep(10 * (attempt + 1))
        raise RuntimeError("모든 Gemini 모델이 응답하지 않음 (혼잡 또는 무료 한도 소진)")
    if mode == "ollama":
        req = urllib.request.Request(
            os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/generate",
            data=json.dumps({"model": os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"), "prompt": prompt,
                             "format": "json", "stream": False}).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(json.loads(urllib.request.urlopen(req, timeout=1800).read())["response"])
    return None


PROMPT = """너는 PC·콘솔 게이머를 위한 한국어 게임 웹진 기자다.
아래 이슈들 각각에 대해 기사를 하나씩 써라. 각 기사는 해당 이슈의 출처 자료만 근거로 한다.
자료에 없는 사실(날짜, 가격, 플랫폼, 수치)은 절대 지어내지 마라.
원문 문장을 번역해 옮기지 말고, 여러 출처를 종합해 자신의 문장으로 새로 써라.

신뢰도 규칙:
- "공식": 공식 출처(official=true)가 직접 발표했거나, 매체가 회사의 공식 발표를 전한 경우
- "확인": 공식 발표는 아니지만 2개 이상 매체가 보도
- "루머": 단일 매체의 독자 보도, 유출, 소문, 익명 소식통

PC·콘솔 비디오게임과 무관하거나(보드·카드게임, 게임과 연결 약한 영화·드라마 소식 등)
광고/딜 나열/퀴즈/팟캐스트 같은 저가치 글은 {{"issue": 번호, "skip": true}} 로만 표시하라.

JSON 하나만 출력:
{{"articles": [
 {{"issue": 이슈 번호,
  "title": "한국어 헤드라인(40자 이내, 낚시 금지)",
  "tldr": ["핵심 요약 1", "핵심 요약 2", "핵심 요약 3"],
  "why": "PC·콘솔 게이머에게 왜 중요한가 (1~2문장)",
  "body": ["문단1", "문단2", "문단3"],
  "category": "{cats} 중 하나",
  "platforms": ["PC","PS5","Xbox","Switch" 중 출처에 명시된 것만],
  "games": ["관련 게임명"],
  "games_en": ["관련 게임의 영문 공식 제목 (Steam 검색용, 출처에 나온 표기 그대로)"],
  "trust": "공식|확인|루머"}}
]}}

{issues}"""


VERIFY = """너는 게임 웹진의 팩트체커다. 아래 기사들을 각 이슈의 출처 자료와 한 문장씩 대조하라.
출처에 근거가 없는 주장(추측, 배경 설명으로 지어낸 사실, 틀린 날짜·수치·플랫폼)은 삭제하거나 출처에 맞게 고쳐라.
문체와 구조는 유지하고, 문제없는 문장은 건드리지 마라. trust 값도 규칙에 맞는지 확인하라
(공식=회사 공식 발표, 확인=2개 이상 매체, 루머=단일 매체 독자 보도·유출·소문).

JSON 하나만 출력 (입력의 모든 기사를 같은 issue 번호로):
{{"articles": [{{"issue": 번호, "problems": ["발견한 문제 설명"], "title": ..., "tldr": [...], "why": ..., "body": [...],
  "category": ..., "platforms": [...], "games": [...], "trust": ...}}]}}

{issues}

기사:
{articles}"""


class LLMDown(Exception):
    pass


def issue_text(n, c):
    return f"=== 이슈 {n} ===\n" + "\n".join(
        f"- {s['source']} (official={str(s['official']).lower()})\n  제목: {s['title']}\n  요약: {s['summary']}"
        for s in c["items"][:6])


def write_batch(cands):
    """후보 이슈들을 한 번에 작성 → 한 번에 팩트체크. 반환: 기사 목록(이슈 순서)."""
    issues = "\n\n".join(issue_text(n, c) for n, c in enumerate(cands, 1))
    try:
        res = llm(PROMPT.format(cats=", ".join(CATEGORIES), issues=issues))
    except Exception as e:
        print(f"  ! LLM 오류: {e}")
        raise LLMDown()
    if res is None:  # LLM 없음 모드
        res = {"articles": [{"issue": n, "title": c["items"][0]["title"], "tldr": [], "why": "",
                             "body": [c["items"][0]["summary"]], "category": "기타", "platforms": [], "games": [],
                             "trust": "공식" if c["items"][0]["official"] else ("확인" if len(c["items"]) > 1 else "루머"),
                             "untranslated": True} for n, c in enumerate(cands, 1)]}
    drafts = {a["issue"]: a for a in res.get("articles", [])
              if isinstance(a, dict) and isinstance(a.get("issue"), int) and not a.get("skip") and a.get("title")}
    print(f"  초안 {len(drafts)}건 (건너뜀 {len(cands) - len(drafts)}건)")

    checked = {}
    if drafts and not any(a.get("untranslated") for a in drafts.values()):
        try:
            v = llm(VERIFY.format(issues="\n\n".join(issue_text(n, cands[n - 1]) for n in drafts),
                                  articles=json.dumps(list(drafts.values()), ensure_ascii=False)))
            checked = {a["issue"]: a for a in (v or {}).get("articles", []) if isinstance(a, dict) and a.get("title")}
            print(f"  팩트체크 수정 {sum(len(a.get('problems', [])) for a in checked.values())}건")
        except Exception as e:
            print(f"  ! 팩트체크 실패, 전부 검수 대기로 보냄: {e}")

    out = []
    for n, a in sorted(drafts.items()):
        c = cands[n - 1]
        if n in checked:
            fixes = checked[n].pop("problems", [])
            a = {**a, **checked[n], "fixes": fixes}
        elif not a.get("untranslated"):
            a["fixes"] = ["팩트체크 미완료"]; a["force_review"] = True
        srcs = c["items"][:6]
        review_all = os.environ.get("REVIEW_ALL") == "1"
        a["status"] = "pending" if review_all or a.get("force_review") or a.get("trust") == "루머" else "published"
        a["id"] = hashlib.md5("".join(sorted(s["id"] for s in srcs)).encode()).hexdigest()[:10]
        a["sources"] = [{"name": s["source"], "title": s["title"], "link": s["link"], "official": s["official"]}
                        for s in srcs]
        a["ts"] = max(s["ts"] for s in srcs)
        a["written"] = time.time()
        a.pop("issue", None)
        out.append(a)
    return out


# ---------- 저장 ----------
def load_json(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def run():
    seen = set(load_json(DATA / "seen.json", []))
    articles = load_json(DATA / "articles.json", [])
    print("수집 중...")
    items = [i for name, url, off in FEEDS for i in fetch_feed(name, url, off)]
    fresh = [i for i in items if i["id"] not in seen and time.time() - i["ts"] < 3 * 86400]
    print(f"새 항목 {len(fresh)}건")
    # 건너뛰는 이슈를 감안해 여유 있게 후보를 뽑고, 작성된 것 중 상위 MAX 건만 쓴다
    cands = cluster(fresh)[:MAX_ARTICLES_PER_RUN + 4]
    for c in cands:
        print(f"후보: {c['items'][0]['title'][:70]} (출처 {len(c['items'])}, 점수 {c['score']:.1f})")
    failed, new = False, []
    if cands:
        try:
            new = write_batch(cands)[:MAX_ARTICLES_PER_RUN]
            for c in cands:  # 건너뛴 이슈도 다시 보지 않음
                seen.update(i["id"] for i in c["items"])
        except LLMDown:
            print("LLM 호출 실패 - 이번 후보는 다음 실행 때 재시도")
            failed = True
    articles += new
    cache = load_json(DATA / "steam_apps.json", {})
    for step in (lambda: attach_images(articles, cache), lambda: collect_patches(cache), lambda: collect_calendar(cache)):
        try:
            step()
        except Exception as e:  # 부가 기능 실패가 기사 저장을 막지 않게
            print(f"  ! {e}")
    save_json(DATA / "steam_apps.json", cache)
    save_json(DATA / "seen.json", sorted(seen))
    save_json(DATA / "articles.json", articles)
    print(f"기사 {len(new)}건 작성")
    for a in new:
        print(f"  [{a['status']}] ({a['trust']}) {a['title']}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")  # Actions 실행 화면에 결과 표시
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### 새 기사 {len(new)}건{' (LLM 오류로 중단)' if failed else ''}\n")
            for a in new:
                f.write(f"- [{a['status']}] ({a['trust']}) {a['title']}\n")
    if failed:
        sys.exit(1)


# ---------- Steam: 게임 이미지 · 패치노트 ----------
# 이미지는 Steam 스토어 공식 헤더 이미지만 사용(퍼블리셔 제공 홍보 이미지). 이름이 확실히 일치할 때만 붙인다.
def get_json(url, timeout=20):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())


def norm(s):
    return re.sub(r"[^0-9a-z가-힣]", "", (s or "").lower().replace("&", "and"))


def steam_app(appid, cache):
    """appid → {name, img}. data/steam_apps.json 에 캐시."""
    key = f"id:{appid}"
    if key not in cache:
        try:
            d = get_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=kr&l=koreana")[str(appid)]
            d = d.get("data") if d.get("success") else None
            cache[key] = {"appid": appid, "name": d["name"], "img": d.get("header_image")} if d else None
            time.sleep(0.3)
        except Exception:
            return None
    return cache[key]


def steam_find(title, cache):
    """게임 이름 → Steam 앱. 이름이 정확히(또는 거의) 같을 때만 반환."""
    key = "q:" + norm(title)
    if not norm(title):
        return None
    if key not in cache:
        cache[key] = None
        try:
            res = get_json("https://store.steampowered.com/api/storesearch/?cc=kr&l=english&term="
                           + urllib.parse.quote(title))
            time.sleep(0.3)
            for it in res.get("items", [])[:5]:
                a, b = norm(it["name"]), norm(title)
                if a == b:  # 시리즈 다른 편 이미지가 붙지 않도록 정확히 같은 이름만
                    app = steam_app(it["id"], cache)
                    cache[key] = app["appid"] if app else None
                    break
        except Exception:
            del cache[key]  # 네트워크 오류는 다음에 재시도
            return None
    return cache.get(f"id:{cache[key]}") if cache.get(key) else None


def attach_images(articles, cache):
    n = 0
    for a in articles:
        if "img" in a:
            continue
        a["img"] = None
        for g in a.get("games_en") or a.get("games") or []:
            app = steam_find(g, cache)
            if app and app.get("img"):
                a["img"], a["appid"] = app["img"], app["appid"]
                n += 1
                break
    print(f"이미지 연결 {n}건")


PATCH_RE = re.compile(r"patch|update|hotfix|fix|업데이트|패치", re.I)


def strip_bbcode(s):
    s = re.sub(r"\{STEAM_CLAN_IMAGE\}\S*", " ", s or "")
    s = re.sub(r"\[\*\]", "\n- ", s)
    s = re.sub(r"\[/?[a-z0-9]+(=[^\]]*)?\]", " ", s, flags=re.I)
    return re.sub(r"[ \t]+", " ", strip_html(s) if "<" in s else s).strip()


PATCH_PROMPT = """너는 한국어 게임 웹진 편집자다. 아래 Steam 패치노트들을 각각 한국 게이머가 읽기 쉽게 요약하라.
패치노트에 없는 내용은 절대 쓰지 마라. 핵심 변경 위주로 최대 4줄, 각 줄 40자 안팎.
게임 플레이와 무관한 공지(이벤트 홍보만, 판매 공지 등)면 skip: true.
JSON 하나만 출력: {{"patches": [{{"n": 번호, "title": "한국어 제목(예: 5.2 업데이트 — 신규 맵 추가)", "points": ["..."], "skip": false}}]}}

{notes}"""


def collect_patches(cache):
    """Steam 동시접속 상위 게임의 최근 패치노트를 모아 한국어 요약(LLM 1회)."""
    p = DATA / "patches.json"
    patches = load_json(p, [])
    have = {x["gid"] for x in patches}
    try:
        ranks = get_json("https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/")["response"]["ranks"]
    except Exception as e:
        print(f"  ! Steam 차트: {e}")
        return
    new = []
    for r in ranks[:int(os.environ.get("PATCH_TOP", 40))]:
        try:
            items = get_json(f"https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid={r['appid']}"
                             "&count=5&maxlength=0&feeds=steam_community_announcements")["appnews"].get("newsitems", [])
        except Exception:
            continue
        for it in items:
            if it["gid"] in have or time.time() - it["date"] > 7 * 86400:
                continue
            if "patchnotes" not in (it.get("tags") or []) and not PATCH_RE.search(it["title"]):
                continue
            app = steam_app(r["appid"], cache)
            if not app:
                continue
            new.append({"gid": it["gid"], "appid": r["appid"], "game": app["name"], "img": app["img"],
                        "orig_title": it["title"], "url": it["url"], "ts": it["date"],
                        "text": strip_bbcode(it["contents"])[:2500]})
        time.sleep(0.2)
    new = sorted(new, key=lambda x: -x["ts"])[:15]
    print(f"새 패치노트 {len(new)}건")
    if not new:
        return
    notes = "\n\n".join(f"=== {i} === {x['game']} — {x['orig_title']}\n{x['text']}" for i, x in enumerate(new, 1))
    try:
        # 패치 요약은 가벼운 작업이라 lite 모델 우선 (flash 하루 한도는 기사용으로 아낀다)
        res = llm(PATCH_PROMPT.format(notes=notes), prefer_lite=True) or {}
        done = {x["n"]: x for x in res.get("patches", []) if isinstance(x, dict) and isinstance(x.get("n"), int)}
    except Exception as e:
        print(f"  ! 패치 요약 실패 (다음 실행 때 재시도): {e}")
        return
    for i, x in enumerate(new, 1):
        s = done.get(i)
        if not s:
            continue
        x.pop("text")
        if s.get("skip"):
            x["skip"] = True
        else:
            x["title"], x["points"] = s.get("title") or x["orig_title"], s.get("points", [])
        patches.append(x)
    save_json(p, sorted(patches, key=lambda x: -x["ts"])[:300])


# ---------- 출시 캘린더 (위키백과 출시 목록, CC BY-SA) ----------
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
PLAT_MAP = {"WIN": "PC", "PS5": "PS5", "XBX/S": "Xbox", "NS2": "Switch 2", "NS": "Switch"}


def wiki_clean(s):
    s = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    for _ in range(4):  # 중첩 템플릿 제거
        s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"'{2,}", "", s)
    return re.sub(r"<[^>]+>", "", s).strip()


def wiki_releases(year):
    t = get_json("https://en.wikipedia.org/w/api.php?action=parse&format=json&prop=wikitext&page="
                 f"List_of_video_games_released_in_{year}", timeout=60)["parse"]["wikitext"]["*"]
    t = re.sub(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", t, flags=re.S)
    out, date = [], None
    for row in t.split("\n|-"):
        lines = [l for l in row.split("\n") if l.startswith("|") and not l.startswith(("|}", "|+", "{|"))]
        cells = []
        for l in lines:
            cells += [c for c in l[1:].split("||")]
        cells = [c.split(" | ")[-1] if re.match(r'\s*(rowspan|colspan|style|class)\s*=', c) else c for c in cells]
        cells = [wiki_clean(c) for c in cells]
        if len(cells) < 3:
            continue
        m = re.fullmatch(r"(%s) (\d{1,2})" % "|".join(MONTHS), cells[0])
        if m:
            date, cells = f"{year}-{MONTHS.index(m[1]) + 1:02d}-{int(m[2]):02d}", cells[1:]
        if not date or len(cells) < 2:
            continue
        title, plats = cells[0], [PLAT_MAP[p.strip()] for p in cells[1].split(",") if p.strip() in PLAT_MAP]
        if not title or not plats or re.search(r"\((JP|CN|KR|AS)\)$", title):  # 지역 한정 출시 제외
            continue
        out.append({"date": date, "title": title, "platforms": plats, "genre": cells[3] if len(cells) > 3 else ""})
    return out


def collect_calendar(cache):
    rel = []
    y = datetime.now().year
    for year in (y, y + 1):
        try:
            rel += wiki_releases(year)
        except Exception as e:
            print(f"  ! 위키 {year}: {e}")
    if not rel:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    horizon = datetime.fromtimestamp(time.time() + 120 * 86400).strftime("%Y-%m-%d")
    week_ago = datetime.fromtimestamp(time.time() - 7 * 86400).strftime("%Y-%m-%d")
    rel = [r for r in rel if week_ago <= r["date"] <= horizon]
    for r in rel:
        if "PC" in r["platforms"]:
            app = steam_find(r["title"], cache)
            if app:
                r["img"], r["appid"] = app["img"], app["appid"]
    save_json(DATA / "calendar.json", {"updated": today, "releases": rel})
    print(f"출시 캘린더 {len(rel)}건")


# ---------- 검수 ----------
def review(cmd, ids):
    p = DATA / "articles.json"
    articles = load_json(p, [])
    if cmd == "review":
        pend = [a for a in articles if a.get("status") == "pending"]
        for a in pend:
            print(f"[{a['id']}] ({a['trust']}) {a['title']}")
            for t in a.get("tldr", []):
                print(f"    - {t}")
            for f in a.get("fixes", []):
                print(f"    ! {f}")
            print(f"    출처: {', '.join(s['name'] for s in a['sources'])}")
        print(f"검수 대기 {len(pend)}건  (웹 검수: 사이트주소/review.html)")
        return
    for a in articles:
        if a["id"] in ids:
            a["status"] = "published" if cmd == "approve" else "rejected"
            print(f"{a['status']}: {a['title']}")
    save_json(p, articles)


# ---------- 사이트 빌드 ----------
CSS = """
:root{--bg:#f7f6f3;--fg:#16161a;--mut:#6b6b76;--line:#e3e1dc;--card:#fff;--acc:#e5484d;
--ok:#1f9d55;--warn:#d97706;--info:#2563eb}
@media(prefers-color-scheme:dark){:root{--bg:#0f0f12;--fg:#ededf0;--mut:#9696a3;--line:#26262d;--card:#17171c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.65 "Pretendard",-apple-system,"Segoe UI","Malgun Gothic",sans-serif}
a{color:inherit;text-decoration:none}.wrap{max-width:1080px;margin:0 auto;padding:0 16px}
header{border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
header .wrap{display:flex;align-items:center;gap:20px;height:56px}
.logo{font:700 20px/1 ui-monospace,"Cascadia Code",Consolas,monospace;letter-spacing:-.5px;display:inline-flex;align-items:center;flex:none;white-space:pre}
.logo .k,.logo em{display:inline-block;transition:transform .55s cubic-bezier(.6,-.45,.3,1.45);animation:typ .35s cubic-bezier(.2,.7,.2,1) backwards}
.logo em{font-style:normal;color:var(--acc);animation-delay:.12s}.logo .k2{animation-delay:.24s}
.logo:hover .k1{transform:translateX(4ch)}.logo:hover .k2{transform:translateX(-4ch)}.logo:hover em{transform:rotate(180deg)}
.logo .cur{width:.6ch;height:3px;background:var(--acc);margin:0 0 -6px 3px;animation:blink 1.05s steps(1) infinite .4s}
@keyframes typ{from{opacity:0;transform:translateY(7px)}}@keyframes blink{50%{opacity:0}}
nav{display:flex;gap:6px;overflow-x:auto}nav button{border:1px solid var(--line);background:none;color:var(--mut);
border-radius:99px;padding:4px 12px;font:inherit;font-size:13px;cursor:pointer;white-space:nowrap}
nav button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
h2.sec{font-size:13px;text-transform:uppercase;letter-spacing:1px;color:var(--mut);margin:32px 0 12px}
.brief{display:grid;gap:12px}.brief a{display:block;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:16px 18px}.brief a:hover{border-color:var(--mut)}
.brief .t{font-size:19px;font-weight:800;letter-spacing:-.3px;line-height:1.35}
.brief .w{color:var(--mut);font-size:14px;margin-top:4px}
.meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--mut);margin-bottom:6px}
.b{font-size:11px;font-weight:700;padding:1px 7px;border-radius:4px;border:1px solid}
.b.공식{color:var(--ok)}.b.확인{color:var(--info)}.b.루머{color:var(--warn)}
.stream{border-top:1px solid var(--line)}.stream a{display:grid;grid-template-columns:64px 1fr;gap:12px;
padding:12px 0;border-bottom:1px solid var(--line)}.stream .tm{color:var(--mut);font-size:13px;font-variant-numeric:tabular-nums}
.stream .t{font-weight:600}
article{max-width:680px;margin:40px auto}article h1{font-size:30px;line-height:1.3;letter-spacing:-.6px;margin:8px 0 20px}
.tldr{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--acc);border-radius:8px;padding:14px 18px}
.tldr ul{margin:0;padding-left:18px}.why{font-weight:600;margin:20px 0}
.src{margin-top:32px;font-size:14px;border-top:1px solid var(--line);padding-top:16px}.src a{text-decoration:underline}
.note{font-size:12px;color:var(--mut);margin-top:24px}
.ad{margin:24px 0;min-height:90px;text-align:center}
.ad.ph{border:1px dashed var(--line);border-radius:8px;color:var(--mut);font-size:12px;display:flex;align-items:center;justify-content:center}
.doc{max-width:680px;margin:40px auto}.doc h1{font-size:28px}.doc a{text-decoration:underline}
footer a{margin:0 8px;text-decoration:underline}
.gnb{display:flex;gap:16px;font-size:14px;font-weight:600;color:var(--mut);overflow-x:auto;white-space:nowrap}.gnb a:hover{color:var(--fg)}
.brief a.hi{display:grid;grid-template-columns:1fr 220px;gap:18px;align-items:center}
.th{width:100%;aspect-ratio:460/215;object-fit:cover;border-radius:8px;background:var(--line);display:block}
.stream a.hi{grid-template-columns:64px 1fr 120px}.stream .th{border-radius:6px}
.hero{margin:0 0 20px}.hero img{width:100%;border-radius:10px;display:block}.hero figcaption{font-size:12px;color:var(--mut);margin-top:6px}
.pt{display:grid;grid-template-columns:184px 1fr;gap:16px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.pt .g{font-size:13px;color:var(--mut)}.pt .t{font-weight:800;margin:2px 0 6px}.pt ul{margin:0;padding-left:18px;font-size:14px}
.pt .lk{font-size:12px;color:var(--mut);text-decoration:underline}
.day{display:grid;grid-template-columns:88px 1fr;gap:16px;border-top:1px solid var(--line);padding:14px 0}
.day .d{font-weight:800;font-size:20px;line-height:1.2}.day .d small{display:block;font-size:12px;color:var(--mut);font-weight:500}
.day.today .d{color:var(--acc)}.day.past{opacity:.55}.rel{display:flex;gap:12px;align-items:center;padding:6px 0}
.rel .th{width:120px;flex:none}.rel .nt{width:120px;flex:none;aspect-ratio:460/215;border-radius:8px;background:var(--line)}
.rel .t{font-weight:700}.rel .s{font-size:12px;color:var(--mut)}
@media(max-width:640px){.brief a.hi{grid-template-columns:1fr}.brief a.hi .th{order:-1}
.stream a.hi{grid-template-columns:52px 1fr 88px}.pt{grid-template-columns:1fr}.day{grid-template-columns:64px 1fr}
.rel .th,.rel .nt{width:88px}header .wrap{gap:12px}}
header{background:color-mix(in srgb,var(--bg) 82%,transparent);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}
#pg{position:fixed;top:0;left:0;height:3px;width:0;background:var(--acc);z-index:20;transition:width .1s linear}
.rv{opacity:0;transform:translateY(18px);transition:opacity .6s ease,transform .7s cubic-bezier(.2,.7,.2,1)}
.rv.in{opacity:1;transform:none}
.brief a,.stream a,.pt,.rel{transition:transform .25s ease,box-shadow .25s ease,border-color .25s,background .25s}
.brief a:hover,.pt:hover{transform:translateY(-3px);box-shadow:0 14px 30px -16px rgba(0,0,0,.35)}
.th{transition:transform .5s cubic-bezier(.2,.7,.2,1),filter .3s}a:hover .th{transform:scale(1.03);filter:saturate(1.15)}
.stream a:hover .t,.brief a:hover .t{color:var(--acc)}
.tick{display:flex;align-items:center;gap:14px;border-bottom:1px solid var(--line);height:40px;overflow:hidden;font-size:13px;margin:0 0 4px}
.live{flex:none;font:700 11px ui-monospace,Consolas,monospace;letter-spacing:1px;color:var(--acc);display:flex;align-items:center;gap:7px}
.live::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--acc);animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(229,72,77,.55)}100%{box-shadow:0 0 0 10px rgba(229,72,77,0)}}
.tk{overflow:hidden;flex:1;-webkit-mask-image:linear-gradient(90deg,transparent,#000 4%,#000 96%,transparent);mask-image:linear-gradient(90deg,transparent,#000 4%,#000 96%,transparent)}
.tkin{display:flex;gap:40px;width:max-content;animation:mq 80s linear infinite}.tk:hover .tkin{animation-play-state:paused}
.tkin a{white-space:nowrap}.tkin a:hover{color:var(--acc)}.tkin b{color:var(--mut);font-weight:500;margin-right:8px;font-variant-numeric:tabular-nums}
@keyframes mq{to{transform:translateX(-50%)}}
.lead{position:relative;display:block;border-radius:16px;overflow:hidden;margin-bottom:12px;aspect-ratio:21/9;background:#111;color:#fff;isolation:isolate}
.lead img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;z-index:-2;animation:kb 20s ease-in-out infinite alternate}
@keyframes kb{from{transform:scale(1.04)}to{transform:scale(1.15) translate(-2%,-1.5%)}}
.lead::after{content:"";position:absolute;inset:0;z-index:-1;background:linear-gradient(180deg,rgba(0,0,0,0) 25%,rgba(0,0,0,.88));transition:opacity .4s}
.lead .lc{position:absolute;left:0;right:0;bottom:0;padding:28px}
.lead .t{font-size:32px;font-weight:900;letter-spacing:-.9px;line-height:1.22;color:#fff;max-width:760px}
.lead .w{color:rgba(255,255,255,.82);margin-top:8px;max-width:680px}.lead .meta{color:rgba(255,255,255,.75)}
.lead .t span{background:linear-gradient(transparent 62%,rgba(229,72,77,.75) 62%) no-repeat 0 0/0% 100%;transition:background-size .6s cubic-bezier(.2,.7,.2,1)}
.lead:hover .t span{background-size:100% 100%}
.new{display:inline-block;font:700 10px ui-monospace,Consolas,monospace;color:#fff;background:var(--acc);border-radius:3px;padding:1px 5px;margin-right:6px;vertical-align:2px;animation:pulse 1.6s infinite}
.day.today .d{position:relative}.day.today .d::before{content:"";position:absolute;left:-12px;top:6px;width:6px;height:6px;border-radius:50%;background:var(--acc);animation:pulse 1.6s infinite}
@media(max-width:640px){.lead{aspect-ratio:4/5}.lead .t{font-size:23px}.lead .lc{padding:18px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}.rv{opacity:1;transform:none}}
footer{color:var(--mut);font-size:12px;padding:40px 0;text-align:center}
@media(max-width:640px){article h1{font-size:24px}.brief .t{font-size:17px}}
"""


def esc(s):
    return html.escape(str(s or ""))


def page(title, body):
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>{CSS}</style>{ads_head()}</head><body>
<header><div class="wrap"><a class="logo" href="{{root}}index.html" aria-label="알트탭"><span class="k k1">alt</span><em>+</em><span class="k k2">tab</span><span class="cur"></span></a>
<div class="gnb"><a href="{{root}}index.html">뉴스</a><a href="{{root}}patches.html">패치노트</a><a href="{{root}}calendar.html">출시 캘린더</a></div></div></header>
<div id="pg"></div><div class="wrap">{body}</div><footer><div><a href="{{root}}about.html">소개</a><a href="{{root}}privacy.html">개인정보처리방침</a><a href="{{root}}contact.html">문의</a></div>
<p>AI가 공개 출처를 종합해 작성하고 팩트체크를 거친 기사입니다 · 원문은 각 기사 하단 출처 참고</p></footer>{MOTION_JS}</body></html>"""


MOTION_JS = """<script>(()=>{
if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;
const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target)}}),{rootMargin:'0px 0px -6% 0px'});
document.querySelectorAll('.lead,.brief>a,.stream>a,.pt,.day,article>*,.doc>*,h2.sec').forEach((el,i)=>{
 const r=el.getBoundingClientRect();el.classList.add('rv');el.style.transitionDelay=(r.top<innerHeight?Math.min(i,8)*55:0)+'ms';io.observe(el)});
const pg=document.getElementById('pg');
if(pg&&document.querySelector('article'))addEventListener('scroll',()=>{const h=document.documentElement;
 pg.style.width=Math.min(100,h.scrollTop/Math.max(1,h.scrollHeight-h.clientHeight)*100)+'%'},{passive:true});
})()</script>"""


def ads_head():
    c = os.environ.get("ADSENSE_CLIENT")
    return (f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={esc(c)}" '
            f'crossorigin="anonymous"></script>') if c else ""


def ad(slot_env):
    """광고 슬롯. ADSENSE_CLIENT 가 없으면 자리 표시만."""
    c, slot = os.environ.get("ADSENSE_CLIENT"), os.environ.get(slot_env, "")
    if not c:
        return '<div class="ad ph">광고 영역</div>'
    return (f'<div class="ad"><ins class="adsbygoogle" style="display:block" data-ad-client="{esc(c)}" '
            f'data-ad-slot="{esc(slot)}" data-ad-format="auto" data-full-width-responsive="true"></ins>'
            f'<script>(adsbygoogle=window.adsbygoogle||[]).push({{}});</script></div>')


SITE_NAME = "알트탭"


def static_pages():
    email = os.environ.get("CONTACT_EMAIL") or "contact@example.com"
    today = datetime.now().strftime("%Y년 %m월 %d일")
    pages = {
        "about": ("소개", f"""<p>{SITE_NAME}(ALT+TAB)은 업무 중 잠깐 Alt+Tab 하듯 확인하는 PC·콘솔 게이머를 위한 게임 뉴스 브리핑입니다.</p>
<p>공식 발표와 국내외 게임 매체의 보도를 수집해, 같은 소식을 하나의 이슈로 묶고 핵심 요약과
"왜 중요한가"를 붙여 전달합니다. 기사는 AI가 여러 출처를 종합해 작성하며, 별도의 팩트체크 단계에서
출처와 대조합니다. 루머성 소식은 편집자 검수를 거친 뒤에만 게시됩니다.</p>
<h2>신뢰도 표시</h2><ul><li><b>공식</b> — 플랫폼·개발사·퍼블리셔가 직접 발표</li>
<li><b>확인</b> — 2개 이상의 매체가 보도</li><li><b>루머</b> — 단일 매체 보도, 유출, 소문</li></ul>
<p>모든 기사 하단에 원문 출처 링크를 제공합니다. 오류 제보는 <a href="contact.html">문의</a>로 보내주세요.</p>"""),
        "privacy": ("개인정보처리방침", f"""<p>시행일: {today}</p>
<p>{SITE_NAME}(이하 "사이트")은 회원가입 없이 이용할 수 있으며, 이용자의 이름·연락처 등 개인정보를 직접 수집하지 않습니다.</p>
<h2>쿠키 및 광고</h2><p>사이트는 Google AdSense 광고를 게재할 수 있습니다. Google을 포함한 제3자 공급업체는 쿠키를 사용하여
이용자의 이전 방문 기록을 바탕으로 광고를 게재합니다. Google의 광고 쿠키 사용으로 Google 및 파트너는 이 사이트 및
다른 사이트 방문 정보를 기반으로 광고를 제공할 수 있습니다.</p>
<p>이용자는 <a href="https://www.google.com/settings/ads" target="_blank" rel="noopener">Google 광고 설정</a>에서 맞춤 광고를
해제하거나, <a href="https://www.aboutads.info" target="_blank" rel="noopener">aboutads.info</a>에서 제3자 공급업체의 쿠키 사용을
거부할 수 있습니다. 자세한 내용은 <a href="https://policies.google.com/technologies/ads" target="_blank" rel="noopener">Google 광고 정책</a>을 참고하세요.</p>
<h2>접속 기록</h2><p>호스팅 서비스가 보안 및 통계 목적으로 IP 주소, 브라우저 정보 등 접속 기록을 자동 수집할 수 있습니다.</p>
<h2>문의</h2><p>개인정보 관련 문의: {esc(email)}</p>"""),
        "contact": ("문의", f"""<p>기사 오류 제보, 보도 요청, 광고·제휴 문의는 아래 이메일로 보내주세요.</p>
<p><a href="mailto:{esc(email)}"><b>{esc(email)}</b></a></p>
<p>오류 제보 시 기사 주소와 정정 근거(출처 링크)를 함께 보내주시면 빠르게 반영하겠습니다.</p>"""),
    }
    for name, (title, body) in pages.items():
        (SITE / f"{name}.html").write_text(
            page(f"{title} — {SITE_NAME}", f'<div class="doc"><h1>{title}</h1>{body}</div>').replace("{root}", ""),
            encoding="utf-8")
    c = os.environ.get("ADSENSE_CLIENT")
    if c:
        (SITE / "ads.txt").write_text(f"google.com, {c.replace('ca-', '')}, DIRECT, f08c47fec0942fa0\n", encoding="utf-8")


def meta(a):
    plats = "".join(f"<span>{esc(p)}</span>" for p in a.get("platforms", []))
    return (f'<div class="meta"><span class="b {esc(a["trust"])}">{esc(a["trust"])}</span>'
            f'<span>{esc(a.get("category"))}</span>{plats}<span>· {fmt_time(a["ts"])}</span></div>')


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%m.%d %H:%M")


# 웹 검수 페이지: 브라우저에서 GitHub API 로 data/articles.json 을 직접 수정·커밋한다.
# 커밋(push)되면 워크플로가 사이트를 다시 빌드한다. 토큰은 이 브라우저(localStorage)에만 저장.
REVIEW_BODY = r"""
<style>
.rv-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:24px 0 8px}
.rv-bar input{flex:1;min-width:200px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit;font-size:14px}
.btn{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:7px 14px;font:inherit;font-size:14px;cursor:pointer}
.btn.pri{background:var(--fg);color:var(--bg);border-color:var(--fg)}.btn:disabled{opacity:.4;cursor:default}
.btn.ok.on{background:var(--ok);color:#fff;border-color:var(--ok)}.btn.no.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:12px}
.card.ok{border-color:var(--ok)}.card.no{opacity:.5}
.card .t{font-size:18px;font-weight:800;line-height:1.35;margin:4px 0 8px}
.card ul{margin:6px 0;padding-left:18px;font-size:14px}.card .fx{color:var(--warn);font-size:13px}
.card .sr{font-size:13px;color:var(--mut)}.card .sr a{text-decoration:underline}
.card .act{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}
.card select{padding:6px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit;font-size:13px}
#msg{font-size:14px;color:var(--mut);min-height:20px}
.tabs{display:flex;gap:6px;margin:16px 0}
.save{position:sticky;bottom:16px;display:flex;justify-content:flex-end;margin-top:16px}
</style>
<h2 class="sec">기사 검수</h2>
<div id="login">
  <p>GitHub 토큰을 한 번 입력하면 이 브라우저에만 저장됩니다.<br>
  <a style="text-decoration:underline" target="_blank" rel="noopener" href="https://github.com/settings/personal-access-tokens/new">토큰 만들기</a>
  → Repository access: <b>Only select repositories</b> → <b>__REPO__</b> 선택 → Permissions → Contents: <b>Read and write</b></p>
  <div class="rv-bar"><input id="tok" type="password" placeholder="github_pat_..."><button class="btn pri" id="go">연결</button></div>
</div>
<div id="app" hidden>
  <div class="tabs"><button class="btn pri" data-tab="pending">검수 대기 <span id="np"></span></button>
  <button class="btn" data-tab="published">게시된 기사</button>
  <button class="btn" id="logout" style="margin-left:auto">토큰 지우기</button></div>
  <div id="list"></div>
  <div class="save"><button class="btn pri" id="save" disabled>변경사항 저장</button></div>
</div>
<p id="msg"></p>
<script>
const REPO = "__REPO__", PATH = "data/articles.json", API = "https://api.github.com/repos/" + REPO + "/contents/" + PATH;
let tok = "", arts = [], tab = "pending";
const changes = {};  // id -> {status?, trust?}
try { tok = localStorage.getItem("gh_tok") || ""; } catch (e) {}
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const msg = t => $("#msg").textContent = t;

async function gh(opt = {}, raw = false) {
  const r = await fetch(API + (opt.method ? "" : "?ref=main&t=" + Date.now()), {...opt, headers: {
    Authorization: "Bearer " + tok, Accept: raw ? "application/vnd.github.raw+json" : "application/vnd.github+json"}});
  if (!r.ok) { const e = new Error(r.status + " " + (await r.text()).slice(0, 200)); e.status = r.status; throw e; }
  return r;
}
async function fetchLatest() {
  const meta = await (await gh()).json();
  const data = JSON.parse(await (await gh({}, true)).text());
  return {sha: meta.sha, data};
}
function b64(s) {
  const b = new TextEncoder().encode(s); let x = "";
  for (let i = 0; i < b.length; i += 0x8000) x += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  return btoa(x);
}
async function load() {
  msg("불러오는 중...");
  try { arts = (await fetchLatest()).data; }
  catch (e) { msg("연결 실패: " + e.message); if (e.status === 401) { $("#login").hidden = false; $("#app").hidden = true; } return; }
  $("#login").hidden = true; $("#app").hidden = false; msg(""); render();
}
function cur(a) { return {...a, ...(changes[a.id] || {})}; }
function render() {
  const pend = arts.filter(a => a.status === "pending");
  $("#np").textContent = pend.length;
  document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("pri", b.dataset.tab === tab));
  const list = tab === "pending" ? pend : arts.filter(a => (a.status || "published") === "published").sort((x, y) => y.ts - x.ts).slice(0, 40);
  $("#list").innerHTML = list.length ? list.map(a0 => {
    const a = cur(a0), ch = changes[a.id] || {};
    const st = ch.status;
    return `<div class="card ${st === "published" && tab === "pending" ? "ok" : ""} ${st === "rejected" ? "no" : ""}">
      <div class="meta"><span class="b ${esc(a.trust)}">${esc(a.trust)}</span><span>${esc(a.category)}</span>
      <span>· ${new Date(a.ts * 1000).toLocaleString("ko-KR", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"})}</span></div>
      <div class="t"><a href="a/${esc(a.id)}.html" target="_blank">${esc(a.title)}</a></div>
      <ul>${(a.tldr || []).map(t => `<li>${esc(t)}</li>`).join("")}</ul>
      ${(a.fixes || []).map(f => `<div class="fx">! ${esc(f)}</div>`).join("")}
      <div class="sr">출처: ${(a.sources || []).map(s => `<a href="${esc(s.link)}" target="_blank" rel="noopener">${esc(s.name)}</a>`).join(", ")}</div>
      <div class="act">
        ${tab === "pending"
          ? `<button class="btn ok ${st === "published" ? "on" : ""}" data-id="${esc(a.id)}" data-st="published">승인</button>
             <button class="btn no ${st === "rejected" ? "on" : ""}" data-id="${esc(a.id)}" data-st="rejected">거절</button>`
          : `<button class="btn no ${st === "rejected" ? "on" : ""}" data-id="${esc(a.id)}" data-st="rejected">내리기</button>`}
        <select data-trust="${esc(a.id)}">${["공식", "확인", "루머"].map(t => `<option ${t === a.trust ? "selected" : ""}>${t}</option>`).join("")}</select>
      </div></div>`;
  }).join("") : `<p>${tab === "pending" ? "검수 대기 중인 기사가 없습니다." : "게시된 기사가 없습니다."}</p>`;
  const n = Object.keys(changes).length;
  $("#save").disabled = !n; $("#save").textContent = n ? `변경사항 ${n}건 저장` : "변경사항 저장";
}
$("#list").addEventListener("click", e => {
  const b = e.target.closest("button[data-st]"); if (!b) return;
  const c = changes[b.dataset.id] = changes[b.dataset.id] || {};
  if (c.status === b.dataset.st) delete c.status; else c.status = b.dataset.st;
  if (!Object.keys(c).length) delete changes[b.dataset.id];
  render();
});
$("#list").addEventListener("change", e => {
  const id = e.target.dataset.trust; if (!id) return;
  const orig = arts.find(a => a.id === id).trust, c = changes[id] = changes[id] || {};
  if (e.target.value === orig) delete c.trust; else c.trust = e.target.value;
  if (!Object.keys(c).length) delete changes[id];
  render();
});
$("#save").onclick = async () => {
  $("#save").disabled = true;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      msg("저장 중...");
      const {sha, data} = await fetchLatest();  // 봇이 그사이 새 기사를 썼을 수 있으니 최신본에 적용
      const n = Object.keys(changes).length;
      for (const a of data) if (changes[a.id]) Object.assign(a, changes[a.id]);
      await gh({method: "PUT", body: JSON.stringify({
        message: `검수: ${n}건 처리`, sha, content: b64(JSON.stringify(data, null, 1)), branch: "main"})});
      for (const k in changes) delete changes[k];
      arts = data; render();
      msg(`저장했습니다. 1~3분 뒤 사이트에 반영됩니다.`);
      return;
    } catch (e) {
      if (e.status === 409 && attempt < 2) continue;  // 동시에 다른 커밋이 들어온 경우 재시도
      msg("저장 실패: " + e.message); $("#save").disabled = false; return;
    }
  }
};
$("#go").onclick = () => { tok = $("#tok").value.trim(); try { localStorage.setItem("gh_tok", tok); } catch (e) {} load(); };
$("#tok").onkeydown = e => { if (e.key === "Enter") $("#go").click(); };
$("#logout").onclick = () => { try { localStorage.removeItem("gh_tok"); } catch (e) {} location.reload(); };
document.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => { tab = b.dataset.tab; render(); });
if (tok) load();
</script>"""


def build_patches():
    ps = [x for x in load_json(DATA / "patches.json", []) if not x.get("skip")][:80]
    rows = "".join(f"""<div class="pt"><a href="{esc(x['url'])}" target="_blank" rel="noopener">
<img class="th" src="{esc(x['img'])}" alt="" loading="lazy"></a><div>
<div class="g">{esc(x['game'])} · {fmt_time(x['ts'])}</div><div class="t">{esc(x.get('title'))}</div>
<ul>{''.join(f'<li>{esc(t)}</li>' for t in x.get('points', []))}</ul>
<a class="lk" href="{esc(x['url'])}" target="_blank" rel="noopener">Steam 원문 패치노트</a></div></div>""" for x in ps)
    (SITE / "patches.html").write_text(page("패치노트 — " + SITE_NAME, f"""
<h2 class="sec">Steam 인기 게임 패치노트</h2>
<p class="note">Steam 동시접속 상위 게임의 최근 업데이트를 AI가 한국어로 요약했습니다. 정확한 내용은 원문을 확인하세요.</p>
{rows or '<p>아직 수집된 패치노트가 없습니다.</p>'}""").replace("{root}", ""), encoding="utf-8")


def build_calendar():
    cal = load_json(DATA / "calendar.json", {"releases": []})
    today = datetime.now().strftime("%Y-%m-%d")
    days = {}
    for r in cal["releases"]:
        days.setdefault(r["date"], []).append(r)
    wd = "월화수목금토일"
    out, marked = "", False
    for d in sorted(days):
        dt = datetime.strptime(d, "%Y-%m-%d")
        rels = sorted(days[d], key=lambda r: (not r.get("img"), -len(r["platforms"])))
        items = "".join(
            f'<div class="rel" data-p="{esc("|".join(r["platforms"]))}">'
            + (f'<a href="https://store.steampowered.com/app/{r["appid"]}" target="_blank" rel="noopener">'
               f'<img class="th" src="{esc(r["img"])}" alt="" loading="lazy"></a>' if r.get("img") else '<div class="nt"></div>')
            + f'<div><div class="t">{esc(r["title"])}</div><div class="s">{esc(" · ".join(r["platforms"]))}'
            + (" — " + esc(r["genre"]) if r.get("genre") else "") + '</div></div></div>' for r in rels)
        anchor = ' id="today"' if d >= today and not marked else ""
        marked = marked or d >= today
        out += (f'<div class="day{" today" if d == today else ""}{" past" if d < today else ""}"{anchor}>'
                f'<div class="d">{dt.month}.{dt.day}<small>{wd[dt.weekday()]}요일{" · 오늘" if d == today else ""}</small></div>'
                f'<div>{items}</div></div>')
    filters = "".join(f'<button data-f="{f}">{f}</button>' for f in ["PC", "PS5", "Xbox", "Switch 2", "Switch"])
    (SITE / "calendar.html").write_text(page("출시 캘린더 — " + SITE_NAME, f"""
<h2 class="sec">출시 캘린더</h2><nav><button class="on" data-f="">전체</button>{filters}</nav>
<div style="margin-top:12px">{out or '<p>일정을 불러오지 못했습니다.</p>'}</div>
<p class="note">출시 일정 출처: 위키백과 "List of video games released in {datetime.now().year}" (CC BY-SA 4.0) ·
지역 한정 출시 제외 · 이미지: Steam 스토어 · 업데이트 {esc(cal.get("updated", ""))}</p>
<script>document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{
document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));const f=b.dataset.f;
document.querySelectorAll('.rel').forEach(r=>r.style.display=!f||r.dataset.p.split('|').includes(f)?'':'none');
document.querySelectorAll('.day').forEach(d=>d.style.display=[...d.querySelectorAll('.rel')].some(r=>r.style.display!=='none')?'':'none')}});
const t=document.getElementById('today');if(t)t.scrollIntoView({{block:'start'}})</script>""").replace("{root}", ""),
        encoding="utf-8")


def build():
    every = sorted(load_json(DATA / "articles.json", []), key=lambda a: -a["ts"])
    articles = [a for a in every if a.get("status", "published") == "published"]
    pending = [a for a in every if a.get("status") == "pending"]
    (SITE / "a").mkdir(parents=True, exist_ok=True)
    for a in articles + pending:
        tldr = "".join(f"<li>{esc(t)}</li>" for t in a.get("tldr", []))
        body = "".join(f"<p>{esc(p)}</p>" for p in a.get("body", []))
        srcs = "".join(f'<li>{"[공식] " if s["official"] else ""}{esc(s["name"])} — '
                       f'<a href="{esc(s["link"])}" target="_blank" rel="noopener">{esc(s["title"])}</a></li>'
                       for s in a["sources"])
        note = "※ 아직 번역·재작성되지 않은 원문 요약입니다." if a.get("untranslated") else \
            f"이 기사는 AI가 출처 {len(a['sources'])}건을 종합해 작성했습니다. 사실관계는 출처를 확인하세요."
        html_ = page(a["title"] + " — " + SITE_NAME, f"""<article>{meta(a)}<h1>{esc(a["title"])}</h1>
{f'<figure class="hero"><img src="{esc(a["img"])}" alt=""><figcaption>이미지: Steam 스토어</figcaption></figure>' if a.get("img") else ''}
{f'<div class="tldr"><ul>{tldr}</ul></div>' if tldr else ''}
{f'<p class="why">{esc(a["why"])}</p>' if a.get("why") else ''}
{body}{ad("ADSENSE_SLOT_ARTICLE")}<div class="src"><b>출처</b><ul>{srcs}</ul></div><p class="note">{note}</p></article>""")
        (SITE / "a" / f"{a['id']}.html").write_text(html_.replace("{root}", "../"), encoding="utf-8")

    recent = [a for a in articles if time.time() - a["ts"] < 86400] or articles
    top = sorted(recent, key=lambda a: (-len(a["sources"]), a["trust"] != "공식", -a["ts"]))[:5]
    lead = next((a for a in top if a.get("img")), None)
    lead_html = (f'<a class="lead" href="a/{lead["id"]}.html"><img src="{esc(lead["img"])}" alt=""><div class="lc">{meta(lead)}'
                 f'<div class="t"><span>{esc(lead["title"])}</span></div><div class="w">{esc(lead.get("why"))}</div></div></a>'
                 ) if lead else ""
    top = [a for a in top if a is not lead]
    ticker = "".join(f'<a href="a/{a["id"]}.html"><b>{fmt_time(a["ts"])[6:]}</b>{esc(a["title"])}</a>' for a in articles[:12])
    brief = "".join(f'<a href="a/{a["id"]}.html" class="{"hi" if a.get("img") else ""}"><div>{meta(a)}'
                    f'<div class="t">{esc(a["title"])}</div><div class="w">{esc(a.get("why"))}</div></div>'
                    + (f'<img class="th" src="{esc(a["img"])}" alt="" loading="lazy">' if a.get("img") else "") + '</a>'
                    for a in top)
    stream = "".join(f'<a href="a/{a["id"]}.html" class="{"hi" if a.get("img") else ""}" data-c="{esc(a.get("category"))}" '
                     f'data-p="{esc(" ".join(a.get("platforms", [])))}"><span class="tm">{fmt_time(a["ts"])[6:]}</span>'
                     f'<div>{meta(a)}<div class="t">{"<span class=new>NEW</span>" if time.time() - a["ts"] < 3 * 3600 else ""}{esc(a["title"])}</div></div>'
                     + (f'<img class="th" src="{esc(a["img"])}" alt="" loading="lazy">' if a.get("img") else "")
                     + '</a>' for a in articles[:100])
    filters = "".join(f'<button data-f="{f}">{f}</button>' for f in ["PC", "PS5", "Xbox", "Switch"] + CATEGORIES[:-1])
    index = page("알트탭 ALT+TAB — 업무 중 잠깐, 게임 소식 3줄", f"""
<div class="tick"><span class="live">LIVE</span><div class="tk"><div class="tkin">{ticker}{ticker}</div></div></div>
<h2 class="sec">오늘의 브리핑</h2>{lead_html}<div class="brief">{brief or '<p>아직 기사가 없습니다.</p>'}</div>
{ad("ADSENSE_SLOT_HOME")}<h2 class="sec">최신 소식</h2><nav><button class="on" data-f="">전체</button>{filters}</nav>
<div class="stream" id="st" style="margin-top:12px">{stream}</div>
<script>document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{
document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));const f=b.dataset.f;
document.querySelectorAll('#st a').forEach(a=>a.style.display=!f||a.dataset.c===f||a.dataset.p.split(' ').includes(f)?'':'none')}})</script>""")
    (SITE / "index.html").write_text(index.replace("{root}", ""), encoding="utf-8")
    repo = os.environ.get("GITHUB_REPOSITORY", "khb950404-creator/gamewebzine")
    (SITE / "review.html").write_text(
        page("검수 — " + SITE_NAME, REVIEW_BODY.replace("__REPO__", repo)).replace("{root}", "")
        .replace("<head>", '<head><meta name="robots" content="noindex">'), encoding="utf-8")
    static_pages()
    build_patches()
    build_calendar()
    print(f"사이트 빌드 완료: {SITE / 'index.html'} (기사 {len(articles)}건)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in ("review", "approve", "reject"):
        review(cmd, sys.argv[2:])
    elif cmd != "build":
        run()
    build()
