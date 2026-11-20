"""스피드옥션 엑셀 → 좌표 → 정비구역 매칭 → 결과 엑셀.

사용법
  python match_auctions.py            # input 폴더의 파일을 한 번 처리
  python match_auctions.py --watch    # input 폴더를 지켜보다가 새 엑셀이 들어오면 자동 처리
  python match_auctions.py --dry-run  # 지오코딩 없이 엑셀 해석 결과만 확인
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, shape

from geocode import VWorld
from speedauction_excel import load_items
from zones import METRIC_CRS, load_zones

HERE = Path(__file__).resolve().parent
INPUT_EXT = {".xls", ".xlsx", ".html", ".htm"}
PYEONG = 3.305785

CHECKLIST = ("① 구역 단계·고시일·권리산정기준일(정비사업 정보몽땅, 구청) "
             "② 집합건물 보존등기일이 기준일보다 앞서는지 "
             "③ 경매신청 채권자가 금융기관인지(조합원 지위 양도 제한 예외) "
             "④ 같은 구역 낙찰가율·대지지분 평당가 비교")


def load_config() -> dict:
    try:
        from dotenv import load_dotenv
        load_dotenv(HERE / ".env.credential")  # 비밀 값 (커밋 안 함)
        load_dotenv(HERE / ".env.normal")      # 일반 설정
    except ImportError:
        pass
    base = Path(os.getenv("WORK_DIR", HERE))
    if not base.is_absolute():  # 상대 경로는 실행 위치가 아니라 이 스크립트 폴더 기준
        base = HERE / base
    return {
        "key": os.getenv("VWORLD_KEY", ""),
        "domain": os.getenv("VWORLD_DOMAIN", ""),
        "input": Path(os.getenv("INPUT_DIR", base / "input")),
        "zones": Path(os.getenv("ZONE_DIR", base / "zones")),
        "output": Path(os.getenv("OUTPUT_DIR", base / "output")),
        "state": Path(os.getenv("STATE_DIR", base / "state")),
        "districts": [d.strip() for d in os.getenv("DISTRICTS", "").split(",") if d.strip()],
        "near_m": float(os.getenv("NEAR_METERS", "50")),
        "use_parcel": os.getenv("USE_PARCEL_POLYGON", "1") == "1",
        "archive": os.getenv("ARCHIVE_INPUT", "1") == "1",
        "zone_crs": os.getenv("ZONE_DEFAULT_CRS", "EPSG:5186"),
        "zone_types": os.getenv("ZONE_TYPE_PREFIXES", "UQ12,UQ18,UQ51,UQ64"),
    }


def input_files(d: Path) -> list[Path]:
    return sorted(p for p in d.glob("*") if p.is_file() and p.suffix.lower() in INPUT_EXT
                  and not p.name.startswith("~$"))


def collect_items(files: list[Path], districts: list[str]) -> pd.DataFrame:
    rows = []
    for f in files:
        try:
            items = load_items(f)
            print(f"  {f.name}: {len(items)}건")
            rows += items
        except Exception as e:  # noqa: BLE001
            print(f"  {f.name}: 읽기 실패 ({e})")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.drop_duplicates("key", keep="last")
    if districts:
        df = df[df["구"].isin(districts)]
    return df.reset_index(drop=True)


def geocode_items(df: pd.DataFrame, vw: VWorld, use_parcel: bool) -> gpd.GeoDataFrame:
    geoms, lons, lats, refined, pnus = [], [], [], [], []
    for i, r in df.iterrows():
        g = vw.geocode(r["지오코딩주소"], r["주소종류"])
        if not g:
            geoms.append(None); lons.append(None); lats.append(None); refined.append(""); pnus.append("")
            continue
        geom = Point(g["lon"], g["lat"])
        pnu = ""
        if use_parcel:
            p = vw.parcel_polygon(g["lon"], g["lat"])
            if p:
                geom, pnu = shape(p["geometry"]), p.get("pnu", "")
        geoms.append(geom); lons.append(g["lon"]); lats.append(g["lat"])
        refined.append(g.get("refined", "")); pnus.append(pnu)
        if (i + 1) % 20 == 0:
            vw.save()
            print(f"    좌표 변환 {i + 1}/{len(df)}")
    vw.save()
    df = df.assign(경도=lons, 위도=lats, 정제주소=refined, PNU=pnus)
    return gpd.GeoDataFrame(df, geometry=geoms, crs="EPSG:4326")


def match(items: gpd.GeoDataFrame, zones: gpd.GeoDataFrame, near_m: float) -> pd.DataFrame:
    ok = items[items.geometry.notna()].to_crs(METRIC_CRS)
    bad = pd.DataFrame(items[items.geometry.isna()].drop(columns="geometry"))

    hit = gpd.sjoin(ok[["key", "geometry"]], zones, how="inner", predicate="intersects")
    if not hit.empty:
        # 필지 면적 중 구역 안 비율 (필지 경계를 쓴 경우)
        zg = zones.geometry
        hit["겹침%"] = [
            round(g.intersection(zg.loc[z]).area / g.area * 100) if g.area > 0 else 100
            for g, z in zip(hit.geometry, hit["index_right"])
        ]
        agg = hit.groupby("key").agg({
            "구역명": lambda s: " / ".join(dict.fromkeys(x for x in s if x)),
            "구역출처": lambda s: " / ".join(dict.fromkeys(s)),
            "구역속성": lambda s: " || ".join(dict.fromkeys(s))[:1000],
            "겹침%": "max",
        }).reset_index()
    else:
        agg = pd.DataFrame(columns=["key", "구역명", "구역출처", "구역속성", "겹침%"])

    rest = ok[~ok["key"].isin(agg["key"])]
    near = pd.DataFrame(columns=["key", "구역명", "구역출처", "구역속성", "경계거리m"])
    if not rest.empty and near_m > 0:
        nj = gpd.sjoin_nearest(rest[["key", "geometry"]], zones, how="inner",
                               max_distance=near_m, distance_col="경계거리m")
        if not nj.empty:
            nj = nj.sort_values("경계거리m").drop_duplicates("key")
            near = nj[["key", "구역명", "구역출처", "구역속성", "경계거리m"]].copy()
            near["경계거리m"] = near["경계거리m"].round(1)

    out = pd.DataFrame(ok.drop(columns="geometry"))
    out = out.merge(agg, on="key", how="left").merge(near, on="key", how="left", suffixes=("", "_근접"))
    for c in ("구역명", "구역출처", "구역속성"):
        out[c] = out[c].fillna(out.get(c + "_근접"))
        out = out.drop(columns=[c + "_근접"], errors="ignore")
    out["판정"] = "해당없음"
    out.loc[out["경계거리m"].notna(), "판정"] = f"경계근접(<{near_m:g}m)"
    out.loc[out["겹침%"].notna(), "판정"] = "구역안"
    out.loc[(out["판정"] == "구역안") & (out["겹침%"] < 50), "판정"] = "구역걸침"
    if not bad.empty:
        bad["판정"] = "좌표실패"
        out = pd.concat([out, bad], ignore_index=True)
    return out


def finish(df: pd.DataFrame, state_dir: Path) -> pd.DataFrame:
    seen_path = state_dir / "seen.json"
    seen = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {}
    today = datetime.now().strftime("%Y-%m-%d")
    df["신규"] = ["신규" if k not in seen else "" for k in df["key"]]
    df["최초확인일"] = [seen.get(k, today) for k in df["key"]]
    for k in df["key"]:
        seen.setdefault(k, today)
    state_dir.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")

    # 구역명에 붙은 고시 연도 중 가장 최근 값. 오래된 값만 있으면 이미 끝난 사업지일 수 있음
    df["최근고시연도"] = df["구역명"].fillna("").astype(str).str.findall(r"고시(?:일)? ?(\d{4})").apply(
        lambda ys: max(int(y) for y in ys) if ys else None)
    pyeong = df["대지권㎡"] / PYEONG
    df["대지권평"] = pyeong.round(2)
    df["대지평당최저가(만원)"] = (df["최저가"] / pyeong / 10_000).round(0)
    df["확인할것"] = ""
    df.loc[df["판정"].isin(["구역안", "구역걸침"]) | df["판정"].str.startswith("경계"), "확인할것"] = CHECKLIST
    order = {"구역안": 0, "구역걸침": 1}
    df["_o"] = df["판정"].map(lambda x: order.get(x, 2 if x.startswith("경계") else 3 if x == "해당없음" else 4))
    df = df.sort_values(["_o", "구", "매각기일"]).drop(columns="_o")
    cols = ["판정", "신규", "구역명", "최근고시연도", "겹침%", "경계거리m", "구", "법원", "경매계", "사건번호", "물번", "소재지", "용도",
            "대지권㎡", "대지권평", "전용㎡", "감정가", "최저가", "최저가율%", "대지평당최저가(만원)", "매각기일",
            "결과", "유찰수", "특수조건", "확인할것", "상세링크", "구역출처", "구역속성", "정제주소", "PNU",
            "경도", "위도", "지오코딩주소", "최초확인일", "원본파일", "key"]
    return df[[c for c in cols if c in df.columns]]


def write_excel(df: pd.DataFrame, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"재개발매칭_{datetime.now():%Y%m%d_%H%M}.xlsx"
    sheets = {
        "구역안·걸침": df[df["판정"].isin(["구역안", "구역걸침"])],
        "경계근접": df[df["판정"].str.startswith("경계")],
        "전체": df,
        "좌표실패": df[df["판정"] == "좌표실패"],
    }
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, d in sheets.items():
            d.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "D2"
            ws.auto_filter.ref = ws.dimensions
            headers = [c.value for c in ws[1]]
            widths = {"구역명": 22, "소재지": 40, "특수조건": 30, "확인할것": 40, "상세링크": 12, "구역속성": 40}
            for i, h in enumerate(headers, start=1):
                ws.column_dimensions[ws.cell(1, i).column_letter].width = widths.get(h, 11)
            if "상세링크" in headers:
                ci = headers.index("상세링크") + 1
                for r in range(2, ws.max_row + 1):
                    c = ws.cell(r, ci)
                    if c.value:
                        c.hyperlink, c.value, c.style = c.value, "열기", "Hyperlink"
            for h in ("감정가", "최저가"):
                if h in headers:
                    ci = headers.index(h) + 1
                    for r in range(2, ws.max_row + 1):
                        ws.cell(r, ci).number_format = "#,##0"
    return path


def run_once(cfg: dict, dry_run: bool = False) -> Path | None:
    cfg["input"].mkdir(parents=True, exist_ok=True)
    files = input_files(cfg["input"])
    if not files:
        print(f"입력 파일이 없습니다: {cfg['input']}")
        return None
    print(f"[{datetime.now():%H:%M:%S}] 엑셀 {len(files)}개 읽는 중")
    items = collect_items(files, cfg["districts"])
    print(f"  대상 물건 {len(items)}건 (구 필터: {', '.join(cfg['districts']) or '없음'})")
    if items.empty:
        return None
    if dry_run:
        print(items[["구", "사건번호", "물번", "지오코딩주소", "용도", "감정가", "최저가", "매각기일", "결과"]].to_string())
        return None

    zones = load_zones(cfg["zones"], cfg["zone_crs"], cfg["zone_types"])
    vw = VWorld(cfg["key"], cfg["state"] / "geocode_cache.json", cfg["domain"])
    gdf = geocode_items(items, vw, cfg["use_parcel"])
    result = finish(match(gdf, zones, cfg["near_m"]), cfg["state"])
    path = write_excel(result, cfg["output"])
    counts = result["판정"].value_counts().to_dict()
    print(f"  결과: {counts}")
    print(f"  저장: {path}")

    if cfg["archive"]:
        dest = cfg["input"] / "처리완료" / datetime.now().strftime("%Y%m%d_%H%M")
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.move(str(f), dest / f.name)
    return path


def watch(cfg: dict, interval: int = 30):
    print(f"입력 폴더를 지켜봅니다: {cfg['input']} (종료: Ctrl+C)")
    last = None
    while True:
        files = input_files(cfg["input"])
        sig = tuple((f.name, f.stat().st_size, f.stat().st_mtime) for f in files)
        if files and sig == last:  # 두 번 연속 같으면 다운로드가 끝난 것으로 보고 처리
            try:
                run_once(cfg)
            except Exception as e:  # noqa: BLE001
                print(f"  처리 중 오류: {e}")
            last = None
        else:
            last = sig
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="스피드옥션 엑셀과 정비구역 매칭")
    ap.add_argument("--watch", action="store_true", help="입력 폴더를 지켜보며 자동 처리")
    ap.add_argument("--dry-run", action="store_true", help="엑셀 해석 결과만 출력")
    ap.add_argument("--districts", help="쉼표로 구분한 구 이름 (.env의 DISTRICTS보다 우선)")
    args = ap.parse_args()
    cfg = load_config()
    if args.districts is not None:
        cfg["districts"] = [d.strip() for d in args.districts.split(",") if d.strip()]
    if args.watch:
        watch(cfg)
    else:
        run_once(cfg, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
