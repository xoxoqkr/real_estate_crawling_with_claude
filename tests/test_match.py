"""match()의 판정 로직 단위 테스트. 합성 폴리곤만 쓰므로 데이터 파일·API 키가 필요 없다.

좌표는 미터 좌표계(EPSG:5186)로 만든 뒤 match()에 넘길 때 물건만 WGS84로 변환한다.
구역은 한 변 100m 정사각형을 기본으로 쓴다.
"""
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from match_auctions import match  # noqa: E402
from zones import METRIC_CRS  # noqa: E402

X0, Y0 = 200_000.0, 550_000.0  # 서울 부근의 임의 기준점


def sq(x, y, w, h=None):
    """기준점에서 (x, y)만큼 떨어진 w×h(m) 사각형."""
    return box(X0 + x, Y0 + y, X0 + x + w, Y0 + y + (h if h is not None else w))


def make_zones(*specs):
    """specs: (이름, geometry) 또는 (이름, geometry, 출처)"""
    rows = [{"구역명": s[0], "구역출처": s[2] if len(s) > 2 else "테스트",
             "구역속성": f"NM={s[0]}", "geometry": s[1]} for s in specs]
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=METRIC_CRS)


def make_items(**geoms):
    """key=geometry (None이면 좌표실패 물건)."""
    rows = [{"key": k, "사건번호": k, "geometry": g} for k, g in geoms.items()]
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=METRIC_CRS)
    return gdf.to_crs("EPSG:4326")


def run(items, zones, near_m=50.0):
    return match(items, zones, near_m).set_index("key")


ZONE = make_zones(("가구역", sq(0, 0, 100)))


def test_fully_inside_is_gu_yeok_an():
    r = run(make_items(a=sq(40, 40, 10)), ZONE).loc["a"]
    assert r["판정"] == "구역안" and r["겹침%"] == 100 and r["구역명"] == "가구역"


def test_point_inside_counts_as_100():
    r = run(make_items(a=Point(X0 + 50, Y0 + 50)), ZONE).loc["a"]
    assert r["판정"] == "구역안" and r["겹침%"] == 100


@pytest.mark.parametrize("inside_w, expected", [(4, "구역걸침"), (6, "구역안")])
def test_partial_overlap_threshold_is_50_percent(inside_w, expected):
    # 폭 10m 필지가 구역 오른쪽 경계(x=100)에 걸쳐, 안쪽 폭만 inside_w
    parcel = sq(100 - inside_w, 40, 10)
    r = run(make_items(a=parcel), ZONE).loc["a"]
    assert r["판정"] == expected
    assert r["겹침%"] == pytest.approx(inside_w * 10, abs=1)


def test_exactly_50_percent_is_gu_yeok_an():
    r = run(make_items(a=sq(95, 40, 10)), ZONE).loc["a"]
    assert r["겹침%"] == 50 and r["판정"] == "구역안"


def test_near_boundary_within_threshold():
    r = run(make_items(a=sq(120, 40, 10)), ZONE).loc["a"]  # 구역 오른쪽 변에서 20m
    assert r["판정"].startswith("경계근접")
    assert r["경계거리m"] == pytest.approx(20, abs=0.5)
    assert r["구역명"] == "가구역"


def test_far_away_is_not_applicable():
    r = run(make_items(a=sq(300, 40, 10)), ZONE).loc["a"]
    assert r["판정"] == "해당없음" and pd.isna(r["구역명"])


def test_near_threshold_is_respected():
    items = make_items(a=sq(120, 40, 10))  # 20m 떨어짐
    assert run(items, ZONE, near_m=10).loc["a", "판정"] == "해당없음"
    assert run(items, ZONE, near_m=0).loc["a", "판정"] == "해당없음"
    assert run(items, ZONE, near_m=30).loc["a", "판정"].startswith("경계근접")


def test_missing_geometry_is_coordinate_failure():
    out = run(make_items(a=sq(40, 40, 10), b=None), ZONE)
    assert out.loc["b", "판정"] == "좌표실패"
    assert out.loc["a", "판정"] == "구역안"


def test_all_geocoding_failed():
    out = run(make_items(a=None, b=None), ZONE)
    assert set(out["판정"]) == {"좌표실패"}


def test_no_zone_hit_at_all():
    out = run(make_items(a=sq(500, 500, 10)), ZONE)
    assert out.loc["a", "판정"] == "해당없음"


def test_one_row_per_item_even_with_overlapping_zones():
    zones = make_zones(("가구역", sq(0, 0, 100)), ("나구역", sq(20, 20, 100), "다른파일"))
    out = match(make_items(a=sq(50, 50, 10)), zones, 50.0)
    assert len(out) == 1
    r = out.iloc[0]
    assert r["구역명"] == "가구역 / 나구역" and r["구역출처"] == "테스트 / 다른파일"


def test_inside_wins_over_near():
    # 가구역에는 걸치고, 나구역은 근처에 있어도 판정은 구역안
    zones = make_zones(("가구역", sq(0, 0, 100)), ("나구역", sq(130, 0, 100)))
    r = run(make_items(a=sq(40, 40, 10)), zones).loc["a"]
    assert r["판정"] == "구역안" and pd.isna(r["경계거리m"])


def test_nearest_zone_is_chosen_for_near():
    zones = make_zones(("가까운", sq(130, 0, 100)), ("먼", sq(-45, 0, 40)))
    r = run(make_items(a=sq(105, 40, 10)), zones).loc["a"]  # 가까운 구역까지 15m
    assert r["판정"].startswith("경계근접") and r["구역명"] == "가까운"


# ── 아래는 현재 구현의 알려진 문제. 고치면 xfail이 XPASS로 바뀌어 알려준다 ──

@pytest.mark.xfail(strict=True, reason="intersects가 변 접촉도 참이라 겹침 0%가 '구역걸침'이 됨")
def test_edge_touching_only_is_not_overlap():
    r = run(make_items(a=sq(100, 40, 10)), ZONE).loc["a"]  # 변만 맞닿음
    assert r["판정"] != "구역걸침"


@pytest.mark.xfail(strict=True, reason="겹침%를 구역별 max로 집계해, 인접한 두 구역에 나뉜 필지가 과소 계산됨")
def test_parcel_split_across_two_zones_uses_union():
    zones = make_zones(("가구역", sq(0, 0, 100)), ("나구역", sq(100, 0, 100)))
    r = run(make_items(a=sq(96, 40, 10)), zones).loc["a"]  # 가 40% + 나 60%... 합 100%
    assert r["겹침%"] == 100
