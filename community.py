"""커뮤니티 화제 신호: 에펨코리아 인기글 중 PC/콘솔 게시판 제목만 읽어 '지금 사람들이 떠드는 게임 이슈'를 잡는다.

robots.txt 가 일반 봇에 허용한 /best, /best2 목록만 하루 몇 번 읽는다 (게시판 본문·이미지는 가져오지 않음).
글을 퍼오지 않고, 같은 이슈의 뉴스 후보 점수를 올리는 데만 쓴다 — 기사는 공식·언론 출처로 쓴다.
"""
import html, re, time, urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
GAME_BOARDS = ("PC/콘솔", "모바일게임", "게임")


def fm_hot():
    """[{title, votes, comments}] — PC/콘솔 게시판에서 인기글로 올라온 것."""
    out, seen = [], set()
    for n, path in enumerate(("/best", "/best2")):
        if n:
            time.sleep(3)  # 연속 요청은 430 으로 막힌다
        try:
            req = urllib.request.Request("https://www.fmkorea.com" + path, headers={"User-Agent": UA})
            h = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        except Exception as e:
            print(f"  ! 펨코 인기글 실패 {path}: {e}")
            continue
        for li in h.split('<li class="li ')[1:]:
            cat = re.search(r'class="category">\s*<a [^>]*>([^<]+)', li)
            t = re.search(r'class="ellipsis-target">([^<]+)', li)
            if not cat or not t or not any(b in cat.group(1) for b in GAME_BOARDS):
                continue
            title = html.unescape(t.group(1)).strip()
            if title in seen:
                continue
            seen.add(title)
            v = re.search(r'class="count">(\d+)', li)
            c = re.search(r'class="comment_count">\[(\d+)\]', li)
            out.append({"title": title, "votes": int(v.group(1)) if v else 0, "comments": int(c.group(1)) if c else 0})
    return out


def boost(clusters, hot, tokens, names=None):
    """커뮤니티 인기글과 같은 이슈인 뉴스 묶음에 가산점 (+3, 반응이 크면 +5). 점수 순으로 다시 정렬.
    names: {한글 게임명: 영문 게임명} — 한글 인기글 제목을 영문 뉴스와 맞추는 데 쓴다."""
    def key(t):
        tk = tokens(t)
        for ko, en in (names or {}).items():
            if ko and ko in t:
                tk |= tokens(en)
        return tk
    hts = [(key(h["title"]), h) for h in hot]
    for c in clusters:
        for tk, h in hts:
            same = tk & c["tokens"]
            if len([w for w in same if not w.isdigit()]) >= 2 or any(len(w) >= 5 and not w.isdigit() for w in same):
                c["score"] += 5 if h["votes"] >= 300 else 3
                c["hot"] = h["title"]
                break
    return sorted(clusters, key=lambda c: -c["score"])


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for h in fm_hot():
        print(h)
