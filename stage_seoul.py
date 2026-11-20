"""서울시 '정비사업 추진현황' 엑셀(서울 열린데이터광장)을 zone_stage.csv로 가져온다.

  python stage_seoul.py                      # data 폴더의 '서울시 정비사업 추진현황*.xlsx'를 가져옴
  python stage_seoul.py --file 경로.xlsx --asof 2026-06-30
  python stage_seoul.py --districts 관악구,광진구   # 이 구만 (기본: .env.normal의 DISTRICTS, 비우면 전체)

자료는 분기마다 나오고 법적 효력이 없는 참고자료다. 가져온 행의 확인일은 자료 기준일이라, 더 최근 확인일이 있는
행(구청 고시, 수동 입력)이 같은 구역에 있으면 그쪽이 쓰인다.
"""
from __future__ import annotations

import argparse
import csv
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from stage import COLS

SOURCE = "서울시 정비사업 추진현황"
HEAD_ROWS = 4  # 머리글 4줄 뒤부터 데이터
COLNAMES = ["CODE", "순번", "자치구", "구역명", "위치1", "위치2", "공공민간", "일반재촉", "사업유형", "단계", "멸실",
            "지정최초", "지정변경", "추진위", "조합설립", "건축심의", "시행최초", "시행변경", "관리최초", "관리변경",
            "이주시작", "이주종료", "착공", "총세대", "분양", "임대"]
# 자료의 '사업추진단계' 값 → 우리 단계. 건축심의는 조합설립 뒤, 사업시행인가 앞 단계다.
LABEL = {"구역지정": "정비구역지정", "추진위": "추진위원회", "조합설립": "조합설립인가", "건축심의": "조합설립인가",
         "사업시행": "사업시행인가", "관리처분": "관리처분인가", "착공": "착공"}
# 사업시행인가 고시 후 120일 안에 분양공고를 한다(도시정비법). 이 기간이 지난 구역은 분양신청이 열렸거나 끝났을 수 있어
# 조건부 구간(분양신청)으로 본다. 어디까지나 추정이다.
SALE_NOTICE_DAYS = 120


def keyword_of(name) -> str:
    """서울시 자료의 구역명을 구역 SHP의 이름과 맞추기 쉬운 키워드로 줄인다. (신림5구역주택재개발사업 → 신림5)"""
    n = re.sub(r"\s+", "", str(name))
    n = re.sub(r"재정비촉진(구역|지구)$", "", n)
    n = re.sub(r"(주택)?재개발(정비)?(사업|구역)?$", "", n)
    n = re.sub(r"구역$", "", n)
    return n


def _d(v):
    return None if pd.isna(v) else pd.Timestamp(v).date()


def derive(row: pd.Series, today: date) -> tuple[str | None, str]:
    """한 행의 (단계, 비고). 단계를 못 정하면 (None, 사유)."""
    label = str(row["단계"]).strip()
    stage = LABEL.get(label)
    if not stage:
        return None, f"사업추진단계 '{label}' 해석 불가"
    notes = []
    if stage == "관리처분인가" and _d(row["이주시작"]) and _d(row["이주시작"]) <= today:
        stage = "이주"
        notes.append(f"이주 시작 {_d(row['이주시작'])}")
    if stage == "사업시행인가":
        sd = _d(row["시행최초"])
        if sd:
            elapsed = (today - sd).days
            notes.append(f"사업시행인가 {sd}")
            if elapsed > SALE_NOTICE_DAYS:
                stage = "분양신청"
                notes.append(f"인가 후 {elapsed}일 경과: 분양신청 시기로 추정(분양공고 확인)")
    return stage, "; ".join(notes)


def read_xlsx(path: Path) -> pd.DataFrame:
    d = pd.read_excel(path, header=None, skiprows=HEAD_ROWS)
    d = d.iloc[:, :len(COLNAMES)]
    d.columns = COLNAMES[:d.shape[1]]
    d = d[d["자치구"].notna() & d["구역명"].notna()].copy()
    d["자치구"] = d["자치구"].astype(str).str.strip()
    return d


def import_xlsx(xlsx: Path, csv_path: Path, asof: str, districts: list[str] | None = None,
                today: date | None = None) -> dict:
    """xlsx를 읽어 zone_stage.csv에 병합한다. 같은 (자치구, 키워드)는 확인일이 같거나 더 늦을 때만 덮어쓴다."""
    today = today or date.today()
    d = read_xlsx(xlsx)
    if districts:
        d = d[d["자치구"].isin(districts)]
    existing = []
    if Path(csv_path).exists():
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            existing = list(csv.DictReader(f))
    # 예전 빈 틀 행(자치구·단계 없음)은 치운다. 사람이 채운 행과 다른 출처의 행은 그대로 둔다.
    existing = [r for r in existing if (r.get("단계") or "").strip() or (r.get("자치구") or "").strip()]
    index = {((r.get("자치구") or "").strip(), (r.get("구역키워드") or "").strip(), (r.get("출처") or "").strip()): i
             for i, r in enumerate(existing)}
    stat = {"읽음": len(d), "추가": 0, "갱신": 0, "건너뜀": 0, "해석불가": 0}
    for _, row in d.iterrows():
        stage, note = derive(row, today)
        if not stage:
            stat["해석불가"] += 1
            continue
        kw = keyword_of(row["구역명"])
        new = {"구역키워드": kw, "자치구": row["자치구"], "단계": stage, "확인일": asof, "출처": SOURCE,
               "사업유형": str(row["사업유형"]).strip(),
               "비고": "; ".join(x for x in [f"원본 구역명 {row['구역명']}", f"위치 {row['위치1']}", note] if x)}
        k = (row["자치구"], kw, SOURCE)
        if k in index:
            if asof >= (existing[index[k]].get("확인일") or ""):
                existing[index[k]] = new
                stat["갱신"] += 1
            else:
                stat["건너뜀"] += 1
        else:
            index[k] = len(existing)
            existing.append(new)
            stat["추가"] += 1
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(existing)
    return stat


def main():
    from match_auctions import load_config
    ap = argparse.ArgumentParser(description="서울시 정비사업 추진현황 xlsx를 zone_stage.csv로 가져오기")
    ap.add_argument("--file", help="xlsx 경로 (기본: WORK_DIR 아래 '서울시 정비사업 추진현황*.xlsx')")
    ap.add_argument("--asof", default="2026-06-30", help="자료 기준일 (파일명의 '26년 6월기준' 등, 기본 2026-06-30)")
    ap.add_argument("--districts", help="쉼표로 구분한 구 (기본: DISTRICTS 설정, 비우면 전체)")
    args = ap.parse_args()
    cfg = load_config()
    base = cfg["stage_file"].parent
    path = Path(args.file) if args.file else next(iter(sorted(base.glob("*서울시 정비사업 추진현황*.xlsx"))), None)
    if not path or not path.exists():
        raise SystemExit(f"xlsx를 찾지 못했습니다: {base}")
    districts = ([x.strip() for x in args.districts.split(",") if x.strip()] if args.districts is not None
                 else cfg["districts"])
    stat = import_xlsx(path, cfg["stage_file"], args.asof, districts)
    print(f"{path.name} → {cfg['stage_file']}  (구: {', '.join(districts) or '전체'})")
    print("  " + ", ".join(f"{k} {v}" for k, v in stat.items()))


if __name__ == "__main__":
    main()
