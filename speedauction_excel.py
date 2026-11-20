"""스피드옥션 검색결과 '엑셀' 저장 파일을 읽어 물건 목록으로 바꿉니다.

스피드옥션의 엑셀 저장은 화면의 결과표(.tbl_result)를 그대로 내보냅니다.
파일이 진짜 엑셀(xls/xlsx)이든 HTML 표를 .xls로 저장한 것이든 모두 읽도록 했습니다.
열이 합쳐져 있거나 나뉘어 있어도 되도록, 행 전체 글자를 정규식으로 다시 해석합니다.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from urllib.parse import quote

import pandas as pd

# 종합검색 화면의 법원 선택 목록에서 확인 (2026-11-19)
COURT_CODES = {
    "서울중앙": "A01",
    "서울동부": "A02",
    "서울서부": "A03",
    "서울남부": "A04",
    "서울북부": "A05",
}

DETAIL_URL = "https://www.speedauction.co.kr/pop/open/!/view/product?token={token}"

RESULT_WORDS = ("신건", "유찰", "재진행", "재매각", "낙찰", "매각", "취하", "변경", "정지", "기각", "취소", "대금미납")


# ---------------------------------------------------------------- 파일 읽기
def _read_any(path: Path) -> list[pd.DataFrame]:
    raw = path.read_bytes()
    head = raw[:2048].lstrip().lower()
    tables: list[pd.DataFrame] = []
    if head.startswith(b"<") or b"<table" in head or b"<html" in head:
        for enc in ("utf-8", "cp949"):
            try:
                text = raw.decode(enc)
                tables = pd.read_html(io.StringIO(text), header=None)
                break
            except (UnicodeDecodeError, ValueError):
                continue
    if not tables:
        tables = [pd.read_excel(path, header=None, dtype=str)]
    return tables


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def read_rows(path: Path) -> list[dict]:
    """파일에서 머리글(소재지가 들어간 행) 아래 행들을 {머리글: 값} 목록으로 돌려줍니다."""
    for df in _read_any(path):
        df = df.astype(object)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [" ".join(_cell(c) for c in col) for col in df.columns]
        # 머리글이 열 이름으로 이미 들어간 경우
        cols = [_cell(c) for c in df.columns]
        header_idx = -1
        if any("소재지" in c for c in cols):
            header = cols
        else:
            header = None
            for i in range(min(len(df), 30)):
                vals = [_cell(v) for v in df.iloc[i].tolist()]
                if any("소재지" in v for v in vals):
                    header, header_idx = vals, i
                    break
        if header is None:
            continue
        rows = []
        for i in range(header_idx + 1, len(df)):
            vals = [_cell(v) for v in df.iloc[i].tolist()]
            if sum(1 for v in vals if v) < 3:
                continue
            row = {}
            for h, v in zip(header, vals):
                key = h or f"col{len(row)}"
                row[key] = (row.get(key, "") + " " + v).strip() if key in row else v
            rows.append(row)
        if rows:
            return rows
    raise ValueError(f"'소재지' 머리글이 있는 표를 찾지 못했습니다: {path.name}")


# ---------------------------------------------------------------- 행 해석
_NUM = re.compile(r"\d{1,3}(?:,\d{3})+|\d{4,}")


def _col(row: dict, *keywords: str) -> str:
    return " ".join(v for k, v in row.items() if any(w in k for w in keywords))


def _money_list(text: str) -> list[int]:
    out = []
    for m in _NUM.finditer(text):
        s = m.group(0)
        if re.fullmatch(r"\d{4}", s):  # 연도 같은 4자리 숫자는 제외
            continue
        out.append(int(s.replace(",", "")))
    return out


def _area(text: str, label: str) -> float | None:
    m = re.search(label + r"\s*([\d.,]+)\s*(?:m²|㎡|m2)", text)
    return float(m.group(1).replace(",", "")) if m else None


def split_address(addr: str) -> tuple[str, str]:
    """소재지에서 지오코딩에 쓸 주소(지번 또는 도로명까지)와 주소 종류를 뽑습니다.

    '서울특별시 광진구 구의동 201-11 다솜빌 4층401호' -> ('서울특별시 광진구 구의동 201-11', 'parcel')
    """
    addr = re.sub(r"\(.*?\)", " ", addr)
    tokens = addr.split()
    for i, tok in enumerate(tokens):
        if i < 2:
            continue
        t = tok.replace("번지", "")
        prev = tokens[i - 1]
        if re.fullmatch(r"산?\d+(-\d+)?", t) or (prev == "산" and re.fullmatch(r"\d+(-\d+)?", t)):
            kind = "road" if re.search(r"(로|길)\d*(번길)?$", prev) else "parcel"
            if prev == "산":  # '산 12-3' → '산12-3'
                base = tokens[:i - 1] + ["산" + t]
            else:
                base = tokens[:i] + [t]
            return " ".join(base), kind
    return " ".join(tokens[:4]), "parcel"


def parse_row(row: dict) -> dict | None:
    whole = " ".join(row.values())
    loc = _col(row, "소재지") or whole

    court_m = re.search(r"(서울(?:중앙|동부|서부|남부|북부))(?:지법|지방법원)?\s*(?:(\d+)\s*계)?", whole)
    case_m = re.search(r"(20\d{2})\s*-\s*(\d+)", _col(row, "사건") or whole)
    if not (court_m and case_m):
        return None
    item_m = re.search(r"물번\s*\[?\s*(\d+)", whole)
    court = court_m.group(1)
    year, case = case_m.group(1), case_m.group(2)
    item = item_m.group(1) if item_m else "1"

    addr_m = re.search(r"(서울(?:특별시)?\s+\S+구\s+.+?)(?=\s*(?:대지권|토지\s*[\d.]|건물\s*[\d.]|전용|제시외|\[)|$)", loc)
    address = addr_m.group(1).strip() if addr_m else loc
    geo_addr, addr_kind = split_address(address)
    gu_m = re.search(r"서울(?:특별시)?\s+(\S+구)", address)

    tags = []
    for t in re.findall(r"\[([^\]]+)\]", loc):
        if re.fullmatch(r"\d+", t.strip()):
            continue
        tags += [x.strip() for x in t.split(",") if x.strip()]

    price_text = _col(row, "감정", "최저", "낙찰") or whole
    prices = _money_list(price_text)
    appraisal = prices[0] if prices else None
    min_price = prices[1] if len(prices) > 1 else None
    sold_price = prices[2] if len(prices) > 2 else None

    date_m = re.search(r"(20\d{2}-\d{2}-\d{2})", _col(row, "매각기일", "기일") or whole)
    res_text = _col(row, "결과", "유찰") or whole
    result = next((w for w in RESULT_WORDS if w in res_text), "")
    fail_m = re.search(r"(\d+)\s*회", res_text)
    pct_m = re.search(r"\((\d+)%\)", res_text)

    use = _col(row, "용도").replace(" ", "")

    land = _area(loc, "대지권") or _area(loc, "토지")
    excl = _area(loc, "전용") or _area(loc, "건물")
    code = COURT_CODES.get(court, "")
    token = f"{code}|{year}|{case}|{item}" if code else ""

    return {
        "key": f"{court}|{year}|{case}|{item}",
        "법원": court,
        "경매계": court_m.group(2) or "",
        "사건번호": f"{year}타경{case}",
        "물번": int(item),
        "구": gu_m.group(1) if gu_m else "",
        "소재지": address,
        "지오코딩주소": geo_addr,
        "주소종류": addr_kind,
        "용도": use,
        "대지권㎡": land,
        "전용㎡": excl,
        "감정가": appraisal,
        "최저가": min_price,
        "낙찰가": sold_price,
        "최저가율%": int(pct_m.group(1)) if pct_m else (round(min_price / appraisal * 100) if appraisal and min_price else None),
        "매각기일": date_m.group(1) if date_m else "",
        "결과": result,
        "유찰수": int(fail_m.group(1)) if fail_m else None,
        "특수조건": ", ".join(tags),
        "상세링크": DETAIL_URL.format(token=quote(token, safe="")) if token else "",
    }


def load_items(path: Path) -> list[dict]:
    items = []
    for row in read_rows(path):
        it = parse_row(row)
        if it:
            it["원본파일"] = path.name
            items.append(it)
    return items
