"""재개발 사업 단계와 '입주권(조합원 분양권) 가능 구간' 판정.

기준 (서울 = 투기과열지구 가정, 2차 자료 기반의 작업용 기준이며 입찰 전 법령·조합 확인 필요)
  A  정비구역지정 ~ 사업시행인가   : 조합원 지위 양도 제한이 없는 구간. 가능성 높음
  B  분양신청                      : 낙찰 시점과 분양신청 기간이 어긋나면 현금청산. 조건부
  C  관리처분인가                  : 일반 매매는 불가. 금융기관·국가 채권자 경매만 승계 가능
  제외  후보지, 이주 이후          : 구역 미지정 또는 매수 실익 낮음
  단계미확인                       : 단계 자료가 없음
공통 조건: 권리산정기준일 이전 건물인지, 1인 1분양 원칙에 걸리지 않는지 (결과 엑셀 '확인할것' 열)

단계 자료는 zone_stage.csv (WORK_DIR 아래)에 둔다. 열: 구역키워드, 단계, 확인일, 출처, 비고
  - 구역키워드는 매칭 결과의 구역명에 들어 있는 말 (예: 신림5구역). 뒤에 숫자가 이어지면 다른 구역으로 본다.
  - 단계는 STAGES의 이름 또는 별칭. 재개발닷컴 등에서 읽은 값이든 직접 적은 값이든 같은 파일에 쌓는다.

  python stage.py --template   # 최신 결과 엑셀의 구역들로 빈 zone_stage.csv 틀을 만든다(기존 행 유지)
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import pandas as pd

# (이름, 별칭들) 사업 진행 순서
STAGES = [
    ("후보지", ["후보지", "선정지", "대상지"]),
    ("정비구역지정", ["정비구역지정", "구역지정", "정비구역"]),
    ("추진위원회", ["추진위원회", "추진위"]),
    ("조합설립인가", ["조합설립인가", "조합설립"]),
    ("사업시행인가", ["사업시행인가", "사업시행"]),
    ("분양신청", ["분양신청", "분양공고"]),
    ("관리처분인가", ["관리처분인가", "관리처분"]),
    ("이주", ["이주"]),
    ("철거", ["철거"]),
    ("착공", ["착공"]),
    ("준공", ["준공"]),
    ("이전고시", ["이전고시", "청산"]),
]
TIER = {
    "정비구역지정": "A", "추진위원회": "A", "조합설립인가": "A", "사업시행인가": "A",
    "분양신청": "B",
    "관리처분인가": "C",
    "후보지": "제외", "이주": "제외", "철거": "제외", "착공": "제외", "준공": "제외", "이전고시": "제외",
}
UNKNOWN = "단계미확인"
TIER_NOTE = {
    "A": "A 정비구역지정~사업시행인가: 조합원 승계 제한 없는 구간",
    "B": "B 분양신청: 낙찰 시점이 분양신청 기간과 맞는지 조합에 확인",
    "C": "C 관리처분인가 후: 경매 신청 채권자가 금융기관·국가일 때만 조합원 승계 가능",
}
COLS = ["구역키워드", "단계", "확인일", "출처", "비고"]


def normalize_stage(text) -> str | None:
    """자유 문장에서 단계 하나를 뽑는다. 단계가 둘 이상 섞여 있거나 못 찾으면 None."""
    t = re.sub(r"\s+", "", str(text or ""))
    t = re.sub(r"(완료|중|예정|진행)$", "", t)
    if not t:
        return None
    for name, aliases in STAGES:
        if t == name or t in aliases:
            return name
    hit = {name for name, aliases in STAGES if any(a in t for a in aliases)}
    return hit.pop() if len(hit) == 1 else None


def tier_of(stage: str | None) -> str:
    return TIER.get(stage or "", UNKNOWN)


def load_stage_table(path: Path) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            kw = (r.get("구역키워드") or "").strip()
            st = normalize_stage(r.get("단계"))
            if kw and st:
                rows.append({"kw": kw, "stage": st, "date": (r.get("확인일") or "").strip()})
            elif kw and (r.get("단계") or "").strip():
                print(f"  zone_stage.csv: '{kw}'의 단계 '{r.get('단계')}'를 해석하지 못해 건너뜀")
    return rows


def lookup(zone_name, rows: list[dict]) -> tuple[str | None, str]:
    """구역명에 키워드가 들어 있는 행 중 확인일이 가장 최근인 것의 (단계, 확인일)."""
    if not isinstance(zone_name, str) or not zone_name:
        return None, ""
    best = None
    for r in rows:
        if re.search(re.escape(r["kw"]) + r"(?!\d)", zone_name):
            if best is None or r["date"] > best["date"]:
                best = r
    return (best["stage"], best["date"]) if best else (None, "")


def attach(df: pd.DataFrame, csv_path: Path) -> pd.DataFrame:
    """결과 표에 사업단계, 단계확인일, 입주권단계 열을 붙인다. 입주권단계는 구역안·구역걸침에만 매긴다."""
    df = df.copy()
    for c in ("사업단계", "단계확인일", "입주권단계"):
        df[c] = ""
    rows = load_stage_table(csv_path)
    judge = df["판정"].astype(str)
    related = judge.isin(["구역안", "구역걸침"]) | judge.str.startswith("경계")
    for i in df.index[related]:
        stage, date = lookup(df.at[i, "구역명"], rows)
        if stage:
            df.at[i, "사업단계"], df.at[i, "단계확인일"] = stage, date
        if judge[i] in ("구역안", "구역걸침"):
            df.at[i, "입주권단계"] = tier_of(stage)
    return df


def zone_keywords(zone_name) -> list[str]:
    """구역명 문자열에서 구역 키워드 후보(각 구역 이름의 첫 단어)를 뽑는다."""
    out = []
    for part in str(zone_name or "").split(" / "):
        part = part.strip()
        if not part or part.startswith("(이름 없음)"):
            continue
        kw = part.split(" [")[0].split()[0] if part.split(" [")[0].split() else ""
        if kw and kw not in out:
            out.append(kw)
    return out


def make_template(result_xlsx: Path, csv_path: Path) -> int:
    """최신 결과의 구역들을 키워드로 정리해 빈 단계 틀을 만든다. 이미 있는 키워드는 건드리지 않는다."""
    df = pd.read_excel(result_xlsx, sheet_name="전체")
    df = df[df["판정"].astype(str).isin(["구역안", "구역걸침"]) | df["판정"].astype(str).str.startswith("경계")]
    existing = []
    if Path(csv_path).exists():
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            existing = list(csv.DictReader(f))
    have = {(r.get("구역키워드") or "").strip() for r in existing}
    added = 0
    for name in df["구역명"].dropna().unique():
        for kw in zone_keywords(name):
            if kw not in have:
                existing.append({"구역키워드": kw, "단계": "", "확인일": "", "출처": "",
                                 "비고": str(name)[:80]})
                have.add(kw)
                added += 1
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(existing)
    return added


def main():
    from match_auctions import load_config
    from sa_interest import latest_result
    ap = argparse.ArgumentParser(description="구역별 사업 단계 파일(zone_stage.csv) 관리")
    ap.add_argument("--template", action="store_true", help="최신 결과 엑셀의 구역으로 빈 틀 만들기")
    args = ap.parse_args()
    cfg = load_config()
    if args.template:
        n = make_template(latest_result(cfg["output"]), cfg["stage_file"])
        print(f"구역 {n}개를 추가했습니다: {cfg['stage_file']}  ('단계' 열을 채워 주세요)")
        print("단계 값:", ", ".join(n for n, _ in STAGES))


if __name__ == "__main__":
    main()
