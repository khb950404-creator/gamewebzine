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
import json, os, re, sys, html, hashlib, time, urllib.request, urllib.error
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
def llm(prompt):
    mode = os.environ.get("LLM", "gemini" if os.environ.get("GEMINI_API_KEY") else "none")
    if mode == "gemini":
        time.sleep(5)  # 무료 티어 분당 요청 제한 대응
        # 기본 모델이 계속 혼잡하면 lite 모델로 대체
        models = [os.environ.get("GEMINI_MODEL", "gemini-flash-latest"), "gemini-flash-lite-latest"]
        for model in models:
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                data=json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                                 "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}).encode(),
                headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
            for attempt in range(3):  # 과부하(503)·한도(429)는 잠시 후 재시도
                try:
                    r = json.loads(urllib.request.urlopen(req, timeout=120).read())
                    return json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
                except urllib.error.HTTPError as e:
                    if e.code not in (429, 500, 503):
                        raise
                    print(f"    … {model} {e.code}, {15 * (attempt + 1)}초 후 재시도")
                    time.sleep(15 * (attempt + 1))
        raise RuntimeError("모든 Gemini 모델이 응답하지 않음 (혼잡 또는 무료 한도 초과)")
    if mode == "ollama":
        req = urllib.request.Request(
            os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/generate",
            data=json.dumps({"model": os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"), "prompt": prompt,
                             "format": "json", "stream": False}).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(json.loads(urllib.request.urlopen(req, timeout=600).read())["response"])
    return None


PROMPT = """너는 PC·콘솔 게이머를 위한 한국어 게임 웹진 기자다.
아래 출처 자료만 근거로 기사를 써라. 자료에 없는 사실(날짜, 가격, 플랫폼, 수치)은 절대 지어내지 마라.
원문 문장을 번역해 옮기지 말고, 여러 출처를 종합해 자신의 문장으로 새로 써라.

신뢰도 규칙:
- "공식": 공식 출처(official=true)가 직접 발표
- "확인": 공식은 아니지만 2개 이상 매체가 보도
- "루머": 단일 매체 보도, 유출, 소문, 익명 소식통

JSON 하나만 출력:
{{"title": "한국어 헤드라인(40자 이내, 낚시 금지)",
 "tldr": ["핵심 요약 1", "핵심 요약 2", "핵심 요약 3"],
 "why": "PC·콘솔 게이머에게 왜 중요한가 (1~2문장)",
 "body": ["문단1", "문단2", "문단3"],
 "category": "{cats} 중 하나",
 "platforms": ["PC","PS5","Xbox","Switch" 중 해당하는 것],
 "games": ["관련 게임명"],
 "trust": "공식|확인|루머",
 "skip": false}}
PC·콘솔 비디오게임과 무관하거나(보드·카드게임, 게임과 연결 약한 영화·드라마 소식 등) 광고/딜 나열/퀴즈/팟캐스트 같은 저가치 글이면 {{"skip": true}} 만 출력.

출처 자료:
{sources}"""


VERIFY = """너는 게임 웹진의 팩트체커다. 아래 기사를 출처 자료와 한 문장씩 대조하라.
출처에 근거가 없는 주장(추측, 배경 설명으로 지어낸 사실, 틀린 날짜·수치·플랫폼)은 삭제하거나 출처에 맞게 고쳐라.
문체와 구조는 유지하고, 문제없는 문장은 건드리지 마라. trust 값도 규칙에 맞는지 확인하라
(공식=official 출처가 직접 발표, 확인=2개 이상 매체, 루머=단일 매체·유출·소문).

JSON 하나만 출력: {{"problems": ["발견한 문제 설명"], "article": {{수정된 기사 전체, 입력과 같은 필드}}}}

출처 자료:
{sources}

기사:
{article}"""


class LLMDown(Exception):
    pass


def write_article(c):
    srcs = c["items"][:6]
    sources = "\n\n".join(
        f"[{i+1}] {s['source']} (official={str(s['official']).lower()})\n제목: {s['title']}\n요약: {s['summary']}"
        for i, s in enumerate(srcs))
    try:
        a = llm(PROMPT.format(cats=", ".join(CATEGORIES), sources=sources))
    except Exception as e:
        print(f"  ! LLM 오류: {e} {getattr(e, 'read', lambda: b'')()[:200]}")
        raise LLMDown()
    if a is None:  # LLM 없음 모드
        s = srcs[0]
        a = {"title": s["title"], "tldr": [], "why": "", "body": [s["summary"]], "category": "기타",
             "platforms": [], "games": [], "trust": "공식" if s["official"] else ("확인" if len(srcs) > 1 else "루머"),
             "untranslated": True}
    if a.get("skip"):
        return None
    if not a.get("untranslated"):
        try:
            v = llm(VERIFY.format(sources=sources, article=json.dumps(a, ensure_ascii=False)))
            if v and isinstance(v.get("article"), dict) and v["article"].get("title"):
                a = {**a, **v["article"]}
                a["fixes"] = v.get("problems", [])
                if a["fixes"]:
                    print(f"    팩트체크 수정 {len(a['fixes'])}건")
        except Exception as e:
            print(f"    ! 팩트체크 실패, 검수 대기로 보냄: {e}")
            a["fixes"] = ["팩트체크 실패"]; a["force_review"] = True
    review_all = os.environ.get("REVIEW_ALL") == "1"
    a["status"] = "pending" if review_all or a.get("force_review") or a.get("trust") == "루머" else "published"
    a["id"] = hashlib.md5("".join(sorted(s["id"] for s in srcs)).encode()).hexdigest()[:10]
    a["sources"] = [{"name": s["source"], "title": s["title"], "link": s["link"], "official": s["official"]} for s in srcs]
    a["ts"] = max(s["ts"] for s in srcs)
    a["written"] = time.time()
    return a


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
    clusters = cluster(fresh)
    written, failed = 0, False
    for c in clusters:
        if written >= MAX_ARTICLES_PER_RUN:
            break
        print(f"작성: {c['items'][0]['title'][:70]} (출처 {len(c['items'])}, 점수 {c['score']:.1f})")
        try:
            a = write_article(c)
        except LLMDown:
            print("LLM 호출 실패로 중단 - 작성된 기사까지 저장 (남은 항목은 다음 실행 때 재시도)")
            failed = True
            break
        for i in c["items"]:
            seen.add(i["id"])  # skip 판정도 다시 보지 않음
        if a:
            articles.append(a); written += 1
    save_json(DATA / "seen.json", sorted(seen))
    save_json(DATA / "articles.json", articles)
    print(f"기사 {written}건 작성")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")  # Actions 실행 화면에 결과 표시(로그인 없이 보임)
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### 새 기사 {written}건{' (LLM 오류로 중단)' if failed else ''}\n")
            for a in articles[len(articles) - written:]:
                f.write(f"- [{a.get('status')}] ({a['trust']}) {a['title']}\n")
    if failed:
        sys.exit(1)


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
        print(f"검수 대기 {len(pend)}건  (미리보기: site/_review.html)")
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
.logo{font-weight:900;font-size:20px;letter-spacing:-.5px}.logo b{color:var(--acc)}
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
footer{color:var(--mut);font-size:12px;padding:40px 0;text-align:center}
@media(max-width:640px){article h1{font-size:24px}.brief .t{font-size:17px}}
"""


def esc(s):
    return html.escape(str(s or ""))


def page(title, body):
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
<style>{CSS}</style>{ads_head()}</head><body>
<header><div class="wrap"><a class="logo" href="{{root}}index.html">PRESS<b>START</b></a></div></header>
<div class="wrap">{body}</div><footer><div><a href="{{root}}about.html">소개</a><a href="{{root}}privacy.html">개인정보처리방침</a><a href="{{root}}contact.html">문의</a></div>
<p>AI가 공개 출처를 종합해 작성하고 팩트체크를 거친 기사입니다 · 원문은 각 기사 하단 출처 참고</p></footer></body></html>"""


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


SITE_NAME = "PRESS START"


def static_pages():
    email = os.environ.get("CONTACT_EMAIL") or "contact@example.com"
    today = datetime.now().strftime("%Y년 %m월 %d일")
    pages = {
        "about": ("소개", f"""<p>{SITE_NAME}는 PC·콘솔 게이머를 위한 게임 뉴스 브리핑입니다.</p>
<p>공식 발표와 국내외 게임 매체의 보도를 수집해, 같은 소식을 하나의 이슈로 묶고 핵심 요약과
"왜 중요한가"를 붙여 전달합니다. 기사는 AI가 여러 출처를 종합해 작성하며, 별도의 팩트체크 단계에서
출처와 대조합니다. 루머성 소식은 편집자 검수를 거친 뒤에만 게시됩니다.</p>
<h2>신뢰도 표시</h2><ul><li><b>공식</b> — 플랫폼·개발사·퍼블리셔가 직접 발표</li>
<li><b>확인</b> — 2개 이상의 매체가 보도</li><li><b>루머</b> — 단일 매체 보도, 유출, 소문</li></ul>
<p>모든 기사 하단에 원문 출처 링크를 제공합니다. 오류 제보는 <a href="contact.html">문의</a>로 보내주세요.</p>"""),
        "privacy": ("개인정보처리방침", f"""<p>시행일: {today}</p>
<p>{SITE_NAME}(이하 "사이트")는 회원가입 없이 이용할 수 있으며, 이용자의 이름·연락처 등 개인정보를 직접 수집하지 않습니다.</p>
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
        html_ = page(a["title"], f"""<article>{meta(a)}<h1>{esc(a["title"])}</h1>
{f'<div class="tldr"><ul>{tldr}</ul></div>' if tldr else ''}
{f'<p class="why">왜 중요한가 — {esc(a["why"])}</p>' if a.get("why") else ''}
{body}{ad("ADSENSE_SLOT_ARTICLE")}<div class="src"><b>출처</b><ul>{srcs}</ul></div><p class="note">{note}</p></article>""")
        (SITE / "a" / f"{a['id']}.html").write_text(html_.replace("{root}", "../"), encoding="utf-8")

    recent = [a for a in articles if time.time() - a["ts"] < 86400] or articles
    top = sorted(recent, key=lambda a: (-len(a["sources"]), a["trust"] != "공식", -a["ts"]))[:5]
    brief = "".join(f'<a href="a/{a["id"]}.html" data-c="{esc(a.get("category"))}">{meta(a)}'
                    f'<div class="t">{esc(a["title"])}</div><div class="w">{esc(a.get("why"))}</div></a>' for a in top)
    stream = "".join(f'<a href="a/{a["id"]}.html" data-c="{esc(a.get("category"))}" '
                     f'data-p="{esc(" ".join(a.get("platforms", [])))}"><span class="tm">{fmt_time(a["ts"])[6:]}</span>'
                     f'<div>{meta(a)}<div class="t">{esc(a["title"])}</div></div></a>' for a in articles[:100])
    filters = "".join(f'<button data-f="{f}">{f}</button>' for f in ["PC", "PS5", "Xbox", "Switch"] + CATEGORIES[:-1])
    index = page("PRESS START — 게임 브리핑", f"""
<h2 class="sec">오늘의 브리핑</h2><div class="brief">{brief or '<p>아직 기사가 없습니다.</p>'}</div>
{ad("ADSENSE_SLOT_HOME")}<h2 class="sec">최신 소식</h2><nav><button class="on" data-f="">전체</button>{filters}</nav>
<div class="stream" id="st" style="margin-top:12px">{stream}</div>
<script>document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{{
document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));const f=b.dataset.f;
document.querySelectorAll('#st a').forEach(a=>a.style.display=!f||a.dataset.c===f||a.dataset.p.split(' ').includes(f)?'':'none')}})</script>""")
    (SITE / "index.html").write_text(index.replace("{root}", ""), encoding="utf-8")
    rv = "".join(f'<a href="a/{a["id"]}.html">{meta(a)}<div class="t">[{a["id"]}] {esc(a["title"])}</div>'
                 f'<div class="w">{"<br>".join("! " + esc(f) for f in a.get("fixes", []))}</div></a>' for a in pending)
    (SITE / "_review.html").write_text(page("검수 대기", f'<h2 class="sec">검수 대기 {len(pending)}건 — '
        f'python webzine.py approve ID / reject ID</h2><div class="brief">{rv}</div>').replace("{root}", ""),
        encoding="utf-8")
    static_pages()
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
