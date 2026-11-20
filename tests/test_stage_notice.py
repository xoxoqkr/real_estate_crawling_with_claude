"""stage_notice.py: 구청 고시 해석과 zone_stage.csv 병합. 게시판 샘플 HTML(tests/fixtures/notice)로 파서를 검증한다."""
import csv
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stage  # noqa: E402
import stage_notice as sn  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "notice"


def N(title, number="고시 제2026-1호", gu="관악구", date="2026-11-01", detail=""):
    return sn.Notice(gu=gu, number=number, title=title, date=date, url="https://example/view", detail_text=detail)


# ---------------------------------------------------------------- 제목 해석
@pytest.mark.parametrize("title, stage_, kw, changed", [
    ("봉천제4-1-3구역 주택재개발정비사업 관리처분계획인가 고시", "관리처분인가", "봉천4-1-3", False),
    ("신림8구역 주택정비형 재개발사업 조합설립인가 고시", "조합설립인가", "신림8", False),
    ("신림10구역 주택정비형 재개발사업 조합설립추진위원회 승인 고시", "추진위원회", "신림10", False),
    ("신림7구역 주택정비형 재개발사업 정비계획 결정 및 정비구역 지정, 지구단위계획(구역) 결정 및 지형도면 고시",
     "정비구역지정", "신림7", False),
    ("강남아파트 재건축정비사업 관리처분계획(변경)인가", "관리처분인가", "강남아파트", True),
    ("신림2재정비촉진구역 재개발정비사업 관리처분계획 변경인가 고시", "관리처분인가", "신림2", True),
    ("자양 현대한창 가로주택정비사업 사업시행계획인가 고시", "사업시행인가", "자양현대한창", False),
    ("자양동 92-1번지 일원 소규모재개발정비사업 조합설립(변경)인가 고시", "조합설립인가", "자양동92-1번지", True),
    ("자양4동 A구역 주택정비형 재개발사업 사업시행계획인가 고시", "사업시행인가", "자양4동A", False),
    ("봉천 제14구역 주택재개발정비사업 준공인가 고시", "준공", "봉천14", False),
])
def test_classify_target(title, stage_, kw, changed):
    result, info = sn.classify(N(title))
    assert result == "대상"
    assert (info["단계"], info["구역키워드"], info["변경"]) == (stage_, kw, changed)


def test_sale_notice_is_target_even_as_gongo():
    result, info = sn.classify(N("봉천14구역 주택재개발정비사업 조합원 분양신청 공고", number="공고 제2026-10호"))
    assert result == "대상" and info["단계"] == "분양신청"


@pytest.mark.parametrize("title, number", [
    ("봉천 제14구역 주택재개발정비사업 사업시행계획 변경 공람공고", "공고 제2026-2233호"),
    ("사업시행계획인가를 위한 주민공람 공고(중곡아파트 공공재건축정비사업)", "공고 제2026-1308호"),
    ("자양3동 A구역 주택정비형 재개발사업 조합설립주민협의체 부위원장(주민대표) 선거 선거인명부 확정 공고", "공고 제2026-1305호"),
    ("신림2재정비촉진구역 재개발정비사업 재결서 정본 공시송달 공고", "공고 제2026-1956호"),
    ("봉천제4-1-2구역 주택재개발 정비계획 (경미한)변경 및 지형도면 정정 고시", "고시 제2026-103호"),  # 단계 변화 없음
    ("자양1 주택재건축 소공원 조성계획결정(최초) 및 지형도면 고시", "고시 제2025-2호"),
    ("도시계획시설(도로)사업 실시계획 인가 고시", "고시 제2024-106호"),
    ("신림5구역 주택정비형 재개발사업 조합설립인가 고시", "공고 제2026-1호"),  # 고시가 아닌 공고
])
def test_classify_not_target(title, number):
    assert sn.classify(N(title, number=number))[0] in ("무관", "해석불가")


def test_cancel_notice_needs_check():
    assert sn.classify(N("신림11구역 정비구역 해제 고시"))[0] == "확인필요"
    assert sn.classify(N("OO구역 주택재개발정비사업 조합설립인가 취소 고시"))[0] == "확인필요"


def test_operator_designation_is_not_project_approval():
    """'사업시행자 지정'(신탁·모아타운)은 사업시행계획인가로 보지 않는다."""
    title = "성현동 1021번지 일대 소규모주택정비 관리지역(모아타운) 1구역 가로주택정비사업 사업시행자 지정 고시"
    assert sn.classify(N(title))[0] == "확인필요"
    assert sn.classify(N("신림5구역 주택정비형 재개발사업 사업시행자 고시"))[0] == "확인필요"


def test_non_project_sale_notice_is_ignored():
    assert sn.classify(N('2023년 서울특별시 관악구 "친환경 도시텃밭" 분양 공고', number="공고 제2023-218호"))[0] == "무관"


def test_rebuild_keyword_drops_housing_word():
    assert sn.classify(N("봉천1-1 주택재건축정비사업 사업시행계획인가 정정 고시"))[1]["구역키워드"] == "봉천1-1"


def test_zone_name_from_body_when_title_has_none():
    body = ("「도시 및 주거환경정비법」제31조 … 「워커힐아파트1단지 재건축정비사업 조합설립추진위원회」를 승인하고 "
            "다음과 같이 고시합니다.")
    result, info = sn.classify(N("재건축정비사업 조합설립추진위원회 승인 고시", gu="광진구", detail=body))
    assert result == "대상" and (info["단계"], info["구역키워드"]) == ("추진위원회", "워커힐아파트1단지")


def test_no_zone_name_is_unparsable():
    assert sn.classify(N("자율주택정비사업 준공인가 및 공사완료 고시"))[0] == "해석불가"


@pytest.mark.parametrize("text, want", [
    ("권리산정기준일 : 2025. 1. 23.", "2025-01-23"),
    ("권리산정기준일은 2024년 3월 5일로 한다", "2024-03-05"),
    ("권리 산정 기준일: 2023-12-01", "2023-12-01"),
    ("기준일 없음", ""),
])
def test_rights_date(text, want):
    assert sn.rights_date(text) == want


@pytest.mark.parametrize("text, want", [
    ("다. 고시일자: 2026. 6. 26. 라. 고시방법: 구보 게재", "2026-06-26"),
    ("다음과 같이 고시합니다. 2026년 10월 26일 서울특별시 광진구청장 1. 사업의 종류", "2026-10-26"),
    ("고시합니다. 2026년 8월 27일 서울특별시 광 진 구 청 장 1. 사업의 명칭", "2026-08-27"),
    ("다음과 같이 고시합니다. 2025. 9. 11. 서울특별시 광진구청장 붙임 : 고시문", "2025-09-11"),
    ("날짜 없음", ""),
])
def test_notice_date(text, want):
    assert sn.notice_date(text) == want


def test_merge_prefers_date_in_body(tmp_path):
    p = tmp_path / "zone_stage.csv"
    sn.merge([N("신림8구역 주택정비형 재개발사업 조합설립인가 고시", date="2026-10-06", detail="고시일자: 2026. 10. 8.")],
             p, log=lambda *a: None)
    assert read_csv(p)[0]["확인일"] == "2026-10-08"


# ---------------------------------------------------------------- 병합
def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=stage.COLS)
        w.writeheader()
        w.writerows(rows)


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


SEOUL = {"구역키워드": "봉천14", "자치구": "관악구", "단계": "분양신청", "확인일": "2026-06-30",
         "출처": "서울시 정비사업 추진현황", "사업유형": "주택정비형 재개발", "비고": "원본 구역명 봉천14"}
MANUAL = {"구역키워드": "신림8", "자치구": "관악구", "단계": "추진위원회", "확인일": "2026-01-01",
          "출처": "수동", "사업유형": "", "비고": "직접 적음"}


def test_merge_adds_and_keeps_other_sources(tmp_path):
    p = tmp_path / "zone_stage.csv"
    write_csv(p, [SEOUL, MANUAL])
    notices = [
        N("봉천14구역 주택재개발정비사업 관리처분계획인가 고시", number="서울특별시 관악구 고시 제2026-70호",
          date="2026-10-01", detail="권리산정기준일 : 2010. 7. 15."),
        N("신림8구역 주택정비형 재개발사업 조합설립인가 고시", number="고시 제2026-101호", date="2026-10-08"),
        N("봉천 제14구역 주택재개발정비사업 사업시행계획 변경 공람공고", number="공고 제2026-2233호"),
    ]
    stats = sn.merge(notices, p, log=lambda *a: None)
    rows = read_csv(p)
    assert stats["관악구"]["추가"] == 2 and stats["관악구"]["무관"] == 1
    assert SEOUL in [dict(r) for r in rows] and MANUAL in [dict(r) for r in rows]  # 다른 출처 행은 그대로
    new = next(r for r in rows if r["출처"] == "관악구 고시 제2026-70호")
    assert (new["구역키워드"], new["단계"], new["확인일"]) == ("봉천14", "관리처분인가", "2026-10-01")
    assert new["사업유형"] == "주택정비형 재개발"  # 같은 구역의 기존 행에서 가져옴
    assert "권리산정기준일 2010-07-15" in new["비고"] and "URL https://example/view" in new["비고"]


def test_merge_is_idempotent_and_updates_only_with_later_date(tmp_path):
    p = tmp_path / "zone_stage.csv"
    n = N("신림8구역 주택정비형 재개발사업 조합설립인가 고시", number="고시 제2026-101호", date="2026-10-08")
    sn.merge([n], p, log=lambda *a: None)
    st = sn.merge([n], p, log=lambda *a: None)
    assert st["관악구"]["건너뜀"] == 1 and len(read_csv(p)) == 1
    later = N(n.title, number=n.number, date="2026-10-09")
    assert sn.merge([later], p, log=lambda *a: None)["관악구"]["갱신"] == 1
    assert read_csv(p)[0]["확인일"] == "2026-10-09"


def test_merge_never_lowers_stage(tmp_path):
    """착공한 구역에 사업시행계획 변경인가가 나와도 단계를 되돌리지 않는다."""
    p = tmp_path / "zone_stage.csv"
    write_csv(p, [{**SEOUL, "구역키워드": "신림2", "단계": "착공"}])
    st = sn.merge([N("신림2재정비촉진구역 재개발정비사업 사업시행계획 변경인가 고시", date="2026-11-01")], p,
                  log=lambda *a: None)
    assert st["관악구"]["확인필요"] == 1 and len(read_csv(p)) == 1


def test_merge_skips_notices_before_cancellation(tmp_path):
    p = tmp_path / "zone_stage.csv"
    notices = [N("황해연립 재건축정비사업 조합설립(경미한)변경인가 고시", date="2023-04-13"),
               N("황해연립 소규모재건축사업 조합설립인가 취소 고시", date="2025-10-28")]
    st = sn.merge(notices, p, log=lambda *a: None)
    assert st["관악구"]["확인필요"] == 2 and read_csv(p) == []


def test_merge_dry_run_does_not_write(tmp_path):
    p = tmp_path / "zone_stage.csv"
    write_csv(p, [SEOUL])
    sn.merge([N("신림8구역 주택정비형 재개발사업 조합설립인가 고시")], p, dry_run=True, log=lambda *a: None)
    assert len(read_csv(p)) == 1


def test_notice_row_wins_in_stage_attach(tmp_path):
    """병합 뒤 stage.attach가 같은 구역의 서울시 행(분기 자료)보다 고시 행(더 최근)을 쓴다."""
    p = tmp_path / "zone_stage.csv"
    write_csv(p, [SEOUL])
    sn.merge([N("봉천14구역 주택재개발정비사업 관리처분계획인가 고시", date="2026-10-01")], p, log=lambda *a: None)
    df = pd.DataFrame([{"판정": "구역안", "구": "관악구", "구역명": "봉천14구역 주택정비형 재개발구역 [고시 2009.12.24]"}])
    out = stage.attach(df, p)
    assert out.loc[0, "사업단계"] == "관리처분인가"
    assert out.loc[0, "단계확인일"] == "2026-10-01"
    assert out.loc[0, "입주권단계"] == "C"


def test_notice_beats_later_seoul_row_with_lower_stage(tmp_path):
    """6/26 관리처분인가 고시가 6/30 기준 서울시 자료의 '분양신청(추정)'에 밀리지 않는다 (봉천4-1-3 실제 사례)."""
    p = tmp_path / "zone_stage.csv"
    write_csv(p, [{**SEOUL, "구역키워드": "봉천4-1-3", "단계": "분양신청", "확인일": "2026-06-30"}])
    sn.merge([N("봉천제4-1-3구역 주택재개발정비사업 관리처분계획인가 고시", date="2026-06-25",
                detail="다. 고시일자: 2026. 6. 26.")], p, log=lambda *a: None)
    df = pd.DataFrame([{"판정": "구역안", "구": "관악구", "구역명": "봉천4-1-3구역 [고시 2011.02.24]"}])
    out = stage.attach(df, p)
    assert (out.loc[0, "사업단계"], out.loc[0, "단계확인일"], out.loc[0, "입주권단계"]) == ("관리처분인가", "2026-06-26", "C")


def test_manual_row_still_wins_when_latest(tmp_path):
    """가장 최근 행이 사람이 적은 행이면 단계가 낮아도 그 값을 쓴다 (예외는 서울시 분기 자료에만)."""
    p = tmp_path / "zone_stage.csv"
    write_csv(p, [{**SEOUL, "구역키워드": "신림9", "단계": "조합설립인가", "확인일": "2026-06-30"},
                  {**MANUAL, "구역키워드": "신림9", "단계": "정비구역지정", "확인일": "2026-11-01"}])
    assert stage.lookup("신림9구역", stage.load_stage_table(p), "관악구")[0] == "정비구역지정"


# ---------------------------------------------------------------- 게시판 샘플 (실제 HTML)
def test_parse_gwanak_list_fixture():
    html = (FIX / "gwanak_list.html").read_text(encoding="utf-8")
    items = sn.GwanakAdapter().parse_list(html)
    assert len(items) >= 10
    gosi = [n for n in items if n.is_gosi]
    assert gosi, "고시 행이 있어야 함"
    for n in items:
        assert n.number and n.title and len(n.date) == 10
        assert n.url.startswith("https://www.gwanak.go.kr/site/gwanak/ex/bbsNew/View.do?typeCode=1&not_ancmt_mgt_no=")
    assert all(n.source.startswith("관악구 ") and "서울특별시" not in n.source for n in items)


def test_parse_gwangjin_list_fixture():
    html = (FIX / "gwangjin_list.html").read_text(encoding="utf-8")
    items = sn.GwangjinAdapter().parse_list(html)
    assert len(items) >= 5
    for n in items:
        assert n.number and n.title and len(n.date) == 10
        assert "notAncmtMgtNo=" in n.url and n.url.startswith("https://www.gwangjin.go.kr/portal/bbs/B0000378/view.do")
    assert any(n.is_gosi for n in items)


@pytest.mark.parametrize("name, adapter", [("gwanak_detail.html", sn.GwanakAdapter),
                                           ("gwangjin_detail.html", sn.GwangjinAdapter)])
def test_parse_detail_fixture(name, adapter):
    text = adapter.parse_detail((FIX / name).read_text(encoding="utf-8"))
    assert "고시" in text and "<script" not in text and len(text) > 200
    assert len(sn.notice_date(text)) == 10  # 본문에서 고시일을 읽는다


def test_fixture_titles_classify_without_errors():
    """샘플 목록의 모든 제목이 예외 없이 분류된다."""
    for name, ad in [("gwanak_list.html", sn.GwanakAdapter), ("gwangjin_list.html", sn.GwangjinAdapter)]:
        for n in ad().parse_list((FIX / name).read_text(encoding="utf-8")):
            assert sn.classify(n)[0] in ("대상", "무관", "확인필요", "해석불가")
