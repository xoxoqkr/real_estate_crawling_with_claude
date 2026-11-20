"""매칭 결과 중 정비구역 관련 물건을 스피드옥션 '관심경매물건'의 지정 폴더에 메모와 함께 저장합니다.

- 저장 요청: 물건 팝업(wish/edit)의 '저장' 버튼과 같은 POST /ajax?post&ajax.id=search@add_interest_v2.ajax
  (gubun=폴더코드, reviewStar=중요도, alrm=알림, memo=메모, data[]=법원코드|연도|사건|물번)
- 이미 관심물건에 있는 물건은 건너뜁니다. 저장하면 기존 폴더·메모를 덮어쓸 수 있기 때문입니다.
- 폴더코드 (2026-11-19 확인): 100 박태준, 5 재개발, 6 재건축, 98 기타

사용법
  python sa_interest.py                    # output 폴더의 가장 최근 결과 엑셀로 저장
  python sa_interest.py --file 결과.xlsx    # 특정 결과 엑셀로 저장
  python sa_interest.py --dry-run          # 저장하지 않고 저장할 목록과 메모만 출력
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from match_auctions import load_config
from speedauction_excel import COURT_CODES

MYPAGE_URL = "/member/sub/!/mypage/01?page={page}&list_view_cnt=100"
SAVE_AJAX = "/ajax?post&ajax.id=search@add_interest_v2.ajax"

JS_LIST_INTERESTS = """
async (url) => {
  const t = await fetch(url, {credentials: 'include'}).then(r => r.text());
  const d = new DOMParser().parseFromString(t, 'text/html');
  const total = parseInt(((d.body.innerText.match(/총\\s*([\\d,]+)\\s*건/) || [])[1] || '0').replace(/,/g, ''));
  const items = [...d.querySelectorAll('.list_wish .con')].map(b => {
    const s = b.textContent.replace(/\\s+/g, ' ');
    const m = s.match(/\\[(\\S+?)(?:지방법원|지법)?\\]\\s*(\\d{4})\\s*타경\\s*(\\d+)(?:\\s*\\(?물번\\s*\\[?(\\d+)\\]?\\)?|\\s*\\((\\d+)\\))?/);
    if (!m) return null;
    return {court: m[1], year: m[2], no: m[3], obj: m[4] || m[5] || '1',
            folder: (s.match(/관심유형\\s*(.+?)\\s*중요도/) || [])[1] || ''};
  }).filter(Boolean);
  return {total, items};
}
"""

JS_SAVE = """
async ([url, gubun, star, memo, token]) => {
  const body = jQuery.param({gubun: gubun, reviewStar: star, alrm: 1, memo: memo, data: [token]});
  const r = await fetch(url, {method: 'POST', body, credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'}});
  return {status: r.status, text: (await r.text()).replace(/\\s+/g, ' ').slice(0, 200)};
}
"""


def _fmt(v, unit="", digits=0):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "?"
    return f"{v:,.{digits}f}{unit}"


def make_memo(r: pd.Series) -> str:
    """메모 한 덩어리. 판정·구역·가격 지표·확인할 것 순서."""
    zone = r.get("구역명")
    zone = zone.strip() if isinstance(zone, str) and zone.strip() else "구역명 미상"
    rel = r["판정"]
    if rel == "구역안":
        where = f"{zone} 안 (필지 {_fmt(r.get('겹침%'), '%')} 포함)"
    elif rel == "구역걸침":
        where = f"{zone}에 일부 걸침 (필지 {_fmt(r.get('겹침%'), '%')})"
    else:
        where = f"{zone} 경계에서 {_fmt(r.get('경계거리m'), 'm', 1)}"
    src = r.get("구역출처") if isinstance(r.get("구역출처"), str) else ""
    lines = [
        f"[{datetime.now():%y.%m.%d} 자동매칭] {where}. 구역자료 {src}",
        f"최근 고시 {int(r['최근고시연도'])}년" if pd.notna(r.get("최근고시연도")) else "고시 연도 미상",
        f"대지권 {_fmt(r.get('대지권평'), '평', 2)}, 최저가 {_fmt((r.get('최저가') or 0) / 1e8, '억', 2)}"
        f"({_fmt(r.get('최저가율%'), '%')}), 대지 평당 최저가 {_fmt(r.get('대지평당최저가(만원)'), '만원')}",
    ]
    tags = r.get("특수조건")
    if isinstance(tags, str) and tags.strip():
        lines.append(f"특수조건: {tags}")
    lines.append("확인할 것: ① 구역 단계·권리산정기준일(정비사업 정보몽땅, 구청) ② 보존등기일이 기준일보다 앞서는지 "
                 "③ 경매신청 채권자가 금융기관인지(조합원 지위 양도 제한 예외) ④ 같은 구역 낙찰가율 비교")
    memo = "\n".join(lines)
    return memo.replace("=", " ")  # 일부 경매 사이트가 '='를 지우므로 쓰지 않음


def token_of(key: str) -> str:
    court, year, case, obj = key.split("|")
    return f"{COURT_CODES[court]}|{year}|{case}|{obj}"


def latest_result(out_dir: Path) -> Path:
    files = sorted(out_dir.glob("재개발매칭_*.xlsx"))
    if not files:
        raise FileNotFoundError(f"결과 엑셀이 없습니다: {out_dir}")
    return files[-1]


def select_targets(path: Path, judges: list[str]) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="전체")
    mask = df["판정"].apply(lambda x: any(str(x).startswith(j) for j in judges))
    df = df[mask & df["key"].astype(str).str.split("|").str[0].isin(COURT_CODES)]
    # 최근 고시가 너무 오래된 구역에만 걸린 물건은 끝난 사업지일 가능성이 커서 뺌 (고시 연도를 모르면 남김)
    min_year = int(os.getenv("INTEREST_MIN_NOTICE_YEAR", "2005"))
    if min_year and "최근고시연도" in df.columns:
        old = df["최근고시연도"].notna() & (df["최근고시연도"] < min_year)
        if old.any():
            print(f"  최근 고시가 {min_year}년 이전인 구역만 걸린 {int(old.sum())}건은 저장하지 않음")
        df = df[~old]
    return df.reset_index(drop=True)


def existing_keys(page) -> dict[str, str]:
    keys, pg = {}, 1
    while True:
        res = page.evaluate(JS_LIST_INTERESTS, MYPAGE_URL.format(page=pg))
        for it in res["items"]:
            keys[f"{it['court']}|{it['year']}|{it['no']}|{it['obj']}"] = it["folder"]
        if not res["items"] or len(keys) >= res["total"] or pg >= 30:
            break
        pg += 1
        time.sleep(random.uniform(2, 4))
    return keys


def save_interests(cfg: dict, result_path: Path, page, dry_run: bool = False) -> Path:
    folder = int(os.getenv("INTEREST_FOLDER", "100"))
    star = int(os.getenv("INTEREST_STAR", "0"))
    judges = [j.strip() for j in os.getenv("INTEREST_JUDGES", "구역안,구역걸침").split(",") if j.strip()]
    targets = select_targets(result_path, judges)
    print(f"관심물건 저장 대상: {len(targets)}건 (판정 {', '.join(judges)} / 폴더코드 {folder})")
    log = []
    have = existing_keys(page) if page is not None else {}
    for _, r in targets.iterrows():
        key, memo = str(r["key"]), make_memo(r)
        row = {"key": key, "사건번호": r["사건번호"], "물번": r["물번"], "소재지": r["소재지"],
               "판정": r["판정"], "구역명": r.get("구역명"), "메모": memo}
        if key in have:
            row["결과"] = f"건너뜀: 이미 관심물건({have[key]})"
        elif dry_run or page is None:
            row["결과"] = "dry-run"
        else:
            res = page.evaluate(JS_SAVE, [SAVE_AJAX, folder, star, memo, token_of(key)])
            ok = res["status"] == 200 and "저장되었습니다" in res["text"]
            row["결과"] = "저장" if ok else f"실패: {res['status']} {res['text'][:80]}"
            time.sleep(random.uniform(2, 4))
        print(f"  {r['사건번호']}({r['물번']}) {row['결과']}")
        log.append(row)
    out = cfg["output"] / f"관심저장_{datetime.now():%Y%m%d_%H%M}.csv"
    pd.DataFrame(log).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"  저장 기록: {out}")
    return out


def main():
    ap = argparse.ArgumentParser(description="매칭 결과를 스피드옥션 관심물건에 저장")
    ap.add_argument("--file", help="결과 엑셀 경로 (기본: output 폴더의 최신 파일)")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 목록과 메모만 출력")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    path = Path(args.file) if args.file else latest_result(cfg["output"])
    print(f"결과 파일: {path.name}")
    if args.dry_run:
        save_interests(cfg, path, None, dry_run=True)
        return
    from sa_collect import browser_session
    with browser_session(cfg, args.headed) as page:
        save_interests(cfg, path, page)


if __name__ == "__main__":
    sys.exit(main())
