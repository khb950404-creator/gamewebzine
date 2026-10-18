"""인스타 인사이트 수집·분석.

python insights.py collect  # 게시물별 지표 수집 → data/insights.json, 학습값 → data/ig_learn.json
python insights.py page     # 리포트 페이지 → site/insights.html (검색 노출 안 함)
"""
import html
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

import ig_post as g

DATA, SITE = g.DATA, g.SITE
OUT = DATA / "insights.json"
LEARN = DATA / "ig_learn.json"
KIND_KO = {"news": "뉴스 카드", "reel": "릴스", "series": "주간 TOP5", "deal": "할인 모음"}


def metrics_for(mid, product):
    """게시물 종류마다 지원 지표가 달라서, 안 되면 기본 지표로 다시 시도."""
    sets = (["reach", "views", "likes", "comments", "saved", "shares", "total_interactions",
             "ig_reels_avg_watch_time"] if product == "REELS" else
            ["reach", "views", "likes", "comments", "saved", "shares", "total_interactions", "follows",
             "profile_visits"],
            ["reach", "likes", "comments", "saved", "shares"])
    for ms in sets:
        try:
            r = g.api("GET", f"{mid}/insights", metric=",".join(ms))
            return {d["name"]: (d.get("values") or [{}])[0].get("value", 0) for d in r.get("data", [])}
        except Exception as e:
            err = str(e)
    raise RuntimeError(err[:300])


def collect():
    uid = os.environ[g.UID_ENV]
    me = g.api("GET", "me", fields="followers_count,media_count,username")
    posted = g.load(g.POSTED, {})
    by_media = {p.get("media"): (i, p) for i, p in posted.items() if p.get("media")}
    arts = {a["id"]: a for a in g.load(DATA / "articles.json", [])}
    old = g.load(OUT, {"media": {}})
    media, after = [], None
    for _ in range(5):  # 최근 최대 250건
        kw = {"fields": "id,caption,media_product_type,media_type,timestamp,like_count,comments_count,permalink",
              "limit": 50}
        if after:
            kw["after"] = after
        r = g.api("GET", f"{uid}/media", **kw)
        media += r.get("data", [])
        after = r.get("paging", {}).get("cursors", {}).get("after")
        if not r.get("paging", {}).get("next"):
            break
    out, fails = {}, 0
    for m in media:
        ts = datetime.strptime(m["timestamp"][:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=9)  # KST
        prev = old["media"].get(m["id"], {})
        # 3일 지난 게시물은 지표가 거의 안 변하니 하루 한 번만 갱신
        if prev.get("m") and (datetime.now() - ts).days > 3 and time.time() - prev.get("at", 0) < 86400:
            out[m["id"]] = prev
            continue
        try:
            mt = metrics_for(m["id"], m.get("media_product_type"))
        except Exception as e:
            fails += 1
            if fails == 1:
                print(f"  ! 인사이트 실패 ({m['id']}): {e}")
            mt = prev.get("m") or {"likes": m.get("like_count", 0), "comments": m.get("comments_count", 0)}
        item, p = by_media.get(m["id"], (None, {}))
        a = arts.get(item) or {}
        out[m["id"]] = {
            "at": time.time(), "m": mt, "ts": ts.strftime("%Y-%m-%d %H:%M"), "hour": ts.hour, "wd": ts.weekday(),
            "product": m.get("media_product_type"), "link": m.get("permalink"),
            "kind": p.get("kind") or ("reel" if m.get("media_product_type") == "REELS" else "news"),
            "item": item, "title": a.get("title") or (m.get("caption") or "").split("\n")[0][:60],
            "category": a.get("category"), "games": a.get("games_en") or [],
        }
    hist = old.get("followers", [])
    today = datetime.now().strftime("%Y-%m-%d")
    hist = [h for h in hist if h["d"] != today] + [{"d": today, "n": me.get("followers_count", 0)}]
    g.save(OUT, {"updated": time.time(), "account": me, "followers": hist[-120:], "media": out})
    learn()
    print(f"인사이트 {len(out)}건 (실패 {fails}), 팔로워 {me.get('followers_count')}")


def score(m):
    """게시물 성과 점수: 도달 대비 반응 + 저장·공유·팔로우 가중."""
    x = m["m"]
    eng = x.get("likes", 0) + 2 * x.get("comments", 0) + 3 * x.get("saved", 0) + 3 * x.get("shares", 0) \
        + 10 * x.get("follows", 0)
    return eng + 0.02 * (x.get("reach") or x.get("views") or 0)


def group(items, key):
    d = defaultdict(list)
    for it in items:
        k = key(it)
        if k is not None:
            d[k].append(score(it))
    return {k: (sum(v) / len(v), len(v)) for k, v in d.items()}


def learn():
    """성과가 좋았던 카테고리·게임·시간대를 학습값으로 저장 → ig_post 가 선택·게시 시간에 반영."""
    items = [m for m in g.load(OUT, {"media": {}})["media"].values()
             if datetime.now() - datetime.strptime(m["ts"], "%Y-%m-%d %H:%M") > timedelta(hours=24)]
    if len(items) < 8:  # 표본이 너무 적으면 학습 안 함
        g.save(LEARN, {"n": len(items)})
        return
    base = sum(score(m) for m in items) / len(items) or 1
    def rel(gr, min_n):  # 평균 대비 배율 (-1~+1 범위로 눌러서)
        return {k: round(max(-1, min(1, (v / base) - 1)), 2) for k, (v, n) in gr.items() if n >= min_n}
    news = [m for m in items if m["kind"] == "news"]
    g.save(LEARN, {
        "n": len(items), "updated": time.time(),
        "category": rel(group(news, lambda m: m.get("category")), 2),
        "game": rel(group(items, lambda m: (m["games"] or [None])[0] and m["games"][0].lower()), 2),
        "hour": rel(group(items, lambda m: m["hour"]), 2),
        "kind": rel(group(items, lambda m: m["kind"]), 2),
    })


def page():
    d = g.load(OUT, None)
    SITE.mkdir(exist_ok=True)
    if not d:
        (SITE / "insights.html").write_text("<meta charset=utf-8><p>아직 인사이트 데이터가 없습니다.", encoding="utf-8")
        return
    items = list(d["media"].values())
    week = [m for m in items if datetime.now() - datetime.strptime(m["ts"], "%Y-%m-%d %H:%M") < timedelta(days=7)]
    e = html.escape
    def tot(ms, k):
        return sum(m["m"].get(k, 0) or 0 for m in ms)
    fol = d.get("followers", [])
    f_now = fol[-1]["n"] if fol else 0
    f_week = next((h["n"] for h in fol if h["d"] >= (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")), f_now)
    cards = [("팔로워", f"{f_now:,}", f"이번 주 {f_now - f_week:+,}"),
             ("이번 주 게시", f"{len(week)}", ""),
             ("이번 주 도달", f"{tot(week, 'reach'):,}", ""),
             ("좋아요·댓글", f"{tot(week, 'likes') + tot(week, 'comments'):,}", ""),
             ("저장·공유", f"{tot(week, 'saved') + tot(week, 'shares'):,}", ""),
             ("게시물로 팔로우", f"{tot(week, 'follows'):,}", "")]
    def table(title, gr, label=str):
        rows = sorted(gr.items(), key=lambda x: -x[1][0])
        if not rows:
            return ""
        mx = max(v for v, _ in gr.values()) or 1
        tr = "".join(f"<tr><td>{e(label(k))}</td><td class=bar><i style='width:{v / mx * 100:.0f}%'></i>"
                     f"<b>{v:.0f}</b></td><td class=n>{n}건</td></tr>" for k, (v, n) in rows)
        return f"<section><h2>{title}</h2><table>{tr}</table></section>"
    top = sorted(items, key=score, reverse=True)
    def row(m):
        x = m["m"]
        return (f"<tr><td><a href='{e(m.get('link') or '#')}' target=_blank>{e(m['title'] or '(제목 없음)')}</a>"
                f"<small>{KIND_KO.get(m['kind'], m['kind'])} · {m['ts']}</small></td>"
                f"<td class=n>{x.get('reach', 0):,}</td><td class=n>{x.get('likes', 0):,}</td>"
                f"<td class=n>{x.get('saved', 0):,}</td><td class=n>{x.get('shares', 0):,}</td>"
                f"<td class=n>{x.get('follows', 0) or 0:,}</td></tr>")
    head = "<tr><th>게시물</th><th>도달</th><th>좋아요</th><th>저장</th><th>공유</th><th>팔로우</th></tr>"
    lr = g.load(LEARN, {})
    tips = []
    for k, name in (("kind", "형식"), ("category", "카테고리"), ("hour", "시간대")):
        v = lr.get(k) or {}
        if v:
            best = max(v, key=v.get); worst = min(v, key=v.get)
            lab = (lambda z: f"{z}시") if k == "hour" else (lambda z: KIND_KO.get(z, z))
            tips.append(f"{name}: <b>{e(lab(best))}</b> 반응이 가장 좋고, {e(lab(worst))} 가 가장 약함")
    learn_html = ("<section><h2>자동 반영 중</h2><ul>" + "".join(f"<li>{t}</li>" for t in tips) +
                  "</ul><p class=muted>반응 좋은 카테고리·게임은 게시 우선순위가 올라가고, 반응 좋은 시간대에 더 많이 올립니다.</p></section>"
                  ) if tips else f"<section><p class=muted>자동 반영은 게시물 8건 이상 쌓이면 시작합니다 (현재 {lr.get('n', 0)}건).</p></section>"
    body = f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=robots content=noindex>
<title>인스타 인사이트</title><style>
:root{{--bg:#fff;--fg:#16161a;--mut:#6b6b76;--line:#e7e7ec;--acc:#e5484d;--card:#f7f7f9}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111114;--fg:#ececf1;--mut:#9a9aa6;--line:#2a2a31;--card:#1a1a1f}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,'Noto Sans KR',sans-serif;margin:0}}
main{{max-width:980px;margin:0 auto;padding:24px 16px 60px}}h1{{font-size:22px;margin:0 0 4px}}
h2{{font-size:16px;margin:28px 0 10px}}.muted{{color:var(--mut);font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-top:16px}}
.cards div{{background:var(--card);border-radius:10px;padding:12px}}.cards b{{display:block;font-size:22px}}
.cards span,.cards small{{color:var(--mut);font-size:12px}}
table{{width:100%;border-collapse:collapse}}td,th{{padding:7px 6px;border-bottom:1px solid var(--line);text-align:left;font-size:14px}}
th{{color:var(--mut);font-weight:500;font-size:12px}}.n{{text-align:right;white-space:nowrap}}
td small{{display:block;color:var(--mut);font-size:12px}}a{{color:inherit}}
.bar{{position:relative;width:60%}}.bar i{{position:absolute;left:0;top:8px;bottom:8px;background:var(--acc);opacity:.25;border-radius:3px}}
.bar b{{position:relative;padding-left:6px;font-weight:600}}.wrap{{overflow-x:auto}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:0 24px}}@media(max-width:700px){{.grid2{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>인스타 인사이트</h1><p class=muted>@{e(d['account'].get('username', ''))} · 갱신 {datetime.fromtimestamp(d['updated']).strftime('%m-%d %H:%M')} · 점수 = 좋아요+댓글×2+저장×3+공유×3+팔로우×10+도달×0.02</p>
<div class=cards>{''.join(f'<div><span>{a}</span><b>{b}</b><small>{c}</small></div>' for a, b, c in cards)}</div>
{learn_html}
<div class=grid2>{table('형식별 평균 점수', group(items, lambda m: m['kind']), lambda k: KIND_KO.get(k, k))}
{table('카테고리별 평균 점수', group([m for m in items if m['kind'] == 'news'], lambda m: m.get('category')))}
{table('게시 시간대별 평균 점수', group(items, lambda m: m['hour']), lambda k: f'{k}시')}
{table('요일별 평균 점수', group(items, lambda m: m['wd']), lambda k: '월화수목금토일'[k])}</div>
<section><h2>반응 좋은 게시물 TOP 10</h2><div class=wrap><table>{head}{''.join(row(m) for m in top[:10])}</table></div></section>
<section><h2>반응 약한 게시물 (최근 2주)</h2><div class=wrap><table>{head}{''.join(row(m) for m in sorted([m for m in items if datetime.now() - datetime.strptime(m['ts'], '%Y-%m-%d %H:%M') < timedelta(days=14)], key=score)[:5])}</table></div></section>
</main></body></html>"""
    (SITE / "insights.html").write_text(body, encoding="utf-8")
    print("인사이트 페이지 생성")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    {"collect": collect, "page": page}[sys.argv[1]]()
