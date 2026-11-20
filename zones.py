"""정비구역 경계 파일(SHP, GeoJSON, GPKG)을 읽어 하나의 표로 합칩니다.

zones 폴더에 넣은 파일을 모두 읽습니다. 예:
  - 국토교통부_(연속주제)_도시및주거환경정비_정비구역 SHP (공공데이터포털)
  - 서울 열린데이터광장 의제처리구역 SHP (정비구역·재정비촉진지구 등)
  - 직접 그린 후보지(신통·모아타운) GeoJSON: geojson.io에서 그려 저장, 속성에 name을 넣으면 구역명으로 씀
"""
from __future__ import annotations

import re
import warnings
from pathlib import Path

import geopandas as gpd
import pandas as pd

METRIC_CRS = "EPSG:5186"  # 중부원점 GRS80 (미터)
SEOUL_BBOX_4326 = (126.76, 37.41, 127.19, 37.72)
NAME_CANDIDATES = ("name", "구역명", "ZONE_NM", "DGM_NM", "UQ_NM", "PRPOS_AREA_DSTRC_NM", "NM", "명칭", "사업명")
PATTERNS = ("*.shp", "*.geojson", "*.json", "*.gpkg")

# 서울 의제처리구역(UPIS_C_UQ181) 최종코드(ATRB_SE) → 유형. 레이어표_181.xlsx 기준 (2026-09판)
UQ_TYPES = {
    "UQ1200": "정비구역", "UQ1206": "주택재건축사업", "UQ1210": "주거환경개선사업구역", "UQ1211": "주거환경개선사업",
    "UQ1212": "주거환경관리사업", "UQ1220": "재개발사업구역", "UQ1221": "주택정비형 재개발구역",
    "UQ1222": "도시정비형 재개발구역", "UQ1230": "재개발사업지구", "UQ1231": "주택정비형 재개발지구",
    "UQ1232": "도시정비형 재개발지구", "UQ1240": "재건축사업구역", "UQ1250": "결합정비구역",
    "UQ1260": "자율주택정비사업구역", "UQ1270": "가로주택정비사업구역", "UQ1280": "소규모재건축사업구역",
    "UQ1290": "정비구역(도시및주거환경정비)", "UQ1811": "자율주택정비사업", "UQ1812": "가로주택정비사업",
    "UQ1813": "소규모재건축사업", "UQ5100": "재정비촉진지구", "UQ5110": "주거지형 재정비촉진지구",
    "UQ5120": "중심지형 재정비촉진지구", "UQ5130": "고밀복합형 재정비촉진지구", "UQ5140": "존치정비구역",
    "UQ5150": "존치관리구역", "UQ6400": "시장정비구역", "UQ1100": "도시개발구역", "UQ1700": "아파트지구개발사업",
    "UQ1900": "토지구획정리사업", "UQ5600": "일단의주택지조성사업지역", "UQ6500": "택지개발지구",
}
# 기본으로 쓰는 유형(코드 앞자리): 정비구역, 소규모정비, 재정비촉진지구, 시장정비구역.
# 토지구획정리·택지개발 등 끝난 옛 사업지는 서울 시가지를 넓게 덮어 판정을 흐리므로 뺌. .env의 ZONE_TYPE_PREFIXES로 변경
DEFAULT_TYPE_PREFIXES = "UQ12,UQ18,UQ51,UQ64"


def _read(path: Path, default_crs: str) -> gpd.GeoDataFrame:
    gdf = None
    if path.suffix.lower() == ".shp":
        for enc in ("utf-8", "cp949"):
            try:
                gdf = gpd.read_file(path, encoding=enc)
                # 한글이 깨졌는지 대충 확인
                sample = " ".join(gdf.select_dtypes("object").head(20).astype(str).values.ravel())
                if "�" in sample:
                    continue
                break
            except Exception:  # noqa: BLE001
                gdf = None
        if gdf is None:
            gdf = gpd.read_file(path)
    else:
        gdf = gpd.read_file(path)
    if gdf.crs is None:
        warnings.warn(f"{path.name}: 좌표계 정보(.prj)가 없어 {default_crs}로 가정합니다.")
        gdf = gdf.set_crs(default_crs)
    return gdf


def _uq181_name(r) -> str:
    nm = str(r["DGM_NM"]).strip() if pd.notna(r.get("DGM_NM")) else ""
    typ = UQ_TYPES.get(str(r.get("ATRB_SE", "")), str(r.get("ATRB_SE", "")))
    m = re.search(r"NTC(\d{4})(\d{2})(\d{2})", str(r.get("NTFC_SN", "")))
    notice = f", 고시 {m.group(1)}.{m.group(2)}.{m.group(3)}" if m else ""
    return f"{nm or '(이름 없음)'} [{typ}{notice}]"


def _pick_name(gdf: gpd.GeoDataFrame) -> pd.Series:
    if "ATRB_SE" in gdf.columns and "DGM_NM" in gdf.columns:
        return gdf.apply(_uq181_name, axis=1)
    # 국토부 연속주제 정비구역(LSMD_CONT_UD602): REMARK에 정식 이름, ALIAS에 약칭이 들어 있음 (둘 중 하나만 있기도 함)
    if "REMARK" in gdf.columns and "ALIAS" in gdf.columns:
        def combo(r):
            rem = str(r["REMARK"]).strip() if pd.notna(r["REMARK"]) else ""
            ali = str(r["ALIAS"]).strip() if pd.notna(r["ALIAS"]) else ""
            if rem and ali and ali not in rem and rem not in ali and ali not in ("정비구역",):
                name = f"{rem} ({ali})"
            else:
                name = rem or ali
            # 도면번호(MNUM) 예: 6110000 11110 2009 0465 UDT… → 고시 연도·번호 추정
            m = re.match(r"\d{7}\d{5}(\d{4})(\d{4})", str(r.get("MNUM", "")))
            notice = f"고시 {m.group(1)}-{int(m.group(2))}" if m else ""
            ntf = str(r.get("NTFDATE", "")).strip()
            if ntf and ntf != "nan":
                notice = f"고시일 {ntf}" + (f", {notice}" if notice else "")
            if not name:
                return f"(이름 없음) {notice}".strip()
            return f"{name} [{notice}]" if notice else name
        return gdf.apply(combo, axis=1)
    cols = {c.lower(): c for c in gdf.columns}
    for cand in NAME_CANDIDATES:
        if cand.lower() in cols:
            return gdf[cols[cand.lower()]].astype(str)
    for c in gdf.columns:
        if c != "geometry" and ("NM" in c.upper() or "명" in c) and gdf[c].dtype == object:
            return gdf[c].astype(str)
    return pd.Series([""] * len(gdf), index=gdf.index)


def load_zones(zone_dir: Path, default_crs: str = "EPSG:5186",
               type_prefixes: str = DEFAULT_TYPE_PREFIXES) -> gpd.GeoDataFrame:
    prefixes = tuple(x.strip() for x in type_prefixes.split(",") if x.strip())
    files = sorted({p for pat in PATTERNS for p in zone_dir.rglob(pat)})
    if not files:
        raise FileNotFoundError(f"정비구역 파일이 없습니다: {zone_dir}")
    parts = []
    bbox = gpd.GeoSeries.from_xy([SEOUL_BBOX_4326[0], SEOUL_BBOX_4326[2]],
                                 [SEOUL_BBOX_4326[1], SEOUL_BBOX_4326[3]], crs="EPSG:4326")
    for f in files:
        gdf = _read(f, default_crs).to_crs(METRIC_CRS)
        b = bbox.to_crs(METRIC_CRS)
        gdf = gdf.cx[b.x.min():b.x.max(), b.y.min():b.y.max()]
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
        if "ATRB_SE" in gdf.columns and prefixes:  # 의제처리구역: 정비 관련 유형만
            before = len(gdf)
            gdf = gdf[gdf["ATRB_SE"].astype(str).str.startswith(prefixes)]
            print(f"  {f.name}: 유형 필터({','.join(prefixes)})로 {before}개 중 {len(gdf)}개 사용")
        if gdf.empty:
            continue
        attrs = gdf.drop(columns="geometry")
        out = gpd.GeoDataFrame({
            "구역명": _pick_name(gdf).values,
            "구역출처": f.stem,
            "구역속성": attrs.apply(
                lambda r: "; ".join(f"{k}={v}" for k, v in r.items() if pd.notna(v) and str(v).strip()), axis=1
            ).values,
        }, geometry=gdf.geometry.make_valid().values, crs=METRIC_CRS)
        parts.append(out)
        print(f"  구역 파일 {f.name}: 서울 범위 {len(out)}개")
    if not parts:
        raise ValueError("서울 범위 안에 있는 구역이 없습니다. 좌표계를 확인하세요.")
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=METRIC_CRS)
