"""stage.py 단위 테스트: 단계 해석, 입주권 구간 분류, 구역명 매칭, 틀 생성."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import stage  # noqa: E402


@pytest.mark.parametrize("text, expected", [
    ("사업시행인가", "사업시행인가"), ("사업시행인가 완료", "사업시행인가"), ("조합설립", "조합설립인가"),
    ("관리처분 중", "관리처분인가"), ("추진위", "추진위원회"), ("정비구역 지정", "정비구역지정"),
    ("분양공고", "분양신청"), ("사업시행인가 완료, 관리처분 준비", None),  # 둘 이상 섞이면 해석 안 함
    ("", None), (None, None), ("알수없음", None),
])
def test_normalize_stage(text, expected):
    assert stage.normalize_stage(text) == expected


@pytest.mark.parametrize("st, tier", [
    ("정비구역지정", "A"), ("추진위원회", "A"), ("조합설립인가", "A"), ("사업시행인가", "A"),
    ("분양신청", "B"), ("관리처분인가", "C"),
    ("후보지", "제외"), ("이주", "제외"), ("철거", "제외"), ("준공", "제외"),
    (None, "단계미확인"),
])
def test_tier(st, tier):
    assert stage.tier_of(st) == tier


def test_lookup_keyword_does_not_match_longer_number():
    rows = [{"kw": "신림1", "stage": "조합설립인가", "date": "2026-01-01"}]
    assert stage.lookup("신림10구역 재개발", rows)[0] is None
    assert stage.lookup("신림1구역 재개발", rows)[0] == "조합설립인가"


def test_lookup_prefers_latest_date():
    rows = [{"kw": "가구역", "stage": "조합설립인가", "date": "2026-01-01"},
            {"kw": "가구역", "stage": "사업시행인가", "date": "2026-06-01"}]
    assert stage.lookup("가구역", rows) == ("사업시행인가", "2026-06-01")


def _csv(tmp_path, body):
    p = tmp_path / "zone_stage.csv"
    p.write_text("구역키워드,단계,확인일,출처,비고\n" + body, encoding="utf-8-sig")
    return p


def test_attach_assigns_tier_only_inside_zone(tmp_path):
    p = _csv(tmp_path, "신림5구역,사업시행인가,2026-10-01,수동,\n신림7,관리처분,2026-10-01,수동,\n")
    df = pd.DataFrame({
        "판정": ["구역안", "구역걸침", "경계근접(<50m)", "해당없음", "구역안", "구역안"],
        "구역명": ["(이름 없음) 고시 2026-100 / 신림5구역 주택정비형 [x]", "신림7 재개발", "신림5구역", None,
                 "미지의구역", "신림50구역"],
    })
    out = stage.attach(df, p)
    assert out["입주권단계"].tolist() == ["A", "C", "", "", "단계미확인", "단계미확인"]
    assert out["사업단계"].tolist() == ["사업시행인가", "관리처분인가", "사업시행인가", "", "", ""]


def test_attach_without_stage_file_marks_unknown(tmp_path):
    df = pd.DataFrame({"판정": ["구역안", "해당없음"], "구역명": ["가", None]})
    assert stage.attach(df, tmp_path / "없음.csv")["입주권단계"].tolist() == ["단계미확인", ""]


def test_zone_keywords():
    name = "(이름 없음) 고시 2026-100 / 신림5구역 주택정비형 재개발사업 [주택정비형, 고시 2026.03.20]"
    assert stage.zone_keywords(name) == ["신림5구역"]
    assert stage.zone_keywords("신림7 주택정비형 재개발정비구역 [x] / 신림7 주택정비형 재개발사업 [y]") == ["신림7"]


def test_make_template_keeps_existing_rows(tmp_path):
    xlsx = tmp_path / "r.xlsx"
    pd.DataFrame({"판정": ["구역안", "경계근접(<50m)", "해당없음"],
                  "구역명": ["가구역 재개발 [a]", "나구역 [b]", "다구역"]}).to_excel(xlsx, sheet_name="전체", index=False)
    p = _csv(tmp_path, "가구역,관리처분인가,2026-10-01,수동,메모\n")
    assert stage.make_template(xlsx, p) == 1  # 나구역만 추가, 다구역은 해당없음이라 제외
    rows = stage.load_stage_table(p)
    assert rows == [{"kw": "가구역", "stage": "관리처분인가", "date": "2026-10-01"}]
    assert "나구역" in p.read_text(encoding="utf-8-sig")
