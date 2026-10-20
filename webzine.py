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
import json, os, re, sys, html, hashlib, time, unicodedata, urllib.request, urllib.error, urllib.parse
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
    ("Steam News", "https://store.steampowered.com/feeds/news/", True),
    ("Gematsu", "https://www.gematsu.com/feed", False),
    ("Siliconera", "https://www.siliconera.com/feed/", False),
    ("Push Square", "https://www.pushsquare.com/feeds/latest", False),
    ("Pure Xbox", "https://www.purexbox.com/feeds/latest", False),
    ("Rock Paper Shotgun", "https://www.rockpapershotgun.com/feed", False),
    ("GamesRadar", "https://www.gamesradar.com/rss/", False),
    ("Destructoid", "https://www.destructoid.com/feed/", False),
    ("Polygon", "https://www.polygon.com/rss/index.xml", False),
    ("Kotaku", "https://kotaku.com/rss", False),
    ("Insider Gaming", "https://insider-gaming.com/feed/", False),
]
MAX_ARTICLES_PER_RUN = int(os.environ.get("MAX_ARTICLES", 12))  # 무료 쿼터 보호
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
        img = None
        if official:  # 공식 출처만: 매체 사진은 저작권 문제로 쓰지 않는다
            blob = ET.tostring(it, encoding="unicode")
            m = re.search(r'(?:url|src)="(https?://[^"]+?\.(?:jpe?g|png|webp)[^"]*)"', html.unescape(blob), re.I)
            img = m[1] if m else None
        items.append({
            "id": hashlib.md5(link.encode()).hexdigest()[:12],
            "source": name, "official": official, "title": title, "link": link,
            "summary": strip_html(it.findtext("description"))[:2500], "ts": ts, "img": img,
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


def llm(prompt, prefer_lite=False, search=False):
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
                                 **({"tools": [{"google_search": {}}], "generationConfig": {"temperature": 0.2}} if search else
                                    {"generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}})}).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
            for attempt in range(3):
                try:
                    t = time.time()
                    r = json.loads(urllib.request.urlopen(req, timeout=300).read())
                    print(f"    ({model}, {time.time() - t:.0f}초)")
                    text = "".join(pt.get("text", "") for pt in r["candidates"][0]["content"]["parts"])
                    if search:  # 검색 모드는 JSON 강제가 안 돼서 본문에서 JSON 만 꺼낸다
                        text = text[text.find("{"):text.rfind("}") + 1]
                    return json.loads(text)
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
- "공식": 회사(개발사·퍼블리셔·플랫폼)가 발표한 내용. 공식 블로그·보도자료·트레일러·패치노트를 매체가 전한 경우도 포함
- "확인": 회사 발표는 아니지만 2개 이상 매체가 각자 확인해 보도 (판매량 집계, 업계 동향 등)
- "보도": 매체 한 곳의 취재·분석 기사. 유출이나 익명 제보가 아니면 루머가 아니다
- "루머": 유출, 데이터마이닝, 익명 소식통, "~라고 알려졌다/~할 수도" 같은 추측, 미확인 소문
대부분의 게임 뉴스는 공식 발표를 전하는 기사다. 루머는 정말 확인되지 않은 정보에만 붙여라.

기사 작성 기준 (전문 게임 기자처럼 성의 있게):
- 본문은 4~6문단, 문단마다 3~4문장. 출처 '본문'의 구체적 정보(수치, 날짜, 가격, 인물 발언, 새 기능·모드·콘텐츠, 개발 배경)를 최대한 살려라.
- 1문단: 무엇이 언제 발표됐나 (리드). 2~4문단: 세부 내용, 핵심 발언 인용("...라고 밝혔다"), 전작·경쟁작과의 맥락.
  마지막 문단: 출시일·업데이트 일정 등 게이머가 체크할 점 (출처에 있는 것만).
- 같은 말 반복, "기대를 모으고 있다", "귀추가 주목된다" 같은 상투적 마무리 금지. 문장마다 새 정보가 있어야 한다.

뉴스 가치 판단 (엄격하게): 게이머가 "오, 이건 알아야지" 할 소식만 쓴다.
신작 발표·출시일·연기, 대형 업데이트·확장팩, 판매/동접 기록, 업계 사건(인수·해고·폐업·소송), 주요 논란, 하드웨어 소식, 큰 할인 정도.
아래는 전부 skip: 성우·캐스팅 잡담, 개인 인터뷰 속 사소한 회고, 마이너 인디의 소소한 소식, 코스튬·스킨 하나 추가,
유튜버/커뮤니티 반응 모음, 리스트형 기사("~할 게임 10선"), 가이드·공략, 리뷰 점수 단독 나열, 이미 다룬 소식의 재탕.

PC·콘솔 비디오게임과 무관하거나(보드·카드게임, 게임과 연결 약한 영화·드라마 소식 등)
광고/딜 나열/퀴즈/팟캐스트 같은 저가치 글은 {{"issue": 번호, "skip": true}} 로만 표시하라.

JSON 하나만 출력:
{{"articles": [
 {{"issue": 이슈 번호,
  "title": "한국어 헤드라인(핵심 사실이 제목만 봐도 보이게. 50자 이내, 낚시 금지)",
  "tldr": ["핵심 요약 1", "핵심 요약 2", "핵심 요약 3"] (각각 '~했다', '~이다'로 끝나는 기사체 완결 문장.
           정보 전달 우선: 누가·무엇을·언제·얼마나를 구체적으로(게임명, 날짜, 가격, 수치, 플랫폼). 40~90자.
           "대규모 업데이트 진행", "다양한 콘텐츠 추가"처럼 뭉뚱그린 축약 금지 — 무엇이 추가됐는지 이름을 적어라),
  "why": "PC·콘솔 게이머에게 왜 중요한가 (1~2문장)",
  "body": ["문단1", "문단2", "문단3", "문단4", "문단5"],
  "category": "{cats} 중 하나",
  "platforms": ["PC","PS5","Xbox","Switch" 중 출처에 명시된 것만],
  "games": ["관련 게임명"],
  "games_en": ["관련 게임의 영문 공식 제목 (Steam 검색용, 출처에 나온 표기 그대로)"],
  "hook": "릴스 첫 화면용 한 줄 훅 (8~16자, 궁금증·숫자·반전. 예: 'GTA 6 또 연기?', '90% 할인 실화')",
  "title_en": "영문 헤드라인 (게임 매체 스타일, 60자 이내)",
  "tldr_en": ["English key point 1", "English key point 2", "English key point 3"],
  "hook_en": "English reel hook (3~6 words)",
  "video_q": "이 기사 주제에 딱 맞는 공식 영상을 YouTube 에서 찾을 영문 검색어 (예: 'Fortnite Sonic collaboration trailer'). 트레일러·영상 공개 기사가 아니면 null",
  "trust": "공식|확인|보도|루머"}}
]}}

{issues}"""


VERIFY = """너는 게임 웹진의 팩트체커다. 아래 기사들을 각 이슈의 출처 자료와 한 문장씩 대조하라.
출처에 근거가 없는 주장(추측, 배경 설명으로 지어낸 사실, 틀린 날짜·수치·플랫폼)은 삭제하거나 출처에 맞게 고쳐라.
문체와 구조는 유지하고, 문제없는 문장은 건드리지 마라. trust 값도 규칙에 맞는지 확인하라
(공식=회사 발표를 전한 경우 포함, 확인=2개 이상 매체, 보도=매체 한 곳의 취재, 루머=유출·익명 소식통·추측만).

JSON 하나만 출력 (입력의 모든 기사를 같은 issue 번호로):
{{"articles": [{{"issue": 번호, "problems": ["발견한 문제 설명"], "title": ..., "tldr": [...], "why": ..., "body": [...],
  "category": ..., "platforms": [...], "games": [...], "trust": ...}}]}}

{issues}

기사:
{articles}"""


def clean_article(a):
    """AI 출력 형식 바로잡기. 제목이 글자가 아니면(건너뛸 글 표시가 섞인 경우 등) None."""
    if not isinstance(a.get("title"), str) or not a["title"].strip() or a.get("skip"):
        return None
    for k_ in ("why", "trust", "category", "video_q", "hook", "title_en", "hook_en"):
        if k_ in a and a[k_] is not None and not isinstance(a[k_], str):
            a[k_] = str(a[k_]) if not isinstance(a[k_], (dict, list)) else ""
    for k_ in ("tldr", "body", "platforms", "games", "games_en", "tldr_en"):
        v_ = a.get(k_)
        if isinstance(v_, str):
            v_ = [v_]
        a[k_] = [x for x in (v_ or []) if isinstance(x, str) and x.strip()]
    for k_, d_ in (("trust", "보도"), ("category", "기타"), ("why", "")):
        if not a.get(k_):
            a[k_] = d_
    if a["trust"] not in ("공식", "확인", "보도", "루머"):
        a["trust"] = "보도"
    return a


class LLMDown(Exception):
    pass


def full_text(url, limit=3000):
    """원문 페이지 본문(<p>)을 가져온다. RSS 요약만으론 기사가 얕아서."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"})
        h = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
        h = re.sub(r"(?is)<(script|style|nav|footer|aside|header)[^>]*>.*?</\1>", " ", h)
        ps = [strip_html(p) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", h)]
        return "\n".join(p for p in ps if len(p) > 60)[:limit]
    except Exception:
        return ""


def issue_text(n, c):
    out = []
    for i, s in enumerate(c["items"][:6]):
        body = ""
        if i < 2:  # 앞의 두 출처는 원문 본문까지
            body = s.get("full") or full_text(s.get("link", ""))
            s["full"] = body
        out.append(f"- {s['source']} (official={str(s['official']).lower()})\n  제목: {s['title']}\n  요약: {s['summary']}"
                   + (f"\n  본문: {body}" if body else ""))
    return f"=== 이슈 {n} ===\n" + "\n".join(out)


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
        for k_, v_ in (("trust", "보도"), ("category", "기타"), ("tldr", []), ("body", []), ("why", ""), ("platforms", []), ("games", [])):
            if not a.get(k_):
                a[k_] = v_  # AI 가 필드를 빼먹어도 빌드가 멈추지 않게
        a["status"] = "pending" if review_all else "published"  # 일단 게시하고 검수 페이지에서 확인·수정
        a["id"] = hashlib.md5("".join(sorted(s["id"] for s in srcs)).encode()).hexdigest()[:10]
        a["sources"] = [{"name": s["source"], "title": s["title"], "link": s["link"], "official": s["official"],
                         **({"img": s["img"]} if s.get("img") else {})} for s in srcs]
        a["ts"] = max(s["ts"] for s in srcs)
        a["written"] = time.time()
        a.pop("issue", None)
        a = clean_article(a)
        if a:
            out.append(a)
    return out


# ---------- 공식 영상 (트레일러 기사) ----------
VIDEO_RE = re.compile(r"trailer|teaser|gameplay|reveal|showcase|video|트레일러|티저|영상|게임플레이|쇼케이스", re.I)
YT_RE = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/)|youtu\.be/)([\w-]{11})")
# 매체가 재업로드한 영상은 쓰지 않는다 (공식 채널 영상만)
MEDIA_CHANNELS = {"ign", "gamespot", "pcgamer", "eurogamer", "nintendolife", "vgc", "gametrailers", "gamesradar",
                  "polygon", "kotaku", "gamexplain", "maximiliandood", "gameranx", "skillup", "digitalfoundry", "gamespotgameplay", "theverge", "pushsquare", "purexbox", "gameinformer", "rockpapershotgun", "dualshockers"}


def yt_info(vid):
    try:
        return get_json(f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={vid}", timeout=15)
    except Exception:
        return None


def find_videos(articles, cache=None):
    n = 0
    for a in articles:
        if a.get("video_v"):
            continue
        a["video_v"] = 1
        text = " ".join([a["title"]] + a.get("tldr", []) + [s_["title"] for s_ in a.get("sources", [])])
        if not VIDEO_RE.search(text) or a.get("trust") == "루머" or re.search(r"유출|leak|협박|루머|rumou?r", text, re.I):
            continue  # 유출·루머 기사에 공식 영상을 붙이면 오해를 부른다
        ids = []
        for s_ in a.get("sources", [])[:4]:
            try:
                page_ = urllib.request.urlopen(urllib.request.Request(s_["link"], headers=UA), timeout=15).read().decode("utf-8", "ignore")
            except Exception:
                continue
            for v in YT_RE.findall(page_):
                if v not in ids:
                    ids.append(v)
        for v in ids[:6]:
            info = yt_info(v)
            if not info:
                continue
            ch = re.sub(r"[^a-z]", "", (info.get("author_name") or "").lower())
            if any(m in ch for m in MEDIA_CHANNELS) or not video_fits(info.get("title", ""), a):
                continue
            a["video"] = {"id": v, "title": info.get("title", ""), "channel": info.get("author_name", "")}
            break
        if not a.get("video"):  # 출처 페이지에 없으면 YouTube 에서 공식 채널 영상 검색
            app = (cache or {}).get(f"id:{a.get('appid')}") or {}
            extra = [(app.get("info") or {}).get("dev"), (app.get("info") or {}).get("pub")]
            for g in (a.get("games_en") or [])[:2]:
                v = yt_search(g, extra, a.get("video_q"), a)
                if v:
                    a["video"] = v
                    break
        n += bool(a.get("video"))
    print(f"공식 영상 {n}건")


# 공식 채널로 인정하는 이름 (정규화). 여기에 더해 게임 이름·Steam 개발사/배급사 이름이 들어간 채널도 인정
OFFICIAL_CHANNELS = {"playstation", "xbox", "nintendo", "nintendoofamerica", "nintendouk", "nintendokorea", "playstationkorea",
                     "ubisoft", "ea", "electronicarts", "bethesda", "capcom", "capcomusa", "bandainamco", "bandainamcoentertainmentamerica",
                     "squareenix", "sega", "atlus", "konami", "activision", "callofduty", "rockstargames", "2k", "warnerbrosgames",
                     "devolverdigital", "marvelentertainment", "riotgames", "blizzardentertainment", "epicgames", "steam",
                     "cdprojektred", "focusentertainment", "paradoxinteractive", "annapurnainteractive", "thq", "thqnordic",
                     "krafton", "nexon", "netmarble", "ncsoft", "pearlabyss", "shiftup", "neowiz", "embarkstudios", "remedyentertainment",
                     "fromsoftware", "koeitecmoamerica", "spikechunsoft", "arcsystemworks", "team17", "raw fury".replace(" ", "")}


def video_fits(vtitle, a):
    """영상 제목이 기사 주제와 맞는지: 콜라보처럼 게임이 둘 이상이면 둘 다, 검색어의 주제 단어도 하나 이상."""
    t = norm(vtitle)
    bases = [norm(EDITION_RE.sub("", g).split(":")[0]) for g in (a.get("games_en") or [])[:2]]
    bases = [b for b in bases if len(b) > 2]
    collab = re.search(r"x|×|콜라보|컬래버|collab|crossover", a["title"] + " " + (a.get("video_q") or ""), re.I)
    if bases and sum(b in t for b in bases) < (min(2, len(bases)) if collab else 1):
        return False
    gw = {w_ for g in (a.get("games_en") or []) for w_ in re.findall(r"[a-z0-9]+", g.lower())}
    topic = [w_ for w_ in re.findall(r"[a-z0-9]+", (a.get("video_q") or "").lower())
             if w_ not in YT_GENERIC and w_ not in gw and len(w_) > 2]
    return not topic or any(w_ in vtitle.lower() for w_ in topic)


def yt_age_days(t):
    m = re.match(r"(?:Streamed )?(\d+)\s*(second|minute|hour|day|week|month|year|s|m|h|d|w|mo|y)", (t or "").strip())
    if not m:
        return 999
    n, u = int(m[1]), m[2]
    return n * {"second": 0, "minute": 0, "hour": 0, "s": 0, "m": 0, "h": 0, "day": 1, "d": 1, "week": 7, "w": 7,
                "month": 30, "mo": 30, "year": 365, "y": 365}[u]


YT_GENERIC = set("official trailer teaser gameplay reveal video launch announcement announce cinematic new the a of and "
                 "for with in on to x vs collab collaboration update season showcase first look".split())


def yt_search(game, extra_official=(), query=None, art=None):
    """YouTube 검색 → 최근 2주 안에 공식 채널이 올린 그 게임 영상."""
    try:
        h = urllib.request.urlopen(urllib.request.Request(
            "https://www.youtube.com/results?hl=en&gl=US&search_query=" + urllib.parse.quote(query or game + " official trailer"),
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"}), timeout=20).read().decode("utf-8", "ignore")
        data = json.loads(re.search(r"var ytInitialData = (\{.*?\});</script>", h)[1])
    except Exception:
        return None

    def walk(o):
        if isinstance(o, dict):
            if "videoRenderer" in o:
                yield o["videoRenderer"]
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)
    gbase = norm(EDITION_RE.sub("", game).split(":")[0])
    # 게임 이름 말고 주제를 나타내는 단어 (예: "Fortnite Sonic collaboration trailer" → sonic)
    gwords = set(re.findall(r"[a-z0-9]+", game.lower()))
    topic = [w_ for w_ in re.findall(r"[a-z0-9]+", (query or "").lower()) if w_ not in YT_GENERIC and w_ not in gwords and len(w_) > 2]
    official = OFFICIAL_CHANNELS | {norm(x) for x in extra_official if x}
    for v in list(walk(data))[:12]:
        title = "".join(r_.get("text", "") for r_ in v.get("title", {}).get("runs", []))
        ch = (v.get("ownerText", {}).get("runs") or [{}])[0].get("text", "")
        chn = norm(ch)
        if topic and not any(t_ in title.lower() for t_ in topic):
            continue
        if art and not video_fits(title, art):
            continue
        if gbase not in norm(title) or yt_age_days(v.get("publishedTimeText", {}).get("simpleText")) > 14:
            continue
        verified = any("VERIFIED" in (b_.get("metadataBadgeRenderer", {}).get("style", "")) for b_ in v.get("ownerBadges", []))
        if any(m in chn for m in MEDIA_CHANNELS):
            continue
        gfirst = {w_ for g in [game] + ((art or {}).get("games_en") or []) for w_ in re.findall(r"[a-z0-9]+", g.lower())[:1] if len(w_) > 3}
        if (verified and any(w_ in chn for w_ in gfirst)) or chn in official or (gbase and gbase in chn) or any(o and o in chn for o in official if len(o) > 5):
            return {"id": v["videoId"], "title": title, "channel": ch}
    return None


def video_html(a):
    v = a.get("video")
    if not v:
        return ""
    vid = esc(v["id"])
    # 썸네일만 먼저 보여주고 누르면 재생 (페이지가 무거워지지 않게)
    return (f'<figure class="vid"><button class="yt" data-v="{vid}" aria-label="영상 재생">'
            f'<img src="https://i.ytimg.com/vi/{vid}/hqdefault.jpg" alt="" loading="lazy"><span class="play"></span></button>'
            f'<figcaption>영상: {esc(v.get("channel") or "YouTube")}{" — " + esc(v["title"]) if v.get("title") else ""}</figcaption></figure>')


# ---------- 게임 공식 한글 표기 ----------
# data/names.json: {"정규화된 영문명": {"en": 원문, "ko": 공식 한글명 또는 null, "src": 출처}}
# 직접 고쳐도 되고(검수 때 발견한 표기), 새 게임은 Steam 한국어 이름 → 구글 검색 순으로 자동으로 채운다.
NAMES_PROMPT = """다음 게임들의 '한국 공식 한글 표기명'을 구글에서 검색해 확인하라.
기준: 한국 퍼블리셔·플랫폼(PlayStation Korea, 닌텐도 코리아, Xbox, Steam 한국어 페이지) 공식 표기 > 국내 주요 게임 매체(인벤, 디스이즈게임, 루리웹 등) 다수 표기.
공식 한글명이 없거나 영문 그대로 쓰면 null. 추측해서 소리 나는 대로 옮기지 마라.
JSON 하나만 출력: {{"names": [{{"en": "영문명", "ko": "공식 한글명 또는 null"}}]}}

{names}"""


def has_hangul(t):
    return bool(re.search(r"[가-힣]", t or ""))


def resolve_names(articles, cache):
    """기사에 나온 게임의 공식 한글명을 확인해 제목·요약·본문의 표기를 바로잡는다."""
    names = load_json(DATA / "names.json", {})
    pairs = [(a, i, en) for a in articles for i, en in enumerate(a.get("games_en") or []) if en]
    unknown = []
    for _, _, en in pairs:
        k = norm(en)
        if k in names or not k:
            continue
        app = steam_find(en, cache)
        steam_ko = (app or {}).get("name", "")
        if has_hangul(steam_ko):  # Steam 한국어 페이지 이름 (예: "에이스 컴뱃 8: 시브의 날개 (ACE COMBAT 8...)")
            ko = re.sub(r"\s*\([^)]*[A-Za-z][^)]*\)\s*$", "", steam_ko).replace("™", "").replace("®", "").strip()
            ko = re.sub(r"(?<=[가-힣])\s+[A-Za-z][A-Za-z0-9 :'’&.-]*$", "", ko).strip()  # "니발리스 나이트 Nivalis Nights"
            names[k] = {"en": en, "ko": ko, "src": "Steam"}
        elif en not in unknown:
            unknown.append(en)
    if unknown:
        try:
            res = llm(NAMES_PROMPT.format(names="\n".join(f"- {n}" for n in unknown[:40])), prefer_lite=True, search=True) or {}
            for r in res.get("names", []):
                if r.get("en"):
                    names[norm(r["en"])] = {"en": r["en"], "ko": r.get("ko") or None, "src": "검색"}
        except Exception as e:
            print(f"  ! 한글 표기 검색 실패: {e}")
    fixed = 0
    for a, i, en in pairs:
        off = (names.get(norm(en)) or {}).get("ko")
        games = a.get("games") or []
        mine = games[i] if i < len(games) else None
        if not off or not mine or norm(off) == norm(mine):
            continue
        swaps = [(mine, off)]
        if ":" in mine and ":" in off:  # 본문에 부제 없이 쓴 경우도 ("마블 토콘" → "마블 투혼")
            swaps.append((mine.split(":")[0].strip(), off.split(":")[0].strip()))
        for old, new in swaps:
            a["title"] = a["title"].replace(old, new)
            a["why"] = (a.get("why") or "").replace(old, new)
            a["tldr"] = [t.replace(old, new) for t in a.get("tldr", [])]
            a["body"] = [t.replace(old, new) for t in a.get("body", [])]
        games[i] = off
        a.setdefault("fixes", []).append(f"게임명 표기 '{mine}' → '{off}'")
        fixed += 1
    save_json(DATA / "names.json", names)
    print(f"한글 표기 교정 {fixed}건 (사전 {len(names)}개)")


# ---------- 저장 ----------
def load_json(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def run():
    state = load_json(DATA / "state.json", {})
    gap = float(os.environ.get("MIN_GAP_MIN", 150))
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and time.time() - state.get("last_run", 0) < gap * 60:
        print(f"마지막 실행 후 {gap:.0f}분이 지나지 않아 건너뜀")
        return
    state["last_run"] = time.time()
    save_json(DATA / "state.json", state)
    seen = set(load_json(DATA / "seen.json", []))
    articles = load_json(DATA / "articles.json", [])
    print("수집 중...")
    items = [i for name, url, off in FEEDS for i in fetch_feed(name, url, off)]
    fresh = [i for i in items if i["id"] not in seen and time.time() - i["ts"] < 3 * 86400]
    print(f"새 항목 {len(fresh)}건")
    # 건너뛰는 이슈를 감안해 여유 있게 후보를 뽑고, 작성된 것 중 상위 MAX 건만 쓴다
    cands = cluster(fresh)[:MAX_ARTICLES_PER_RUN + 8]
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
    for step in (lambda: resolve_names(new, cache), lambda: attach_images(articles, cache), lambda: find_videos(new, cache), lambda: collect_patches(cache), lambda: collect_calendar(cache),
                 lambda: collect_deals(cache), collect_all_deals):
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
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    s = unicodedata.normalize("NFC", s)  # 한글은 다시 조합
    return re.sub(r"[^0-9a-z가-힣]", "", s.lower().replace("&", "and"))


def steam_info(d):
    langs = d.get("supported_languages") or ""
    ko = "한국어" in langs or "Korean" in langs
    ko_voice = bool(re.search(r"(한국어|Korean)<strong>\*</strong>", langs))
    price = d.get("price_overview") or {}
    return {"date": (d.get("release_date") or {}).get("date", ""),
            "soon": (d.get("release_date") or {}).get("coming_soon", False),
            "genres": [g["description"] for g in d.get("genres", [])][:3],
            "dev": (d.get("developers") or [""])[0], "pub": (d.get("publishers") or [""])[0],
            "ko": ko, "ko_voice": ko_voice,
            "price": "무료" if d.get("is_free") else price.get("final_formatted", ""),
            "discount": price.get("discount_percent", 0),
            "meta": (d.get("metacritic") or {}).get("score")}


def library_hero(appid):
    u = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/library_hero.jpg"
    try:
        urllib.request.urlopen(urllib.request.Request(u, method="HEAD", headers=UA), timeout=10)
        return u
    except Exception:
        return None


def steam_app(appid, cache):
    """appid → {name, img}. data/steam_apps.json 에 캐시."""
    key = f"id:{appid}"
    if key not in cache or (cache[key] and "info" not in cache[key]):  # 예전 캐시엔 상세 정보가 없음
        try:
            d = get_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=kr&l=koreana")[str(appid)]
            d = d.get("data") if d.get("success") else None
            # hd: 첫 번째 공식 스크린샷(1920x1080) — 기사 대표 이미지용
            shots = (d or {}).get("screenshots") or [{"path_full": library_hero(appid)}]
            cache[key] = {"appid": appid, "name": d["name"], "img": d.get("header_image"),
                          "hd": shots[0].get("path_full") or d.get("header_image"), "info": steam_info(d)} if d else None
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


EDITION_RE = re.compile(r"\s*[-–:]?\s*(definitive|deluxe|ultimate|complete|gold|goty|game of the year|standard|"
                        r"enhanced|remastered|director'?s cut)( edition)?\s*$", re.I)


_igdb_token = None


def igdb(query):
    """IGDB(트위치) API. 콘솔 게임까지 공식 아트워크·스크린샷·표지를 제공."""
    global _igdb_token
    cid, sec = os.environ.get("IGDB_CLIENT_ID"), os.environ.get("IGDB_CLIENT_SECRET")
    if not (cid and sec):
        return None
    if not _igdb_token:
        _igdb_token = json.loads(urllib.request.urlopen(urllib.request.Request(
            "https://id.twitch.tv/oauth2/token?" + urllib.parse.urlencode(
                {"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}), data=b""), timeout=30).read())["access_token"]
    req = urllib.request.Request("https://api.igdb.com/v4/games", data=query.encode(),
                                 headers={"Client-ID": cid, "Authorization": f"Bearer {_igdb_token}", "Accept": "application/json"})
    time.sleep(0.3)  # 초당 4회 제한
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def igdb_find(title, cache):
    """게임 이름 → IGDB 이미지 (정확히 같은 이름·대체 이름만). 아트워크 > 스크린샷 > 표지 순."""
    key = "ig:" + norm(title)
    if not norm(title) or not os.environ.get("IGDB_CLIENT_ID"):
        return None
    if key not in cache:
        cache[key] = None
        try:
            q = title.replace('"', "")
            res = igdb(f'search "{q}"; fields name,alternative_names.name,artworks.image_id,screenshots.image_id,'
                       f'cover.image_id,first_release_date; limit 8;') or []
            want = norm(EDITION_RE.sub("", title))
            for g in res:
                cands = [g.get("name", "")] + [x.get("name", "") for x in g.get("alternative_names", [])]
                if not any(norm(EDITION_RE.sub("", c)) == want for c in cands):
                    continue
                art = (g.get("artworks") or g.get("screenshots") or [{}])[0].get("image_id")
                cov = (g.get("cover") or {}).get("image_id")
                if art:
                    cache[key] = {"img": f"https://images.igdb.com/igdb/image/upload/t_screenshot_big/{art}.jpg",
                                  "hd": f"https://images.igdb.com/igdb/image/upload/t_1080p/{art}.jpg"}
                elif cov:
                    cache[key] = {"img": f"https://images.igdb.com/igdb/image/upload/t_cover_big/{cov}.jpg",
                                  "hd": f"https://images.igdb.com/igdb/image/upload/t_720p/{cov}.jpg", "cover": True}
                if cache[key]:
                    break
        except Exception as e:
            print(f"  ! IGDB: {e}")
            del cache[key]
            return None
    return cache.get(key)


def itad_find(title, cache):
    """ITAD 게임 검색으로 배너 이미지(정확히 같은 이름만). Steam 에 없는 PC 게임 보완용."""
    if not os.environ.get("ITAD_API_KEY"):
        return None
    key = "itq:" + norm(title)
    if key not in cache:
        cache[key] = None
        try:
            for g in itad("/games/search/v1", title=title, results=5):
                if norm(EDITION_RE.sub("", g.get("title", ""))) == norm(title) and (g.get("assets") or {}).get("banner600"):
                    cache[key] = g["assets"]["banner600"]
                    break
        except Exception:
            del cache[key]
            return None
    return cache.get(key)


def attach_images(articles, cache):
    n = 0
    for a in articles:
        if a.get("img") or a.get("img_v") == 2:  # 이미 있거나, 새 방식으로 찾아봤는데 없던 기사
            continue
        a["img"], a["img_v"] = None, 2
        names = []
        for g in (a.get("games_en") or []) + (a.get("games") or []):
            for v in (g, EDITION_RE.sub("", g)):
                if v and v not in names:
                    names.append(v)
        for g in names:
            app = steam_find(g, cache)
            if app and app.get("img"):
                a["img"], a["img_hd"], a["appid"], a["img_src"] = app["img"], app.get("hd"), app["appid"], "Steam 스토어"
                break
        if not a["img"]:
            for g in names:
                ig = igdb_find(g, cache)
                if ig:
                    a["img"], a["img_hd"], a["img_src"] = ig["img"], ig["hd"], "IGDB"
                    break
        if not a["img"]:
            for g in names:
                im = itad_find(g, cache)
                if im:
                    a["img"], a["img_src"] = im, "IsThereAnyDeal"
                    break
        if not a["img"]:
            off = next((s_ for s_ in a.get("sources", []) if s_.get("official") and s_.get("img")), None)
            if off:
                a["img"], a["img_src"] = off["img"], off["name"]
        n += bool(a["img"])
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


# ---------- 할인 (Steam 인기 판매 순위 중 할인 게임, 한국 가격) ----------
def collect_deals(cache):
    ids = []
    for start in (0, 50):
        try:
            res = json.loads(urllib.request.urlopen(urllib.request.Request(
                "https://store.steampowered.com/search/results/?specials=1&filter=topsellers&json=1&count=50"
                f"&start={start}&cc=kr&l=koreana", headers=UA), timeout=30).read())
        except Exception as e:
            print(f"  ! Steam 할인 목록: {e}")
            continue
        for it in res.get("items", []):
            m = re.search(r"/apps/(\d+)/", it.get("logo", ""))
            if m and int(m[1]) not in ids:
                ids.append(int(m[1]))
    if not ids:
        return
    prices = {}
    for i in range(0, len(ids), 50):  # price_overview 는 여러 appid 를 한 번에 조회 가능
        chunk = ids[i:i + 50]
        try:
            d = get_json("https://store.steampowered.com/api/appdetails?filters=price_overview&cc=kr&appids="
                         + ",".join(map(str, chunk)), timeout=30)
        except Exception as e:
            print(f"  ! Steam 가격: {e}")
            continue
        for k, v in d.items():
            po = (v.get("data") or {}).get("price_overview") if v.get("success") and isinstance(v.get("data"), dict) else None
            if po and po.get("discount_percent", 0) > 0:
                prices[int(k)] = po
    hist = load_json(DATA / "price_hist.json", {})
    deals = []
    for rank, appid in enumerate(ids, 1):
        po = prices.get(appid)
        if not po:
            continue
        app = steam_app(appid, cache)
        if not app or not app.get("info"):
            continue
        low = hist.get(str(appid))
        record = low is None or po["final"] <= low
        hist[str(appid)] = po["final"] if low is None else min(low, po["final"])
        i = app["info"]
        deals.append({"appid": appid, "name": app["name"], "img": app["img"], "rank": rank,
                      "discount": po["discount_percent"], "initial": po["initial"] // 100, "final": po["final"] // 100,
                      "low": record and low is not None, "ko": i["ko"], "ko_voice": i["ko_voice"],
                      "genres": i["genres"], "meta": i.get("meta")})
    save_json(DATA / "price_hist.json", hist)
    shop_status = "ok"
    try:
        (attach_shops_itad if os.environ.get("ITAD_API_KEY") else attach_shops)(deals, cache)
    except Exception as e:
        body = getattr(e, "read", lambda: b"")()[:300].decode("utf-8", "ignore")
        shop_status = f"{type(e).__name__}: {e} {body}"
        print(f"  ! 판매처 가격: {shop_status}")
    save_json(DATA / "deals.json", {"updated": datetime.now().strftime("%m.%d %H:%M"), "deals": deals,
                                    "shops_status": shop_status, "itad": bool(os.environ.get("ITAD_API_KEY"))})
    print(f"할인 {len(deals)}건")


# Steam 키를 파는 공인 판매처만 (GOG·Epic·Uplay 는 Steam 키가 아니라 제외)
SHOPS = {"3": "Green Man Gaming", "11": "Humble", "15": "Fanatical", "21": "WinGameStore", "23": "GameBillet",
         "27": "Gamesplanet", "28": "Gamesload", "30": "IndieGala", "2": "GamersGate"}
CS_UA = {"User-Agent": "AltTabWebzine/1.0 (+https://github.com/khb950404-creator/gamewebzine)"}


def cs_json(url):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=CS_UA), timeout=30).read())


def itad(path, body=None, **q):
    q["key"] = os.environ["ITAD_API_KEY"]
    req = urllib.request.Request(f"https://api.isthereanydeal.com{path}?{urllib.parse.urlencode(q)}",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={**({"Content-Type": "application/json"} if body is not None else {}), **CS_UA})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def attach_shops_itad(deals, cache):
    """IsThereAnyDeal: 한국(KR) 기준 상점별 현재가·역대 최저가. Steam 키(DRM: Steam)를 파는 상점만."""
    rate = None
    need = [f"app/{x['appid']}" for x in deals if f"itad:{x['appid']}" not in cache]
    if need:
        for k, v in itad("/lookup/id/shop/61/v1", need).items():  # 61 = Steam
            cache[f"itad:{k[4:]}"] = v
    ids = {cache[f"itad:{x['appid']}"]: x for x in deals if cache.get(f"itad:{x['appid']}")}
    res = []
    for i in range(0, len(ids), 100):
        res += itad("/games/prices/v3", list(ids)[i:i + 100], country="KR", deals="false", vouchers="true")

    def krw(price):
        nonlocal rate
        if price.get("currency") == "KRW":
            return int(price["amount"])
        if rate is None:
            rate = get_json("https://open.er-api.com/v6/latest/USD")["rates"]
        # 다른 통화는 USD 기준 환율로 환산
        return int(round(price["amount"] / rate.get(price["currency"], 1) * rate["KRW"] / 10) * 10)

    n = 0
    for g in res:
        x = ids.get(g.get("id"))
        if not x:
            continue
        shops = []
        for d in g.get("deals", []):
            if d["shop"]["id"] == 61 or not any(m.get("name") == "Steam" for m in d.get("drm", [])):
                continue  # Steam 자체 가격은 따로 쓰고, Steam 키가 아닌 판매(GOG·Epic 등)는 제외
            shops.append({"shop": d["shop"]["name"], "krw": krw(d["price"]), "cut": d.get("cut", 0),
                          "url": d["url"], "cur": d["price"].get("currency")})
        x["shops"] = sorted(shops, key=lambda s_: s_["krw"])[:4]
        low = ((g.get("historyLow") or {}).get("all") or {})
        if low.get("amount") is not None:
            x["ever_low"] = krw(low)
        n += 1
    for x in deals:
        x["best"] = min([x["final"]] + [s_["krw"] for s_ in x.get("shops", [])])
    print(f"판매처 가격 비교 {n}건 (ITAD, KR)")
    if not n:
        raise RuntimeError(f"ITAD 결과 없음: ids={len(ids)} res={len(res)} sample={json.dumps(res[:1], ensure_ascii=False)[:300]}")


def collect_all_deals():
    """Steam 에서 할인 중인 게임 전체(최대 DEALS_MAX)를 모아 site 검색용 data/deals_all.json 으로 저장."""
    if not os.environ.get("ITAD_API_KEY"):
        return
    out, off, cap = [], 0, int(os.environ.get("DEALS_MAX", 3000))
    while off is not None and len(out) < cap:
        r = itad("/deals/v2", country="KR", shops="61", limit=200, offset=off, sort="-trending")
        for g in r.get("list", []):
            d = g.get("deal") or {}
            if g.get("type") not in ("game", "dlc") or g.get("mature") or not d.get("cut"):
                continue
            out.append({"id": g["id"], "t": g["title"], "img": (g.get("assets") or {}).get("banner400") or (g.get("assets") or {}).get("banner300", ""),
                        "p": int(d["price"]["amount"]), "r": int(d["regular"]["amount"]), "c": d["cut"],
                        "lo": int((d.get("historyLow") or {}).get("amount") or 0), "u": d["url"],
                        "x": (d.get("expiry") or "")[:10], "dlc": g["type"] == "dlc"})
        off = r.get("nextOffset") if r.get("hasMore") else None
        time.sleep(0.3)
    # 판매처(Steam 키 판매 공인 상점) 최저가
    byid = {x["id"]: x for x in out}
    ids = list(byid)
    for i in range(0, len(ids), 200):
        try:
            res = itad("/games/prices/v3", ids[i:i + 200], country="KR", deals="false", vouchers="true")
        except Exception as e:
            print(f"  ! 전체 할인 판매처 가격: {e}")
            break
        for g in res:
            x = byid.get(g.get("id"))
            if not x:
                continue
            best = min(((int(d["price"]["amount"]), d["shop"]["name"], d["url"]) for d in g.get("deals", [])
                        if d["shop"]["id"] != 61 and d["price"].get("currency") == "KRW"
                        and any(m.get("name") == "Steam" for m in d.get("drm", []))), default=None)
            if best and best[0] < x["p"] * 0.97:
                x["bp"], x["bs"], x["bu"] = best
        time.sleep(0.3)
    save_json(DATA / "deals_all.json", {"updated": datetime.now().strftime("%m.%d %H:%M"), "list": out})
    print(f"전체 할인 {len(out)}건")


def attach_shops(deals, cache):
    """CheapShark 로 공인 판매처 현재가(USD)·역대 최저가를 가져와 원화로 환산해 붙인다."""
    rate = get_json("https://open.er-api.com/v6/latest/USD")["rates"]["KRW"]
    krw = lambda usd: int(round(float(usd) * rate / 10) * 10)
    gid, budget = {}, 20  # 요청 한도(429) 방지: 실행마다 새 게임은 20개까지만 조회해 캐시를 조금씩 채운다
    for x in deals:  # steamAppID → CheapShark gameID (캐시)
        k = f"cs:{x['appid']}"
        if k not in cache:
            if budget <= 0:
                continue
            budget -= 1
            try:
                r = cs_json(f"https://www.cheapshark.com/api/1.0/games?steamAppID={x['appid']}")
                cache[k] = r[0]["gameID"] if r else None
                time.sleep(1.5)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    break
                continue
            except Exception:
                continue
        if cache.get(k):
            gid[cache[k]] = x
    ids = list(gid)
    n = 0
    for i in range(0, len(ids), 25):
        try:
            res = cs_json("https://www.cheapshark.com/api/1.0/games?ids=" + ",".join(ids[i:i + 25]))
        except Exception as e:
            print(f"  ! CheapShark: {e}")
            continue
        time.sleep(1.5)
        for g, info in res.items():
            x = gid.get(g)
            if not x or not isinstance(info, dict):
                continue
            shops = sorted(({"shop": SHOPS[dl["storeID"]], "krw": krw(dl["price"]), "usd": float(dl["price"]),
                             "cut": round(float(dl["savings"])),
                             "url": "https://www.cheapshark.com/redirect?dealID=" + dl["dealID"]}
                            for dl in info.get("deals", []) if dl["storeID"] in SHOPS), key=lambda s_: s_["krw"])
            x["shops"] = shops[:4]
            low = info.get("cheapestPriceEver") or {}
            if low.get("price"):
                x["ever_low"] = krw(low["price"])
            n += 1
    print(f"판매처 가격 비교 {n}건 (환율 {rate:,.0f}원)")
    for x in deals:
        x["best"] = min([x["final"]] + [s_["krw"] for s_ in x.get("shops", [])])
    return rate


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


def calendar_images(rel, cache):
    """출시 캘린더 이미지: PC 게임은 Steam, 콘솔 게임·Steam 에 없는 게임은 IGDB."""
    for r in rel:
        if r.get("img"):
            continue
        # "(WW)" 같은 지역 표기, "– Nintendo Switch 2 Edition" 같은 판형 꼬리표를 떼고 찾는다
        name = re.sub(r"\s*\((?:WW|NA|EU|JP|AS)\)\s*$", "", r["title"])
        name = re.sub(r"\s*[–-]\s*(?:Nintendo Switch 2|Switch 2|PS5|Xbox Series X\|S)?\s*(?:Edition|Expanded Edition)\s*$", "", name)
        name = EDITION_RE.sub("", name).strip()
        app = (steam_find(r["title"], cache) or steam_find(name, cache)) if "PC" in r["platforms"] else None
        if app:
            r["img"], r["appid"] = app["img"], app["appid"]
            continue
        ig = igdb_find(r["title"], cache) or igdb_find(name, cache)
        if ig:
            r["img"] = ig["img"]
    print(f"캘린더 이미지 {sum(1 for r in rel if r.get('img'))}/{len(rel)}")


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
    calendar_images(rel, cache)
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
.b.공식{color:var(--ok)}.b.확인{color:var(--info)}.b.보도{color:var(--mut)}.b.루머{color:var(--warn)}
.stream{border-top:1px solid var(--line)}.stream a{display:grid;grid-template-columns:64px 1fr;gap:12px;
padding:12px 0;border-bottom:1px solid var(--line)}.stream .tm{color:var(--mut);font-size:13px;font-variant-numeric:tabular-nums}
.stream .t{font-weight:600}
article{max-width:680px;margin:40px auto}
.apage{display:grid;grid-template-columns:minmax(0,680px) 300px;gap:48px;justify-content:center}.apage article{margin:40px 0;max-width:none}
.side{margin:40px 0;display:flex;flex-direction:column;gap:16px}.side>div{position:relative}
.sbx{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.sbx h3{font-size:12px;letter-spacing:1px;color:var(--mut);margin:0 0 10px;font-weight:700}
.sbx ul{list-style:none;margin:0;padding:0}.sbx li{padding:7px 0;border-top:1px solid var(--line);font-size:14px;line-height:1.45}
.sbx li:first-child{border-top:0;padding-top:0}.sbx li a:hover{color:var(--acc)}.sbx .m{font-size:12px;color:var(--mut)}
.gcard img{width:calc(100% + 32px);margin:-14px -16px 12px;display:block;border-radius:12px 12px 0 0;aspect-ratio:460/215;object-fit:cover}
.gcard .n{font-weight:800;font-size:16px;margin-bottom:8px}.gcard dl{display:grid;grid-template-columns:64px 1fr;gap:5px 8px;margin:0;font-size:13px}
.gcard dt{color:var(--mut)}.gcard dd{margin:0}.kb{display:inline-block;font-size:11px;font-weight:700;border:1px solid var(--ok);color:var(--ok);border-radius:4px;padding:0 5px;margin-right:4px}
.kb.no{border-color:var(--line);color:var(--mut)}.gbtn{display:block;text-align:center;margin-top:12px;padding:8px;border-radius:8px;background:var(--fg);color:var(--bg);font-size:13px;font-weight:700}
.gbtn:hover{opacity:.85}.disc{color:#fff;background:var(--ok);border-radius:4px;padding:0 5px;font-size:12px;margin-right:4px}
.relx{margin:32px 0 8px}.relx .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.relx a{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;transition:transform .25s,border-color .25s}
.relx a:hover{transform:translateY(-3px);border-color:var(--mut)}.relx .t{font-weight:700;line-height:1.4}
@media(max-width:1080px){.apage{grid-template-columns:minmax(0,680px);gap:0}.apage article{margin-bottom:0}.side{margin-top:32px}}
@media(max-width:640px){.relx .grid{grid-template-columns:1fr}}article h1{font-size:30px;line-height:1.3;letter-spacing:-.6px;margin:8px 0 20px}
.tldr{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--acc);border-radius:8px;padding:14px 18px}
.tldr ul{margin:0;padding-left:18px}.why{font-weight:600;margin:20px 0}
.src{margin-top:32px;font-size:14px;border-top:1px solid var(--line);padding-top:16px}.src a{text-decoration:underline}
.note{font-size:12px;color:var(--mut);margin-top:24px}
.ad{margin:24px 0;min-height:90px;text-align:center}
.ad.ph{border:1px dashed var(--line);border-radius:8px;color:var(--mut);font-size:12px;display:flex;align-items:center;justify-content:center}
.doc{max-width:680px;margin:40px auto}.doc h1{font-size:28px}.doc a{text-decoration:underline}
footer a{margin:0 8px;text-decoration:underline}
.sbtn{margin-left:auto;flex:none;width:36px;height:36px;display:grid;place-items:center;border-radius:50%;color:var(--mut);transition:background .2s,color .2s,transform .2s}
.sbtn:hover{background:var(--line);color:var(--fg);transform:rotate(-8deg)}
.sbox{position:relative;margin:28px 0 8px}.sbox svg{position:absolute;left:16px;top:50%;transform:translateY(-50%);color:var(--mut)}
.sbox input{width:100%;font:inherit;font-size:18px;padding:14px 16px 14px 48px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--fg);outline:none;transition:border-color .2s,box-shadow .2s}
.sbox input:focus{border-color:var(--acc);box-shadow:0 0 0 4px rgba(229,72,77,.15)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 4px}.chips button{border:1px solid var(--line);background:none;color:var(--mut);border-radius:99px;padding:3px 11px;font:inherit;font-size:13px;cursor:pointer}
.chips button:hover{color:var(--fg);border-color:var(--mut)}
#sc{font-size:13px;color:var(--mut);margin:14px 0 4px}
mark{background:rgba(229,72,77,.22);color:inherit;border-radius:2px;padding:0 1px}
.sr-item .s{font-size:14px;color:var(--mut);margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.gnb{display:flex;gap:16px;font-size:14px;font-weight:600;color:var(--mut);overflow-x:auto;white-space:nowrap}.gnb a:hover{color:var(--fg)}
.brief a.hi{display:grid;grid-template-columns:1fr 220px;gap:18px;align-items:center}
.th{width:100%;aspect-ratio:460/215;object-fit:cover;border-radius:8px;background:var(--line);display:block}
.stream a.hi{grid-template-columns:64px 1fr 120px}.stream .th{border-radius:6px}
.hero{margin:0 0 20px}.cover{aspect-ratio:16/9;border-radius:10px;display:flex;flex-direction:column;justify-content:flex-end;padding:28px;color:#fff;
position:relative;overflow:hidden;background:radial-gradient(120% 90% at 85% 10%,var(--c1),transparent 60%),linear-gradient(135deg,var(--c2),#0b0b0e)}
.cover::before{content:"";position:absolute;inset:0;background:repeating-linear-gradient(115deg,rgba(255,255,255,.05) 0 2px,transparent 2px 22px);animation:cvs 18s linear infinite}
@keyframes cvs{to{background-position:220px 0}}
.cover b{font-size:clamp(26px,5vw,44px);font-weight:900;letter-spacing:-1px;line-height:1.1;position:relative}
.cover .cv-k{position:absolute;top:22px;left:28px;font:700 15px ui-monospace,Consolas,monospace;opacity:.8}
.cover .cv-p{font-size:13px;opacity:.75;margin-top:8px;position:relative}
.vid{margin:20px 0}.yt{position:relative;display:block;width:100%;padding:0;border:0;border-radius:10px;overflow:hidden;cursor:pointer;background:#000;aspect-ratio:16/9}
.yt img{width:100%;height:100%;object-fit:cover;display:block;opacity:.85;transition:opacity .3s,transform .5s}.yt:hover img{opacity:1;transform:scale(1.03)}
.yt .play{position:absolute;left:50%;top:50%;width:68px;height:48px;margin:-24px 0 0 -34px;border-radius:14px;background:#e5484d;transition:transform .2s}
.yt .play::after{content:"";position:absolute;left:27px;top:14px;border-style:solid;border-width:10px 0 10px 17px;border-color:transparent transparent transparent #fff}
.yt:hover .play{transform:scale(1.1)}.vid iframe{width:100%;aspect-ratio:16/9;border:0;border-radius:10px;display:block}
.vid figcaption{font-size:12px;color:var(--mut);margin-top:6px}
.hero img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px;display:block;background:var(--line)}.hero figcaption{font-size:12px;color:var(--mut);margin-top:6px}
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
.more{display:flex;justify-content:space-between;align-items:center;margin-top:16px;font-size:14px}
.more button{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:99px;padding:8px 22px;font:inherit;font-size:14px;cursor:pointer;transition:border-color .2s}
.more button:hover{border-color:var(--mut)}.more a{color:var(--mut)}.more a:hover{color:var(--acc)}
footer{color:var(--mut);font-size:12px;padding:40px 0;text-align:center}
@media(max-width:640px){article h1{font-size:24px}.brief .t{font-size:17px}}
"""


def esc(s):
    return html.escape(str(s or ""))


SITE_DESC = "업무 중 잠깐 Alt+Tab 하듯 확인하는 PC·콘솔 게임 뉴스 브리핑. 핵심 3줄 요약, 신뢰도 표시, Steam 패치노트, 출시 캘린더."


def site_url():
    return os.environ.get("SITE_URL", "https://khb950404-creator.github.io/gamewebzine/").rstrip("/") + "/"


def seo_head(title, desc, path, img, ld):
    url = site_url() + path
    tags = [f'<meta name="description" content="{esc(desc)}">', f'<link rel="canonical" href="{esc(url)}">',
            f'<link rel="alternate" type="application/rss+xml" title="{SITE_NAME}" href="{site_url()}rss.xml">',
            f'<meta property="og:type" content="{"article" if ld else "website"}">',
            f'<meta property="og:site_name" content="{SITE_NAME}">', f'<meta property="og:title" content="{esc(title)}">',
            f'<meta property="og:description" content="{esc(desc)}">', f'<meta property="og:url" content="{esc(url)}">',
            f'<meta property="og:locale" content="ko_KR">',
            f'<meta name="twitter:card" content="{"summary_large_image" if img else "summary"}">']
    if img:
        tags.append(f'<meta property="og:image" content="{esc(img)}">')
    for env, name in (("GOOGLE_SITE_VERIFICATION", "google-site-verification"),
                      ("NAVER_SITE_VERIFICATION", "naver-site-verification")):
        if os.environ.get(env):
            tags.append(f'<meta name="{name}" content="{esc(os.environ[env])}">')
    if ld:
        tags.append('<script type="application/ld+json">' + json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") + '</script>')
    return "\n".join(tags)


def page(title, body, desc=SITE_DESC, path="", img=None, ld=None):
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
{seo_head(title, desc, path, img, ld)}
<style>{CSS}</style>{ads_head()}</head><body>
<header><div class="wrap"><a class="logo" href="{{root}}index.html" aria-label="알트탭"><span class="k k1">alt</span><em>+</em><span class="k k2">tab</span><span class="cur"></span></a>
<div class="gnb"><a href="{{root}}index.html">뉴스</a><a href="{{root}}patches.html">패치노트</a><a href="{{root}}calendar.html">출시 캘린더</a><a href="{{root}}deals.html">할인</a></div>
<a class="sbtn" href="{{root}}search.html" aria-label="검색"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></a></div></header>
<div id="pg"></div><div class="wrap">{body}</div><footer><div><a href="{{root}}about.html">소개</a><a href="{{root}}privacy.html">개인정보처리방침</a><a href="{{root}}contact.html">문의</a></div>
<p>AI가 공개 출처를 종합해 작성하고 팩트체크를 거친 기사입니다 · 원문은 각 기사 하단 출처 참고</p></footer>{MOTION_JS}</body></html>"""


MOTION_JS = """<script>document.addEventListener('click',e=>{const b=e.target.closest('.yt');if(!b)return;
const f=document.createElement('iframe');f.src='https://www.youtube-nocookie.com/embed/'+b.dataset.v+'?autoplay=1&rel=0';
f.allow='autoplay; encrypted-media; picture-in-picture; fullscreen';f.allowFullscreen=true;f.title='영상';b.replaceWith(f)});
(()=>{
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
<li><b>확인</b> — 2개 이상의 매체가 보도</li><li><b>보도</b> — 매체 한 곳의 취재 기사</li>
<li><b>루머</b> — 유출, 익명 소식통, 미확인 소문 (편집자 검수 후 게시)</li></ul>
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
            page(f"{title} — {SITE_NAME}", f'<div class="doc"><h1>{title}</h1>{body}</div>', path=f"{name}.html").replace("{root}", ""),
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
.btn.ed.on{background:var(--info);color:#fff;border-color:var(--info)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:12px}
.card.ok{border-color:var(--ok)}.card.no{opacity:.5}.card.chg{border-color:var(--info)}
.card .t{font-size:18px;font-weight:800;line-height:1.35;margin:4px 0 8px}
.card ul{margin:6px 0;padding-left:18px;font-size:14px}.card .fx{color:var(--warn);font-size:13px}
.card .sr{font-size:13px;color:var(--mut)}.card .sr a{text-decoration:underline}
.card .act{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center}
.card select{padding:6px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:inherit;font-size:13px}
.ed-f{display:grid;gap:10px;margin-top:12px}.ed-f label{font-size:12px;color:var(--mut);font-weight:700;display:grid;gap:4px}
.ed-f input,.ed-f textarea{font:inherit;font-size:14px;line-height:1.6;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);width:100%}
.ed-f textarea{resize:vertical}.media-ed{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
.media-ed .pv{width:200px;aspect-ratio:16/9;object-fit:cover;border-radius:8px;border:1px solid var(--line)}.ed-f .row{display:flex;gap:8px;flex-wrap:wrap}
#msg{font-size:14px;color:var(--mut);min-height:20px}
.tabs{display:flex;gap:6px;margin:16px 0}
.save{position:sticky;bottom:16px;display:flex;justify-content:flex-end;margin-top:16px}
.tag{font-size:11px;font-weight:700;color:var(--info);margin-left:6px}
</style>
<h2 class="sec">기사 검수·편집</h2>
<div id="login">
  <p>GitHub 토큰을 한 번 입력하면 이 브라우저에만 저장됩니다.<br>
  <a style="text-decoration:underline" target="_blank" rel="noopener" href="https://github.com/settings/personal-access-tokens/new">토큰 만들기</a>
  → Repository access: <b>Only select repositories</b> → <b>__REPO__</b> 선택 → Permissions → Contents: <b>Read and write</b></p>
  <div class="rv-bar"><input id="tok" type="password" placeholder="github_pat_..."><button class="btn pri" id="go">연결</button></div>
</div>
<div id="app" hidden>
  <div class="tabs"><button class="btn pri" data-tab="todo">미확인 <span id="np"></span></button>
  <button class="btn" data-tab="all">전체 기사</button>
  <button class="btn" id="logout" style="margin-left:auto">토큰 지우기</button></div>
  <div id="list"></div>
  <div class="save"><button class="btn pri" id="save" disabled>변경사항 저장</button></div>
</div>
<p id="msg"></p>
<script>
const REPO = "__REPO__", PATH = "data/articles.json", API = "https://api.github.com/repos/" + REPO + "/contents/" + PATH;
const CATS = __CATS__, TRUSTS = ["공식", "확인", "보도", "루머"];
let tok = "", arts = [], tab = "todo", editing = null;
const changes = {};  // id -> 바뀐 필드들
const uploads = {};  // id -> {url: dataURL, b64} 저장할 때 media/ 에 올림
const MEDIA_API = "https://api.github.com/repos/" + REPO + "/contents/";
async function shrink(file) {  // 큰 사진은 가로 1600px JPEG 로 줄여서 올린다
  const img = await createImageBitmap(file), k = Math.min(1, 1600 / img.width);
  const c = document.createElement("canvas"); c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
  c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
  const url = c.toDataURL("image/jpeg", 0.86);
  return {url, b64: url.split(",")[1]};
}
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
const cur = a => ({...a, ...(changes[a.id] || {})});
const setc = (id, k, v, orig) => {
  const c = changes[id] = changes[id] || {};
  if (JSON.stringify(v) === JSON.stringify(orig)) delete c[k]; else c[k] = v;
  if (!Object.keys(c).length) delete changes[id];
};
const isTodo = a => a.status !== "rejected" && !a.reviewed;
function editor(a) {
  return `<div class="ed-f" data-ed="${esc(a.id)}">
    <label>제목<input data-k="title" value="${esc(a.title)}"></label>
    <label>3줄 요약 (한 줄에 하나)<textarea data-k="tldr" rows="3">${esc((a.tldr || []).join("\n"))}</textarea></label>
    <label>한 줄 설명<textarea data-k="why" rows="2">${esc(a.why)}</textarea></label>
    <label>본문 (문단 사이 빈 줄)<textarea data-k="body" rows="10">${esc((a.body || []).join("\n\n"))}</textarea></label>
    <div class="row"><label>신뢰도<select data-k="trust">${TRUSTS.map(t => `<option ${t === a.trust ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    <label>카테고리<select data-k="category">${CATS.map(t => `<option ${t === a.category ? "selected" : ""}>${t}</option>`).join("")}</select></label></div>
    <div class="media-ed"><img class="pv" src="${esc(uploads[a.id] ? uploads[a.id].url : (a.img_hd || a.img || ""))}" ${a.img || uploads[a.id] ? "" : "hidden"} alt="">
      <div style="flex:1;display:grid;gap:6px"><label>대표 이미지 주소 (비우면 카테고리 커버)<input data-k="img" value="${esc(uploads[a.id] ? "(업로드한 이미지)" : (a.img_hd || a.img || ""))}" placeholder="https://..."></label>
      <div class="row"><label class="btn" style="display:inline-flex">내 PC에서 업로드<input type="file" accept="image/*" data-up="${esc(a.id)}" hidden></label>
      <button class="btn" data-noimg="${esc(a.id)}">이미지 빼기</button></div></div></div>
    <label>영상 (YouTube 주소, 비우면 없음)<input data-k="video" value="${esc(a.video ? "https://youtu.be/" + a.video.id : "")}" placeholder="https://youtu.be/..."></label>
    <div class="row"><button class="btn pri" data-done="${esc(a.id)}">편집 완료</button>
    <button class="btn" data-revert="${esc(a.id)}">원래대로</button></div></div>`;
}
function render() {
  const todo = arts.filter(isTodo);
  $("#np").textContent = todo.length;
  document.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("pri", b.dataset.tab === tab));
  const list = (tab === "todo" ? todo : arts.filter(a => a.status !== "rejected")).slice().sort((x, y) => y.ts - x.ts).slice(0, 60);
  $("#list").innerHTML = list.length ? list.map(a0 => {
    const a = cur(a0), ch = changes[a.id] || {}, edited = ["title", "tldr", "why", "body", "trust", "category", "img", "video"].some(k => k in ch) || uploads[a.id];
    return `<div class="card ${ch.reviewed ? "ok" : ""} ${ch.status === "rejected" ? "no" : ""} ${edited ? "chg" : ""}">
      <div class="meta"><span class="b ${esc(a.trust)}">${esc(a.trust)}</span><span>${esc(a.category)}</span>
      <span>· ${new Date(a.ts * 1000).toLocaleString("ko-KR", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"})}</span>
      ${a.status === "pending" ? '<span class="tag">비공개</span>' : ""}${edited ? '<span class="tag">수정됨</span>' : ""}</div>
      <div class="t"><a href="a/${esc(a.id)}.html" target="_blank">${esc(a.title)}</a></div>
      <ul>${(a.tldr || []).map(t => `<li>${esc(t)}</li>`).join("")}</ul>
      ${(a.fixes || []).map(f => `<div class="fx">! 팩트체크: ${esc(f)}</div>`).join("")}
      <div class="sr">출처: ${(a.sources || []).map(s => `<a href="${esc(s.link)}" target="_blank" rel="noopener">${esc(s.name)}</a>`).join(", ")}</div>
      <div class="act">
        ${a0.reviewed ? "" : `<button class="btn ok ${ch.reviewed ? "on" : ""}" data-id="${esc(a.id)}" data-act="ok">확인 완료</button>`}
        <button class="btn ed ${editing === a.id ? "on" : ""}" data-id="${esc(a.id)}" data-act="edit">수정</button>
        <button class="btn no ${ch.status === "rejected" ? "on" : ""}" data-id="${esc(a.id)}" data-act="del">내리기</button>
      </div>${editing === a.id ? editor(a) : ""}</div>`;
  }).join("") : `<p>${tab === "todo" ? "확인할 기사가 없습니다." : "기사가 없습니다."}</p>`;
  const n = Object.keys(changes).length;
  $("#save").disabled = !n; $("#save").textContent = n ? `변경사항 ${n}건 저장` : "변경사항 저장";
}
function readEditor(id) {
  const f = document.querySelector(`[data-ed="${id}"]`); if (!f) return;
  const a0 = arts.find(a => a.id === id), v = k => f.querySelector(`[data-k="${k}"]`).value;
  setc(id, "title", v("title").trim(), a0.title);
  setc(id, "tldr", v("tldr").split("\n").map(s => s.trim()).filter(Boolean), a0.tldr || []);
  setc(id, "why", v("why").trim(), a0.why || "");
  setc(id, "body", v("body").split(/\n\s*\n/).map(s => s.replace(/\s*\n\s*/g, " ").trim()).filter(Boolean), a0.body || []);
  setc(id, "trust", v("trust"), a0.trust);
  setc(id, "category", v("category"), a0.category);
  if (!uploads[id]) {
    const im = v("img").trim(), orig = a0.img_hd || a0.img || "";
    if (im !== orig) { const c = changes[id] = changes[id] || {};
      Object.assign(c, im ? {img: im, img_hd: im, img_src: "편집자"} : {img: null, img_hd: null, img_src: null}, {img_v: 2}); }
  }
  const vm = v("video").trim().match(/(?:youtu\.be\/|v=|embed\/|shorts\/)([\w-]{11})/) || v("video").trim().match(/^([\w-]{11})$/);
  const vid = vm ? vm[1] : null, ov = a0.video ? a0.video.id : null;
  if (vid !== ov) { const c = changes[id] = changes[id] || {}; c.video = vid ? {id: vid, title: "", channel: "편집자 지정"} : null; c.video_v = 1; }
}
$("#list").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.done) { readEditor(b.dataset.done); editing = null; return render(); }
  if (b.dataset.noimg) { const f = document.querySelector(`[data-ed="${b.dataset.noimg}"]`);
    delete uploads[b.dataset.noimg]; f.querySelector('[data-k="img"]').value = ""; f.querySelector(".pv").hidden = true; return; }
  if (b.dataset.revert) { const id = b.dataset.revert, c = changes[id] || {};
    ["title", "tldr", "why", "body", "trust", "category", "img", "img_hd", "img_src", "img_v", "video", "video_v"].forEach(k => delete c[k]);
    delete uploads[id];
    if (!Object.keys(c).length) delete changes[id]; editing = null; return render(); }
  const id = b.dataset.id, a0 = arts.find(a => a.id === id); if (!a0) return;
  if (editing && editing !== id) readEditor(editing);
  if (b.dataset.act === "ok") setc(id, "reviewed", !(changes[id] || {}).reviewed, a0.reviewed || false);
  if (b.dataset.act === "del") setc(id, "status", (changes[id] || {}).status === "rejected" ? a0.status : "rejected", a0.status);
  if (b.dataset.act === "edit") { if (editing === id) { readEditor(id); editing = null; } else editing = id; }
  render();
});
$("#list").addEventListener("change", async e => {
  const id = e.target.dataset && e.target.dataset.up; if (!id || !e.target.files[0]) return;
  msg("이미지 준비 중...");
  uploads[id] = await shrink(e.target.files[0]);
  const c = changes[id] = changes[id] || {}; c.img_v = 2;
  const f = document.querySelector(`[data-ed="${id}"]`);
  f.querySelector(".pv").src = uploads[id].url; f.querySelector(".pv").hidden = false;
  f.querySelector('[data-k="img"]').value = "(업로드한 이미지)"; msg("업로드는 '변경사항 저장' 때 올라갑니다.");
});
$("#save").onclick = async () => {
  if (editing) { readEditor(editing); editing = null; }
  $("#save").disabled = true;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      msg("저장 중...");
      for (const id of Object.keys(uploads)) {  // 업로드 이미지 먼저 저장소 media/ 에 올림
        const path = `media/${id}-${Date.now()}.jpg`;
        const r = await fetch(MEDIA_API + path, {method: "PUT", headers: {Authorization: "Bearer " + tok, Accept: "application/vnd.github+json"},
          body: JSON.stringify({message: `검수: 이미지 업로드 (${id})`, content: uploads[id].b64, branch: "main"})});
        if (!r.ok) throw new Error("이미지 업로드 실패 " + r.status);
        Object.assign(changes[id] = changes[id] || {}, {img: path, img_hd: path, img_src: "편집자", img_v: 2});
        delete uploads[id];
      }
      const {sha, data} = await fetchLatest();  // 봇이 그사이 새 기사를 썼을 수 있으니 최신본에 적용
      const n = Object.keys(changes).length;
      for (const a of data) if (changes[a.id]) {
        const c = changes[a.id];
        if (["title", "tldr", "why", "body", "img", "video"].some(k => k in c)) { c.edited = Date.now() / 1000; c.reviewed = true; }
        if (a.status === "pending" && (c.reviewed || c.edited)) c.status = c.status || "published";
        Object.assign(a, c);
      }
      await gh({method: "PUT", body: JSON.stringify({
        message: `검수: ${n}건 처리`, sha, content: b64(JSON.stringify(data, null, 1)), branch: "main"})});
      for (const k in changes) delete changes[k];
      arts = data; render();
      msg("저장했습니다. 1~3분 뒤 사이트에 반영됩니다.");
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
document.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => { if (editing) readEditor(editing); editing = null; tab = b.dataset.tab; render(); });
addEventListener("beforeunload", e => { if (Object.keys(changes).length) e.preventDefault(); });
if (tok) load();
</script>"""


def build_feeds(articles):
    base = site_url()
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    now = time.time()
    urls = [("", now, "hourly", "1.0"), ("patches.html", now, "daily", "0.7"), ("calendar.html", now, "daily", "0.7"), ("deals.html", now, "daily", "0.8"),
            ("about.html", None, "monthly", "0.3"), ("privacy.html", None, "yearly", "0.1"), ("contact.html", None, "yearly", "0.1")]
    urls += [(f"a/{a['id']}.html", a.get("written", a["ts"]), "weekly", "0.8") for a in articles]
    sm = "".join(f"<url><loc>{html.escape(base + u)}</loc>" + (f"<lastmod>{iso(t)}</lastmod>" if t else "")
                 + f"<changefreq>{c}</changefreq><priority>{pr}</priority></url>\n" for u, t, c, pr in urls)
    (SITE / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + sm + "</urlset>\n", encoding="utf-8")
    rfc = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items = "".join(
        f"<item><title>{html.escape(a['title'])}</title><link>{base}a/{a['id']}.html</link>"
        f"<guid isPermaLink=\"true\">{base}a/{a['id']}.html</guid><pubDate>{rfc(a.get('written', a['ts']))}</pubDate>"
        f"<category>{html.escape(a.get('category', ''))}</category>"
        f"<description>{html.escape(' / '.join(a.get('tldr', [])) or a.get('why', ''))}</description></item>\n"
        for a in articles[:50])
    (SITE / "rss.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>{SITE_NAME}</title>'
        f"<link>{base}</link><description>{html.escape(SITE_DESC)}</description><language>ko</language>"
        f"<lastBuildDate>{rfc(now)}</lastBuildDate>\n{items}</channel></rss>\n", encoding="utf-8")
    # 프로젝트 주소(github.io/저장소)에선 robots.txt 가 무시되지만, 자체 도메인 연결 후를 위해 생성
    (SITE / "robots.txt").write_text(f"User-agent: *\nDisallow: /review.html\nSitemap: {base}sitemap.xml\n", encoding="utf-8")


def build_search(articles):
    idx = [{"id": a["id"], "t": a["title"], "s": " ".join(a.get("tldr", [])) or a.get("why", ""),
            "k": " ".join([a.get("why", ""), " ".join(a.get("body", [])), " ".join(a.get("games", [])),
                           " ".join(a.get("games_en", [])), a.get("category", ""), " ".join(a.get("platforms", []))]),
            "c": a.get("category", ""), "p": a.get("platforms", []), "tr": a.get("trust", ""), "ts": a["ts"],
            "img": a.get("img")} for a in articles]
    (SITE / "search.json").write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (SITE / "search.html").write_text(page("검색 — " + SITE_NAME, SEARCH_BODY, path="search.html").replace("{root}", ""), encoding="utf-8")


SEARCH_BODY = r"""
<div class="sbox"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
<input id="q" type="search" placeholder="게임, 회사, 플랫폼으로 검색" autocomplete="off" autofocus></div>
<div class="chips" id="chips"></div>
<p id="sc"></p><div class="stream" id="res"></div>
<script>(async()=>{
const $=s=>document.querySelector(s), esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let data=[];try{data=await (await fetch("search.json",{cache:"no-cache"})).json()}catch(e){$("#sc").textContent="검색 데이터를 불러오지 못했습니다."}
const norm=s=>String(s??"").toLowerCase().normalize("NFC");
data.forEach(d=>{d.h=norm([d.t,d.s,d.k].join(" "));d.ht=norm(d.t)});
["PS5","Xbox","Switch 2","PC","출시","업데이트","연기","루머"].forEach(w=>{const b=document.createElement("button");b.textContent=w;b.onclick=()=>{$("#q").value=w;run()};$("#chips").append(b)});
const fmt=ts=>{const d=new Date(ts*1000);return (d.getMonth()+1+"").padStart(2,"0")+"."+(d.getDate()+"").padStart(2,"0")};
const hl=(s,terms)=>{let h=esc(s);terms.forEach(t=>{if(!t)return;const re=new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g,"\\$&"),"gi");h=h.replace(re,m=>"<mark>"+m+"</mark>")});return h};
function run(){
  const q=$("#q").value.trim(), terms=norm(q).split(/\s+/).filter(Boolean);
  history.replaceState(null,"",q?"?q="+encodeURIComponent(q):location.pathname);
  if(!terms.length){$("#sc").textContent=`기사 ${data.length}건에서 검색합니다.`;$("#res").innerHTML="";return}
  const hits=data.filter(d=>terms.every(t=>d.h.includes(t)))
    .map(d=>({d,score:terms.reduce((a,t)=>a+(d.ht.includes(t)?10:1),0)}))
    .sort((a,b)=>b.score-a.score||b.d.ts-a.d.ts).slice(0,60);
  $("#sc").textContent=hits.length?`"${q}" 검색 결과 ${hits.length}건`:`"${q}"에 대한 결과가 없습니다.`;
  $("#res").innerHTML=hits.map(({d})=>`<a class="sr-item" href="a/${esc(d.id)}.html"><span class="tm">${fmt(d.ts)}</span>
   <div><div class="meta"><span class="b ${esc(d.tr)}">${esc(d.tr)}</span><span>${esc(d.c)}</span>${d.p.map(x=>`<span>${esc(x)}</span>`).join("")}</div>
   <div class="t">${hl(d.t,terms)}</div><div class="s">${hl(d.s,terms)}</div></div>
   </a>`).join("");
  document.querySelectorAll("#res a").forEach((el,i)=>{el.animate([{opacity:0,transform:"translateY(10px)"},{opacity:1,transform:"none"}],{duration:350,delay:Math.min(i,10)*30,easing:"cubic-bezier(.2,.7,.2,1)",fill:"backwards"})});
}
let tm;$("#q").addEventListener("input",()=>{clearTimeout(tm);tm=setTimeout(run,120)});
const q0=new URLSearchParams(location.search).get("q");if(q0)$("#q").value=q0;run();
})()</script>"""


def build_deals():
    d = load_json(DATA / "deals.json", {"deals": []})
    alld = load_json(DATA / "deals_all.json", {"list": []})
    (SITE / "deals_all.json").write_text(json.dumps(alld, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    won = lambda n: f"₩{n:,}"
    def badges(x):
        b = '<span class="kb">자막</span>' if x["ko"] else '<span class="kb no">한국어 X</span>'
        b += '<span class="kb">음성</span>' if x["ko_voice"] else ""
        return b

    steam = lambda x: f"https://store.steampowered.com/app/{x['appid']}"

    def compare(x):
        shops = x.get("shops", [])
        cheaper = [s_ for s_ in shops if s_["krw"] < x["final"] * 0.97]  # 3% 이상 싸야 의미 있게 표시
        if cheaper:
            b = cheaper[0]
            head = (f'<a class="best" href="{esc(b["url"])}" target="_blank" rel="noopener sponsored">'
                    f'<span>최저가 · {esc(b["shop"])}</span><b>{won(b["krw"])}</b>'
                    f'<em>Steam보다 {round((1 - b["krw"] / x["final"]) * 100)}%↓</em></a>')
        else:
            head = f'<a class="best st" href="{steam(x)}" target="_blank" rel="noopener"><span>Steam이 최저가</span><b>{won(x["final"])}</b></a>'
        rows = "".join(f'<li><a href="{esc(s_["url"])}" target="_blank" rel="noopener sponsored">{esc(s_["shop"])}'
                       f'<span>{won(s_["krw"])}</span></a></li>' for s_ in shops)
        rows = f'<li><a href="{steam(x)}" target="_blank" rel="noopener">Steam (한국)<span>{won(x["final"])}</span></a></li>' + rows
        ever = f'<div class="ever">역대 최저 ≈ {won(x["ever_low"])}</div>' if x.get("ever_low") else ""
        more = f'<details><summary>판매처 {len(shops) + 1}곳 가격 비교</summary><ul>{rows}</ul>{ever}</details>' if shops else ever
        return head + more

    cards = "".join(
        f'<div class="deal" data-m="{x.get("meta") or 0}" data-d="{x["discount"]}" data-r="{x["rank"]}" data-p="{x.get("best", x["final"])}" '
        f'data-ko="{int(x["ko"])}" data-ch="{int(x.get("best", x["final"]) < x["final"] * 0.97)}" '
        f'data-g="{esc("|".join(x["genres"]))}"><a class="dimg" href="{steam(x)}" target="_blank" rel="noopener">'
        f'<img src="{esc(x["img"])}" alt="" loading="lazy">'
        f'<span class="pct">-{x["discount"]}%</span>{"<span class=low>최저가</span>" if x.get("low") else ""}'
        + (f'<span class="ms {"g" if x["meta"] >= 75 else "y" if x["meta"] >= 50 else "r"}" title="메타크리틱 점수">'
           f'<small>META</small>{x["meta"]}</span>' if x.get("meta") else "") + '</a>'
        f'<a class="dn" href="{steam(x)}" target="_blank" rel="noopener">{esc(x["name"])}</a>'
        f'<div class="dp"><s>{won(x["initial"])}</s><span>Steam</span><b>{won(x["final"])}</b></div>'
        f'<div class="cmp">{compare(x)}</div>'
        f'<div class="dm">{badges(x)}<span class="dg">{esc(" · ".join(x["genres"][:2]))}</span></div></div>'
        + (f'<div class="dad">{ad("ADSENSE_SLOT_DEALS")}</div>' if i % 12 == 11 else "")
        for i, x in enumerate(d["deals"]))
    genres = sorted({g for x in d["deals"] for g in x["genres"]}, key=lambda g: -sum(g in x["genres"] for x in d["deals"]))[:8]
    (SITE / "deals.html").write_text(page("게임 할인 — " + SITE_NAME, f"""
<style>
.dbar{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0 16px}}
.dbar select,.dbar label{{font:inherit;font-size:13px;color:var(--fg)}}.dbar select{{padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--card)}}
.dbar label{{display:flex;gap:6px;align-items:center;cursor:pointer;border:1px solid var(--line);border-radius:99px;padding:4px 12px}}
.dgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}}
.deal{{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column;transition:transform .25s,box-shadow .25s,border-color .25s}}
.deal:hover{{transform:translateY(-4px);box-shadow:0 16px 32px -18px rgba(0,0,0,.4);border-color:var(--mut)}}
.dimg{{position:relative;overflow:hidden}}.dimg img{{width:100%;aspect-ratio:460/215;object-fit:cover;display:block;transition:transform .5s}}
.deal:hover .dimg img{{transform:scale(1.05)}}
.pct{{position:absolute;right:8px;bottom:8px;background:#4c6b22;color:#beee11;font-weight:900;font-size:15px;padding:2px 8px;border-radius:6px}}
.low{{position:absolute;left:8px;top:8px;background:var(--acc);color:#fff;font-size:11px;font-weight:800;padding:2px 7px;border-radius:4px;animation:pulse 1.6s infinite}}
.dn{{font-weight:800;padding:10px 12px 2px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.dp{{padding:2px 12px;display:flex;gap:8px;align-items:baseline}}.dp s{{color:var(--mut);font-size:13px}}.dp b{{font-size:17px}}
.dm{{padding:6px 12px 12px;margin-top:auto;display:flex;gap:4px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--mut)}}
.dp span{{font-size:12px;color:var(--mut);margin-left:auto}}
.cmp{{padding:6px 12px 0}}.best{{display:grid;grid-template-columns:1fr auto;align-items:center;gap:0 8px;padding:7px 10px;border-radius:8px;
background:rgba(31,157,85,.1);border:1px solid rgba(31,157,85,.35);transition:background .2s}}.best:hover{{background:rgba(31,157,85,.18)}}
.best span{{font-size:12px;color:var(--ok);font-weight:700}}.best b{{font-size:16px;grid-row:span 2}}.best em{{font-style:normal;font-size:11px;color:var(--ok)}}
.best.st{{background:none;border-color:var(--line)}}.best.st span{{color:var(--mut)}}
.cmp details{{margin-top:6px;font-size:13px}}.cmp summary{{cursor:pointer;color:var(--mut);font-size:12px;list-style:none}}
.cmp summary::before{{content:"▸ "}}.cmp details[open] summary::before{{content:"▾ "}}
.cmp ul{{list-style:none;margin:6px 0 0;padding:0}}.cmp li a{{display:flex;justify-content:space-between;padding:4px 0;border-top:1px dashed var(--line)}}
.cmp li a:hover{{color:var(--acc)}}.ever{{font-size:12px;color:var(--mut);margin-top:6px}}
.ms{{position:absolute;left:8px;bottom:8px;display:flex;flex-direction:column;align-items:center;justify-content:center;
width:42px;height:42px;border-radius:8px;color:#fff;font-weight:900;font-size:18px;line-height:1;box-shadow:0 4px 12px rgba(0,0,0,.35)}}
.ms small{{font-size:8px;letter-spacing:.5px;opacity:.9;margin-bottom:2px}}.ms.g{{background:#3fa72e}}.ms.y{{background:#d9a400}}.ms.r{{background:#e5484d}}
.dad{{grid-column:1/-1}}.dad .ad{{margin:4px 0}}
.exp{{font-size:11px;color:var(--warn);padding:0 12px}}
.mc{{background:#66cc33;color:#fff;font-weight:800;border-radius:4px;padding:0 5px;font-size:11px}}.dg{{margin-left:auto}}
</style>
<div class="sbox"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
<input id="dq" type="search" placeholder="할인 중인 게임 전체에서 검색 ({len(alld["list"]):,}개)" autocomplete="off"></div>
<div id="dres" hidden><p id="dsc" class="note"></p><div class="dgrid" id="drg"></div></div>
<div id="dtop">{ad("ADSENSE_SLOT_DEALS")}
<h2 class="sec">Steam 할인 — 인기 판매 순위</h2>
<div class="dbar"><select id="sort"><option value="r">인기순</option><option value="d">할인율 높은 순</option><option value="p">최저가 낮은 순</option><option value="m">메타스코어 높은 순</option></select>
<label><input type="checkbox" id="ko"> 한국어 지원만</label>
<label><input type="checkbox" id="ch"> Steam보다 싼 곳 있음</label>
<select id="g"><option value="">전체 장르</option>{"".join(f"<option>{esc(g)}</option>" for g in genres)}</select></div>
<div class="dgrid" id="dg">{cards or "<p>할인 정보를 불러오지 못했습니다.</p>"}</div></div>
<p class="note">Steam 인기 판매 상위 100개 중 할인 중인 게임 · 한국 가격 기준 · 업데이트 {esc(d.get("updated", ""))}<br>
판매처는 Steam 키를 판매하는 공인 상점만 비교합니다(가격 정보: IsThereAnyDeal). 원화 결제가 안 되는 상점은 오늘 환율로 환산한 값이며,
결제 시 카드 해외결제 수수료가 붙을 수 있고 일부 게임은 한국 지역 제한이 있을 수 있으니 구매 전 확인하세요.<br>
이미지 위 "최저가" 배지는 알트탭이 기록한 Steam 가격 중 가장 낮은 경우입니다.</p>
<script>(()=>{{const g=document.getElementById('dg'),cs=[...g.querySelectorAll('.deal')],ads=[...g.querySelectorAll('.dad')];
function f(){{const s=document.getElementById('sort').value,ko=document.getElementById('ko').checked,ch=document.getElementById('ch').checked,ge=document.getElementById('g').value;
cs.sort((a,b)=>s==='m'?b.dataset.m-a.dataset.m:s==='d'?b.dataset.d-a.dataset.d:s==='p'?a.dataset.p-b.dataset.p:a.dataset.r-b.dataset.r)
.forEach(c=>{{c.style.display=(!ko||c.dataset.ko==='1')&&(!ch||c.dataset.ch==='1')&&(!ge||c.dataset.g.split('|').includes(ge))?'':'none';g.appendChild(c)}})}}
['sort','ko','ch','g'].forEach(i=>document.getElementById(i).onchange=()=>{{f();ads.forEach((a,i)=>g.insertBefore(a,[...g.querySelectorAll('.deal')][(i+1)*12]||null))}})
// 전체 검색 (처음 입력할 때 목록을 불러옴)
let all=null,tm;const q=document.getElementById('dq'),res=document.getElementById('dres'),top=document.getElementById('dtop');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c])),won=n=>'₩'+Number(n).toLocaleString('ko-KR');
async function run(){{const v=q.value.trim().toLowerCase();if(!v){{res.hidden=true;top.hidden=false;return}}
 if(!all){{document.getElementById('dsc').textContent='불러오는 중...';res.hidden=false;all=(await (await fetch('deals_all.json')).json()).list}}
 const terms=v.split(/\s+/),hit=all.filter(x=>terms.every(t=>x.t.toLowerCase().includes(t))).slice(0,60);
 top.hidden=true;res.hidden=false;document.getElementById('dsc').textContent=hit.length?`"${{q.value}}" 할인 중 ${{hit.length}}건${{hit.length===60?' (상위 60건)':''}}`:`"${{q.value}}" — 지금 할인 중인 게임이 없습니다.`;
 document.getElementById('drg').innerHTML=hit.map(x=>`<div class="deal"><a class="dimg" href="${{esc(x.u)}}" target="_blank" rel="noopener"><img src="${{esc(x.img)}}" alt="" loading="lazy"><span class="pct">-${{x.c}}%</span>${{x.lo&&x.p<=x.lo?'<span class="low">역대 최저</span>':''}}</a>
 <a class="dn" href="${{esc(x.u)}}" target="_blank" rel="noopener">${{esc(x.t)}}${{x.dlc?' <small>(DLC)</small>':''}}</a><div class="dp"><s>${{won(x.r)}}</s><span>Steam</span><b>${{won(x.p)}}</b></div>
 <div class="cmp">${{x.bp?`<a class="best" href="${{esc(x.bu)}}" target="_blank" rel="noopener sponsored"><span>최저가 · ${{esc(x.bs)}}</span><b>${{won(x.bp)}}</b><em>Steam보다 ${{Math.round((1-x.bp/x.p)*100)}}%↓</em></a>`:`<a class="best st" href="${{esc(x.u)}}" target="_blank" rel="noopener"><span>Steam이 최저가</span><b>${{won(x.p)}}</b></a>`}}
 ${{x.lo?`<div class="ever">역대 최저 ${{won(x.lo)}}</div>`:''}}</div>${{x.x?`<div class="exp">할인 종료 ${{x.x.slice(5).replace('-','.')}}</div>`:''}}<div class="dm"></div></div>`).join('')}}
q.addEventListener('input',()=>{{clearTimeout(tm);tm=setTimeout(run,150)}})}})()</script>""",
        path="deals.html", desc="Steam 인기 게임 할인 정보. 한국 가격, 할인율, 한국어 지원 여부, 최저가를 한눈에.").replace("{root}", ""),
        encoding="utf-8")


def build_patches():
    ps = [x for x in load_json(DATA / "patches.json", []) if not x.get("skip")][:80]
    rows = "".join(f"""<div class="pt"><a href="{esc(x['url'])}" target="_blank" rel="noopener">
<img class="th" src="{esc(x['img'])}" alt="" loading="lazy"></a><div>
<div class="g">{esc(x['game'])} · {fmt_time(x['ts'])}</div><div class="t">{esc(x.get('title'))}</div>
<ul>{''.join(f'<li>{esc(t)}</li>' for t in x.get('points', []))}</ul>
<a class="lk" href="{esc(x['url'])}" target="_blank" rel="noopener">Steam 원문 패치노트</a></div></div>""" for x in ps)
    (SITE / "patches.html").write_text(page("패치노트 — " + SITE_NAME, f"""
<h2 class="sec">Steam 인기 게임 패치노트</h2><!--seo-->
<p class="note">Steam 동시접속 상위 게임의 최근 업데이트를 AI가 한국어로 요약했습니다. 정확한 내용은 원문을 확인하세요.</p>
{rows or '<p>아직 수집된 패치노트가 없습니다.</p>'}""", path="patches.html",
        desc="Steam 인기 게임의 최신 패치노트를 한국어 3줄로 요약. 업데이트 내용을 빠르게 확인하세요.").replace("{root}", ""), encoding="utf-8")


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
            + ((f'<a href="https://store.steampowered.com/app/{r["appid"]}" target="_blank" rel="noopener">'
                f'<img class="th" src="{esc(r["img"])}" alt="" loading="lazy"></a>' if r.get("appid") else
                f'<img class="th" src="{esc(r["img"])}" alt="" loading="lazy">') if r.get("img") else '<div class="nt"></div>')
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
const t=document.getElementById('today');if(t)t.scrollIntoView({{block:'start'}})</script>""", path="calendar.html",
        desc="PC·PS5·Xbox·닌텐도 스위치 게임 출시 일정을 날짜별로 정리한 출시 캘린더.").replace("{root}", ""),
        encoding="utf-8")


REL_STOP = set("출시 공개 발표 업데이트 신작 게임 예정 추가 기념 진행 소식 정보 관련 버전 시리즈 이번 위한".split())


def rel_keys(a):
    games = {norm(g) for g in (a.get("games_en") or []) + (a.get("games") or []) if norm(g)}
    words = {w for w in re.findall(r"[0-9A-Za-z가-힣]{2,}", a["title"].lower()) if w not in REL_STOP}
    return games, words


def related(a, pool, n=4):
    ga, wa = rel_keys(a)
    scored = []
    for b in pool:
        if b["id"] == a["id"]:
            continue
        gb, wb = rel_keys(b)
        sc = (6 if a.get("appid") and a.get("appid") == b.get("appid") else 0) + 4 * len(ga & gb) + len(wa & wb)
        if sc >= 2:
            scored.append((sc + (0.5 if a.get("category") == b.get("category") else 0), b))
    return [b for _, b in sorted(scored, key=lambda x: (-x[0], -x[1]["ts"]))[:n]]


COVER_COLORS = {"신작": ("#e5484d", "#7c1d3f"), "업데이트": ("#2563eb", "#0f2a6b"), "업계": ("#7c3aed", "#2e1065"),
                "하드웨어": ("#0f766e", "#042f2e"), "할인·이벤트": ("#d97706", "#5b2c06"), "e스포츠": ("#db2777", "#4a0d2a")}


def rel_img(u):
    return u if u.startswith("http") else "../" + u


def cover_html(a):
    """이미지가 없는 기사용 커버: 카테고리 색 + 게임명/플랫폼 타이포."""
    c1, c2 = COVER_COLORS.get(a.get("category"), ("#3f3f46", "#0f0f12"))
    name = (a.get("games") or a.get("games_en") or [a.get("category") or SITE_NAME])[0]
    plats = " · ".join(a.get("platforms", []))
    return (f'<figure class="hero"><div class="cover" style="--c1:{c1};--c2:{c2}"><span class="cv-k">alt+tab</span>'
            f'<b>{esc(name)}</b><span class="cv-p">{esc(a.get("category") or "")}{" · " + esc(plats) if plats else ""}</span></div></figure>')


def related_html(a, pool):
    rel = related(a, pool)
    # 연관 기사가 부족하면 같은 카테고리·플랫폼의 최근 기사로 채운다
    if len(rel) < 4:
        ids = {a["id"]} | {b["id"] for b in rel}
        same = [b for b in pool if b["id"] not in ids and (b.get("category") == a.get("category")
                or set(b.get("platforms", [])) & set(a.get("platforms", [])))]
        rel += same[:4 - len(rel)]
    if not rel:
        return ""
    return ('<section class="relx"><h2 class="sec">관련 기사</h2><div class="grid">' + "".join(
        f'<a href="{b["id"]}.html">{meta(b)}<div class="t">{esc(b["title"])}</div></a>' for b in rel) + "</div></section>")


def sidebar_html(a, pool, apps, patches, cal):
    boxes = []
    app = apps.get(f"id:{a.get('appid')}") if a.get("appid") else None
    if app and app.get("info"):
        i = app["info"]
        ko = (f'<span class="kb">자막</span>' if i["ko"] else '<span class="kb no">한국어 미지원</span>') + \
             ('<span class="kb">음성</span>' if i["ko_voice"] else "")
        price = (f'<span class="disc">-{i["discount"]}%</span>' if i.get("discount") else "") + esc(i.get("price") or "")
        rows = [("출시", esc(i["date"]) + (" (예정)" if i.get("soon") else "")), ("장르", esc(", ".join(i["genres"]))),
                ("개발", esc(i["dev"])), ("배급", esc(i["pub"]) if i["pub"] != i["dev"] else ""), ("한국어", ko),
                ("가격", price), ("메타", str(i["meta"]) if i.get("meta") else "")]
        boxes.append(f'<div class="sbx gcard"><img src="{esc(app["img"])}" alt="" loading="lazy"><div class="n">{esc(app["name"])}</div>'
                     f'<dl>{"".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows if v)}</dl>'
                     f'<a class="gbtn" href="https://store.steampowered.com/app/{app["appid"]}" target="_blank" rel="noopener">Steam에서 보기</a></div>')
    gp = [x for x in patches if a.get("appid") and x["appid"] == a.get("appid")][:3]
    if gp:
        boxes.append('<div class="sbx"><h3>이 게임 최근 패치</h3><ul>' + "".join(
            f'<li><a href="../patches.html">{esc(x.get("title"))}</a><div class="m">{fmt_time(x["ts"])}</div></li>' for x in gp) + "</ul></div>")
    today = datetime.now().strftime("%Y-%m-%d")
    up = sorted([r for r in cal if r["date"] >= today], key=lambda r: (r["date"], not r.get("img")))
    seen, up5 = set(), []
    for r in up:  # 하루에 몰리지 않게 날짜별 1개씩
        if r["date"] not in seen:
            seen.add(r["date"]); up5.append(r)
        if len(up5) == 5:
            break
    if up5:
        boxes.append('<div class="sbx"><h3>다가오는 출시</h3><ul>' + "".join(
            f'<li><a href="../calendar.html">{esc(r["title"])}</a><div class="m">{int(r["date"][5:7])}.{int(r["date"][8:])} · '
            f'{esc(" · ".join(r["platforms"][:3]))}</div></li>' for r in up5) + "</ul></div>")
    latest = [b for b in pool if b["id"] != a["id"]][:5]
    if latest:
        boxes.append('<div class="sbx"><h3>최신 기사</h3><ul>' + "".join(
            f'<li><a href="{b["id"]}.html">{esc(b["title"])}</a><div class="m">{fmt_time(b["ts"])}</div></li>' for b in latest)
            + "</ul></div>")
    if not boxes:
        return ""
    return f'<aside class="side">{"".join(boxes)}</aside>'


def build():
    every = sorted([a for a in load_json(DATA / "articles.json", []) if clean_article(a)], key=lambda a: -a.get("ts", 0))
    for a in every:
        for k_, v_ in (("trust", "보도"), ("category", "기타"), ("tldr", []), ("body", []), ("why", ""),
                       ("platforms", []), ("games", []), ("sources", []), ("title", "(제목 없음)")):
            if not a.get(k_):
                a[k_] = v_
    articles = [a for a in every if a.get("status", "published") == "published"]
    pending = [a for a in every if a.get("status") == "pending"]
    (SITE / "a").mkdir(parents=True, exist_ok=True)
    apps = load_json(DATA / "steam_apps.json", {})
    patches = [x for x in load_json(DATA / "patches.json", []) if not x.get("skip")]
    cal = load_json(DATA / "calendar.json", {"releases": []})["releases"]
    for a in articles + pending:
        try:
            tldr = "".join(f"<li>{esc(t)}</li>" for t in a.get("tldr", []))
            body = "".join(f"<p>{esc(p)}</p>" for p in a.get("body", []))
            srcs = "".join(f'<li>{"[공식] " if s["official"] else ""}{esc(s["name"])} — '
                           f'<a href="{esc(s["link"])}" target="_blank" rel="noopener">{esc(s["title"])}</a></li>'
                           for s in a["sources"])
            note = "※ 아직 번역·재작성되지 않은 원문 요약입니다." if a.get("untranslated") else \
                f"이 기사는 AI가 출처 {len(a['sources'])}건을 종합해 작성했습니다."              + (" 편집자가 검토·수정했습니다." if a.get("edited") else " 사실관계는 출처를 확인하세요.")
            desc = " ".join(a.get("tldr", [])) or a.get("why", "") or a["title"]
            img = a.get("img_hd") or a.get("img")
            if img and not img.startswith("http"):  # 검수 페이지에서 업로드한 이미지 (media/...)
                img = site_url() + img
            ld = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": a["title"][:110],
                  "description": desc[:300], "datePublished": datetime.fromtimestamp(a.get("written", a["ts"]), timezone.utc).isoformat(),
                  "dateModified": datetime.fromtimestamp(a.get("written", a["ts"]), timezone.utc).isoformat(),
                  "mainEntityOfPage": site_url() + f"a/{a['id']}.html", "inLanguage": "ko",
                  "author": {"@type": "Organization", "name": SITE_NAME}, "publisher": {"@type": "Organization", "name": SITE_NAME},
                  **({"image": [img]} if img else {})}
            html_ = page(a["title"] + " — " + SITE_NAME, f"""<div class="apage"><article>{meta(a)}<h1>{esc(a["title"])}</h1>
    {f'<figure class="hero"><img src="{esc(rel_img(a.get("img_hd") or a["img"]))}" alt=""><figcaption>이미지: {esc(a.get("img_src") or "Steam 스토어")}</figcaption></figure>' if a.get("img") else cover_html(a)}
    {f'<div class="tldr"><ul>{tldr}</ul></div>' if tldr else ''}
    {video_html(a)}
    {f'<p class="why">{esc(a["why"])}</p>' if a.get("why") else ''}
    {body}{related_html(a, articles)}{ad("ADSENSE_SLOT_ARTICLE")}<div class="src"><b>출처</b><ul>{srcs}</ul></div><p class="note">{note}</p></article>{sidebar_html(a, articles, apps, patches, cal)}</div>""", desc=desc[:160], path=f"a/{a['id']}.html", img=img, ld=ld)
            (SITE / "a" / f"{a['id']}.html").write_text(html_.replace("{root}", "../"), encoding="utf-8")
        except Exception as e:  # 기사 한 건이 잘못돼도 사이트 빌드는 계속
            print(f"  ! 기사 페이지 실패 {a.get('id')}: {e}")

    recent = [a for a in articles if time.time() - a["ts"] < 86400] or articles
    top = sorted(recent, key=lambda a: (-len(a["sources"]), a["trust"] != "공식", -a["ts"]))[:5]
    lead = None  # 뉴스 목록에는 이미지 없이, 이미지는 기사 안에서만
    lead_html = (f'<a class="lead" href="a/{lead["id"]}.html"><img src="{esc(lead["img"])}" alt=""><div class="lc">{meta(lead)}'
                 f'<div class="t"><span>{esc(lead["title"])}</span></div><div class="w">{esc(lead.get("why"))}</div></div></a>'
                 ) if lead else ""
    top = [a for a in top if a is not lead]
    ticker = "".join(f'<a href="a/{a["id"]}.html"><b>{fmt_time(a["ts"])[6:]}</b>{esc(a["title"])}</a>' for a in articles[:12])
    brief = "".join(f'<a href="a/{a["id"]}.html"><div>{meta(a)}'
                    f'<div class="t">{esc(a["title"])}</div><div class="w">{esc(a.get("why"))}</div></div>'
                    + "</a>"
                    for a in top)
    stream = "".join(f'<a href="a/{a["id"]}.html" data-c="{esc(a.get("category"))}" '
                     f'data-p="{esc(" ".join(a.get("platforms", [])))}"><span class="tm">{fmt_time(a["ts"])[6:]}</span>'
                     f'<div>{meta(a)}<div class="t">{"<span class=new>NEW</span>" if time.time() - a["ts"] < 3 * 3600 else ""}{esc(a["title"])}</div></div>'
                     + '</a>' for a in articles[:100])
    filters = "".join(f'<button data-f="{f}">{f}</button>' for f in ["PC", "PS5", "Xbox", "Switch"] + CATEGORIES[:-1])
    index = page("알트탭 ALT+TAB — 업무 중 잠깐, 게임 소식 3줄", path="", body=f"""
<div class="tick"><span class="live">LIVE</span><div class="tk"><div class="tkin">{ticker}{ticker}</div></div></div>
<h2 class="sec">오늘의 브리핑</h2>{lead_html}<div class="brief">{brief or '<p>아직 기사가 없습니다.</p>'}</div>
{ad("ADSENSE_SLOT_HOME")}<h2 class="sec">최신 소식</h2><nav><button class="on" data-f="">전체</button>{filters}</nav>
<div class="stream" id="st" style="margin-top:12px">{stream}</div>
<div class="more"><button id="more">더 보기</button><a href="search.html">지난 기사 검색 →</a></div>
<script>(()=>{{const PAGE=15;let f='',n=PAGE;const items=[...document.querySelectorAll('#st a')],btn=document.getElementById('more');
const ok=a=>!f||a.dataset.c===f||a.dataset.p.split(' ').includes(f);
function show(){{let k=0;items.forEach(a=>{{const v=ok(a)&&k<n;if(ok(a))k++;a.style.display=v?'':'none';if(v)a.classList.add('in')}});btn.hidden=k<=n}}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));f=b.dataset.f;n=PAGE;show()}});
btn.onclick=()=>{{n+=PAGE;show()}};show()}})()</script>""")
    (SITE / "index.html").write_text(index.replace("{root}", ""), encoding="utf-8")
    repo = os.environ.get("GITHUB_REPOSITORY", "khb950404-creator/gamewebzine")
    (SITE / "review.html").write_text(
        page("검수 — " + SITE_NAME, REVIEW_BODY.replace("__REPO__", repo).replace("__CATS__", json.dumps(CATEGORIES[:-1] + ["기타"], ensure_ascii=False))).replace("{root}", "")
        .replace("<head>", '<head><meta name="robots" content="noindex">'), encoding="utf-8")
    static_pages()
    if (ROOT / "media").exists():  # 검수 페이지에서 업로드한 이미지
        import shutil
        shutil.copytree(ROOT / "media", SITE / "media", dirs_exist_ok=True)
    for step in (lambda: build_search(articles), lambda: build_feeds(articles), build_patches, build_calendar, build_deals):
        try:
            step()
        except Exception as e:  # 한 페이지가 실패해도 나머지는 배포
            print(f"  ! 빌드 단계 실패: {e}")
    print(f"사이트 빌드 완료: {SITE / 'index.html'} (기사 {len(articles)}건)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in ("review", "approve", "reject"):
        review(cmd, sys.argv[2:])
    elif cmd == "deals":  # 할인 갱신 + 이미지 없는 기사 재검색 (LLM 미사용)
        cache = load_json(DATA / "steam_apps.json", {})
        collect_deals(cache)
        collect_all_deals()
        arts = load_json(DATA / "articles.json", [])
        for a in arts:
            if not a.get("img"):
                a.pop("img_v", None)
        for k in [k for k in cache if k.startswith("ig:") and cache[k] is None]:
            del cache[k]
        state = load_json(DATA / "state.json", {})
        try:
            igdb("fields name; limit 1;")
            state["igdb"] = "ok" if os.environ.get("IGDB_CLIENT_ID") else "키 없음"
        except Exception as e:
            state["igdb"] = f"{e} {getattr(e, 'read', lambda: b'')()[:200]}"
        attach_images(arts, cache)
        cal = load_json(DATA / "calendar.json", {"releases": []})
        calendar_images(cal["releases"], cache)
        save_json(DATA / "calendar.json", cal)
        state["cal_img"] = f"{sum(1 for r in cal['releases'] if r.get('img'))}/{len(cal['releases'])}"
        save_json(DATA / "steam_apps.json", cache)
        state["img_count"] = f"{sum(1 for a in arts if a.get('img'))}/{len(arts)}"
        save_json(DATA / "state.json", state)
        save_json(DATA / "articles.json", arts)
        save_json(DATA / "steam_apps.json", cache)
    elif cmd != "build":
        run()
    build()
