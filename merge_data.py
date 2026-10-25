"""git 병합 드라이버: data/*.json 을 '덮어쓰기' 대신 합친다.

로컬과 GitHub Actions 가 동시에 기사를 쓰면 git 이 파일 통째로 한쪽을 골라 다른 쪽 기사가 사라졌다.
  - articles.json (목록): id 기준으로 합침. 같은 기사는 더 최근에 고친 쪽(edited/proofread/written)을 쓴다.
  - ig_posted.json, seen.json 등 (딕셔너리·목록): 합집합.
git 이 호출: python merge_data.py %O %A %B   (%A 에 결과를 쓴다)
"""
import json, sys


def load(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def stamp(a):
    return max(a.get("edited", 0) or 0, a.get("proofread", 0) or 0, a.get("written", 0) or 0)


def main(base, ours, theirs):
    a, b = load(ours), load(theirs)
    if a is None or b is None:
        return 1  # JSON 이 아니면 git 기본 충돌 처리
    if isinstance(a, list) and isinstance(b, list):
        if all(isinstance(x, dict) and "id" in x for x in a + b):
            by = {}
            for x in b + a:  # 같은 시각이면 ours(a)가 이긴다
                k = x["id"]
                if k not in by or stamp(x) >= stamp(by[k]):
                    by[k] = x
            out = sorted(by.values(), key=lambda x: x.get("written", x.get("ts", 0)) or 0)
        else:
            out = list(dict.fromkeys(json.dumps(x, sort_keys=True) for x in a + b))
            out = [json.loads(x) for x in out]
            if all(isinstance(x, str) for x in out):
                out = sorted(set(out))
    elif isinstance(a, dict) and isinstance(b, dict):
        out = {**b, **a}
    else:
        return 1
    with open(ours, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
