"""stage_seoul.py: 서울시 추진현황 xlsx 가져오기. 합성 엑셀로 검증한다."""
import csv
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stage  # noqa: E402
import stage_seoul as ss  # noqa: E402

TODAY = date(2026, 11, 20)


@pytest.mark.parametrize("raw, kw", [
    ("신림5", "신림5"), ("신림5구역주택재개발사업", "신림5"), ("자양5재정비촉진구역", "자양5"),
    ("자양4동A구역주택재개발사업", "자양4동A"), ("극동아파트", "극동아파트"), ("봉천4-1-3", "봉천4-1-3"),
    ("신림1 재정비촉진구역", "신림1"), ("봉천13구역", "봉천13"),
])
def test_keyword_of(raw, kw):
    assert ss.keyword_of(raw) == kw


def row(**kw):
    base = {c: pd.NaT for c in ss.COLNAMES}
    base.update({"자치구": "관악구", "구역명": "신림5", "위치1": "신림동1", "사업유형": "주택정비형 재개발", "단계": "조합설립"})
    base.update(kw)
    return pd.Series(base)


@pytest.mark.parametrize("label, stage_", [
    ("구역지정", "정비구역지정"), ("추진위", "추진위원회"), ("조합설립", "조합설립인가"), ("건축심의", "조합설립인가"),
    ("관리처분", "관리처분인가"), ("착공", "착공"),
])
def test_derive_label_mapping(label, stage_):
    assert ss.derive(row(단계=label), TODAY)[0] == stage_


def test_derive_unknown_label():
    assert ss.derive(row(단계="???"), TODAY)[0] is None


def test_derive_sale_notice_heuristic():
    recent = row(단계="사업시행", 시행최초=pd.Timestamp("2026-09-01"))   # 80일 전
    old = row(단계="사업시행", 시행최초=pd.Timestamp("2026-01-01"))      # 323일 전
    assert ss.derive(recent, TODAY)[0] == "사업시행인가"
    st, note = ss.derive(old, TODAY)
    assert st == "분양신청" and "추정" in note


def test_derive_relocation_after_disposition():
    assert ss.derive(row(단계="관리처분", 이주시작=pd.Timestamp("2026-05-01")), TODAY)[0] == "이주"
    assert ss.derive(row(단계="관리처분", 이주시작=pd.Timestamp("2027-05-01")), TODAY)[0] == "관리처분인가"


def make_xlsx(path, rows):
    head = pd.DataFrame([[None] * len(ss.COLNAMES)] * ss.HEAD_ROWS)
    body = pd.DataFrame([[r.get(c) for c in ss.COLNAMES] for r in rows])
    pd.concat([head, body]).to_excel(path, header=False, index=False)


def test_import_merges_and_respects_newer_rows(tmp_path):
    x, c = tmp_path / "s.xlsx", tmp_path / "zone_stage.csv"
    make_xlsx(x, [
        {"자치구": "관악구", "구역명": "신림5", "사업유형": "주택정비형 재개발", "단계": "조합설립", "위치1": "신림동412"},
        {"자치구": "광진구", "구역명": "자양7", "사업유형": "단독주택재건축", "단계": "구역지정", "위치1": "자양동1"},
        {"자치구": "종로구", "구역명": "수송1", "사업유형": "도시정비형 재개발", "단계": "구역지정", "위치1": "수송동1"},
    ])
    # 이미 있는 구청 고시 행(더 최근)과 예전 빈 틀 행
    c.write_text("구역키워드,자치구,단계,확인일,출처,사업유형,비고\n"
                 "신림5,관악구,사업시행인가,2026-10-01,관악구 고시,,\n신림9구역,,,,,,틀\n", encoding="utf-8-sig")
    st = ss.import_xlsx(x, c, "2026-06-30", ["관악구", "광진구"], TODAY)
    assert st["읽음"] == 2 and st["추가"] == 2
    rows = list(csv.DictReader(open(c, encoding="utf-8-sig")))
    assert not any(r["구역키워드"] == "신림9구역" for r in rows)  # 빈 틀 행 정리
    assert not any(r["구역키워드"] == "수송1" for r in rows)       # 대상 구 아님
    # 구청 고시(10월)가 서울시 자료(6월 기준)보다 최근이라 매칭 결과는 고시 값
    tbl = stage.load_stage_table(c)
    assert stage.lookup("신림5구역주택재개발사업", tbl, "관악구")[:2] == ("사업시행인가", "2026-10-01")
    # 재실행하면 같은 출처 행은 갱신(중복 안 생김)
    ss.import_xlsx(x, c, "2026-06-30", ["관악구", "광진구"], TODAY)
    assert len(list(csv.DictReader(open(c, encoding="utf-8-sig")))) == len(rows)
