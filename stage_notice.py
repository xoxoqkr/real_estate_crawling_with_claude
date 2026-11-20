"""구청 고시·공고로 zone_stage.csv의 재개발 사업 단계를 최신화한다.

서울시 「정비사업 추진현황」(stage_seoul.py)은 분기 자료라 늦다. 이 스크립트는 대상 구 구청 홈페이지의
고시공고 게시판에서 조합설립인가, 사업시행계획인가, 분양공고, 관리처분계획인가 같은 고시를 읽어
고시일을 확인일로 하는 행을 zone_stage.csv에 병합한다. stage.lookup은 같은 구역이면 확인일이 가장 늦은 행을 쓰므로
고시 행이 서울시 행보다 우선한다.

  python stage_notice.py                         # .env.normal의 DISTRICTS (구별 어댑터가 있는 구만), 키워드별 1쪽
  python stage_notice.py --pages 3               # 처음 한 번: 키워드별 3쪽까지 (예전 고시까지 채우기)
  python stage_notice.py --dry-run               # 저장하지 않고 결과만 출력
  python stage_notice.py --from-json a.json ...  # 브라우저로 저장한 게시판 묶음(JSON)에서 읽기 (네트워크 안 씀)

출처 정찰 결과 (2026-11-20)
  - 관악구 /site/gwanak/ex/bbsNew/List.do?typeCode=1 (고시공고). robots.txt는 Googlebot에만 /site/gwanak/ex/bbs/를 막고
    이 게시판(bbsNew)과 다른 로봇은 막지 않음. 목록은 POST + 세션 csrfToken, 상세는 View.do?not_ancmt_mgt_no= (GET).
    고시 행은 '공고일자'가 비어 있어 '등록일'을 고시일로 쓴다.
  - 광진구 /portal/bbs/B0000378 (고시공고/입법예고). robots.txt가 이 경로를 Allow, Crawl-delay 10 → 요청 간격 10초 이상.
    목록은 GET(searchCnd=0 제목, searchWrd), 상세는 view.do?notAncmtMgtNo=. 2025년 9월 이전 고시는 B0000003(robots 비허용)이라 읽지 않음.
  - 토지이음 고시정보(eum.go.kr)는 지형도면이 붙는 고시(정비구역 지정 등)만 있고 조합설립·사업시행·관리처분 인가가 빠져 쓰지 않음.
  - 저작권: 두 구청 모두 공공누리 기준(출처 표시). 자동 수집 금지 조항은 없음. 결과의 출처에 고시번호, 비고에 URL을 남긴다.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from lxml import html as lhtml

from stage import COLS, STAGES, normalize_stage
from stage_seoul import keyword_of

KEYWORDS = ["정비", "재개발", "재건축", "분양"]
USER_AGENT = "Mozilla/5.0 (compatible; zone-stage-notice/1.0; personal research, weekly)"
STAGE_ORDER = {name: i for i, (name, _) in enumerate(STAGES)}

# 제목 필터
RELATED = re.compile(r"정비|재개발|재건축|조합설립|추진위원회|사업시행|관리처분|분양|이주|착공|준공|이전고시")
# 단계가 아니라 절차 중간의 안내·행정 공고
EXCLUDE = re.compile(r"공람|열람|공청회|설명회|모집|선거|공시송달|재결|수용|환경영향|평가위원|안전점검|용역|입찰|채용|"
                     r"공원\s*조성|조성계획")
# 정비 핵심어가 없을 때만 거르는 말 (예: '신림7구역 … 정비구역 지정, 지구단위계획 결정'은 남김)
CORE = re.compile(r"정비구역|재개발|재건축|조합설립|추진위원회|사업시행|관리처분|분양|이전고시|준공")
EXCLUDE_IF_NO_CORE = re.compile(r"공원|도로|주차장|지구단위|특별건축|토지거래")
CANCEL = re.compile(r"취소|폐지|해산|실효|해제|직권")
SALE = re.compile(r"분양\s*(신청|공고)")
SALE_CONTEXT = re.compile(r"정비|재개발|재건축|구역|조합원")  # '도시텃밭 분양 공고' 같은 것 제외
# 신탁·모아타운 방식의 '사업시행자 지정'은 사업시행계획인가가 아니다. 단계 표에 없어 확인 대상으로 둔다
OPERATOR = re.compile(r"사업\s*시행자\s*(지정|고시)|시행자\s*지정")
CHANGE = re.compile(r"변경")
# 제목에서 구역명 뒤에 오는 사업 종류·절차 말
BIZ = re.compile(r"\s*(?:주택정비형|도시정비형|주택재개발|공공재개발|재개발|주택재건축|공공재건축|재건축|가로주택|소규모|자율주택|"
                 r"정비사업|정비계획|시장정비|조합설립|추진위원회|사업시행|관리처분|정비구역\s*지정|분양)")
_HEAD = r"서울특별시\s*(?:[가-힣]\s*){1,4}구\s*청\s*장"  # '광진구청장', '광 진 구 청 장'
NOTICE_DATE = [re.compile(r"고시\s*일자?\s*[:：]\s*(\d{4})\s*[.\-년]\s*(\d{1,2})\s*[.\-월]\s*(\d{1,2})"),
               re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일\.?\s*" + _HEAD),
               re.compile(r"(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})\s*\.?\s*" + _HEAD)]
# 본문의 「○○ 재건축정비사업 …」에서 구역명을 찾는다 (제목에 구역명이 없을 때). 법령 이름은 제외
BODY_NAME = re.compile(r"「([^」]{2,60}?(?:재건축|재개발|정비사업|가로주택)[^」]*)」")
RIGHTS_DATE = re.compile(r"권리\s*산정\s*기준일[^0-9]{0,30}(\d{4})\s*[.\-년]\s*(\d{1,2})\s*[.\-월]\s*(\d{1,2})")


@dataclass
class Notice:
    gu: str
    number: str          # 예: 고시 제2026-63호
    title: str
    date: str            # YYYY-MM-DD
    url: str
    detail_id: str = ""
    detail_text: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_gosi(self) -> bool:
        return bool(re.search(r"고시\s*제", self.number))

    @property
    def source(self) -> str:
        num = re.sub(r"^서울특별시\s*\S+구\s*", "", self.number).strip()
        return f"{self.gu} {num}"


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip() if el is not None else ""


def _date(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"(\d{4})[.\-]?(\d{2})[.\-]?(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _rows(doc):
    return doc.xpath("//table//tbody/tr")


# ---------------------------------------------------------------- 구별 어댑터
class Adapter:
    gu = ""
    base = ""
    delay = (3.0, 6.0)  # 쪽 사이 간격(초)

    def __init__(self, session=None):
        self.session = session

    # 네트워크
    def wait(self):
        time.sleep(random.uniform(*self.delay))

    def fetch_list(self, keyword: str, page: int) -> str:
        raise NotImplementedError

    def fetch_detail(self, notice: Notice) -> str:
        r = self.session.get(notice.url, timeout=30)
        r.raise_for_status()
        return r.text

    # 해석 (네트워크 없음, 테스트 대상)
    def parse_list(self, page_html: str) -> list[Notice]:
        raise NotImplementedError

    @staticmethod
    def parse_detail(page_html: str) -> str:
        doc = lhtml.fromstring(page_html)
        for bad in doc.xpath("//script|//style"):
            bad.drop_tree()
        return _text(doc)


class GwanakAdapter(Adapter):
    gu = "관악구"
    base = "https://www.gwanak.go.kr"
    LIST = "/site/gwanak/ex/bbsNew/List.do?typeCode=1"
    VIEW = "/site/gwanak/ex/bbsNew/View.do?typeCode=1&not_ancmt_mgt_no={id}"
    _csrf = ""

    def _token(self) -> str:
        if not self._csrf:
            r = self.session.get(self.base + self.LIST, timeout=30)
            r.raise_for_status()
            v = lhtml.fromstring(r.text).xpath("//form[@id='BbsNewFVo']//input[@name='csrfToken']/@value")
            if not v:
                raise RuntimeError("관악구 게시판 csrfToken을 찾지 못함 (게시판 구조 변경?)")
            self._csrf = v[0]
            self.wait()
        return self._csrf

    def fetch_list(self, keyword: str, page: int) -> str:
        data = {"pageIndex": page, "searchStartDate": "", "searchEndDate": "", "tgtTypeCd": "NOT_ANCMT_SJ",
                "searchKey": keyword, "pageUnit": 30, "csrfToken": self._token()}
        r = self.session.post(self.base + self.LIST, data=data, timeout=30)
        r.raise_for_status()
        return r.text

    def parse_list(self, page_html: str) -> list[Notice]:
        out = []
        for tr in _rows(lhtml.fromstring(page_html)):
            c = [_text(td) for td in tr.xpath("./td")]
            if len(c) < 7:
                continue
            onclick = " ".join(tr.xpath(".//a/@onclick"))
            m = re.search(r"'([^']+)'", onclick)
            nid = m.group(1) if m else ""
            # 열: 번호, 공고번호, 제목, 공고일자, 마감일자, 담당부서, 등록일, 조회수. 고시는 공고일자가 비어 있음
            out.append(Notice(gu=self.gu, number=c[1], title=c[2], date=_date(c[3]) or _date(c[6]),
                              url=self.base + self.VIEW.format(id=nid) if nid else "", detail_id=nid,
                              extra={"부서": c[5]}))
        return out


class GwangjinAdapter(Adapter):
    gu = "광진구"
    base = "https://www.gwangjin.go.kr"
    LIST = "/portal/bbs/B0000378/list.do"
    delay = (10.5, 13.0)  # robots.txt Crawl-delay: 10

    def fetch_list(self, keyword: str, page: int) -> str:
        q = urlencode({"menuNo": "200192", "pageIndex": page, "searchCnd": "0", "searchWrd": keyword})
        r = self.session.get(f"{self.base}{self.LIST}?{q}", timeout=30)
        r.raise_for_status()
        return r.text

    def parse_list(self, page_html: str) -> list[Notice]:
        out = []
        for tr in _rows(lhtml.fromstring(page_html)):
            c = [_text(td) for td in tr.xpath("./td")]
            if len(c) < 6:
                continue
            href = (tr.xpath(".//a/@href") or [""])[0]
            m = re.search(r"notAncmtMgtNo=(\d+)", href)
            nid = m.group(1) if m else ""
            url = f"{self.base}/portal/bbs/B0000378/view.do?notAncmtMgtNo={nid}&menuNo=200192" if nid else ""
            # 열: 번호, 공고번호, 진행사항, 공고명, 담당부서, 공고시작일, 공고종료일
            out.append(Notice(gu=self.gu, number=c[1], title=c[3], date=_date(c[5]), url=url, detail_id=nid,
                              extra={"부서": c[4]}))
        return out


ADAPTERS = {a.gu: a for a in (GwanakAdapter, GwangjinAdapter)}


# ---------------------------------------------------------------- 공통 해석
def clean_title(title: str) -> str:
    t = re.sub(r"[「」『』\"“”]", " ", title or "")
    return re.sub(r"\s+", " ", t).strip()


def stage_text(title: str) -> str:
    """normalize_stage에 넘길 문장. 이름 안의 말이 단계로 잘못 잡히지 않게 다듬는다."""
    t = clean_title(title)
    t = t.replace("조합설립추진위원회", "추진위원회")
    t = re.sub(r"재정비촉진(구역|지구)", "", t)  # '정비구역'으로 잡히지 않게
    return t


def zone_name(title: str, gu: str = "") -> str:
    """제목에서 구역명(사업 종류 앞부분)을 뽑는다. 앞부분이 비면 괄호 안을 본다."""
    t = clean_title(title)
    t = re.sub(r"^(서울특별시\s*)?(관악구|광진구)?\s*", "", t)
    t = re.sub(r"^\(가칭\)\s*", "", t)
    for part in [t] + re.findall(r"\(([^)]*)\)", t):
        m = BIZ.search(part)
        name = (part[:m.start()] if m else "").strip(" ,·-")
        name = re.sub(r"\s*(일대|일원)$", "", name)
        if len(re.sub(r"\s", "", name)) >= 2:
            return name
    return ""


def zone_keyword(name: str) -> str:
    kw = keyword_of(name)
    kw = re.sub(r"(?<=[가-힣])제(?=\d)", "", kw)  # 봉천제4-1-3 → 봉천4-1-3 (서울시 자료 표기와 맞춤)
    kw = re.sub(r"[,·ㆍ.]", "", kw)  # 뉴서울아파트, 개나리·열망연립 → 뉴서울아파트개나리열망연립
    return kw


def business_type(title: str) -> str:
    for pat, name in [(r"가로주택", "가로주택정비사업"), (r"소규모\s*재건축", "소규모재건축"), (r"소규모\s*재개발", "소규모재개발"),
                      (r"자율주택", "자율주택정비사업"), (r"시장정비", "시장정비사업"), (r"재건축", "재건축"),
                      (r"도시정비형", "도시정비형 재개발"), (r"재개발|재정비촉진", "주택정비형 재개발")]:
        if re.search(pat, title):
            return name
    return ""


def rights_date(text: str) -> str:
    m = RIGHTS_DATE.search(text or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""


def notice_date(text: str) -> str:
    """상세 본문의 고시일(예: '고시일자: 2026. 6. 26.', '2026년 10월 26일 서울특별시 광진구청장')."""
    for pat in NOTICE_DATE:
        m = pat.search(text or "")
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def classify(n: Notice) -> tuple[str, dict]:
    """(결과, 정보). 결과: 대상, 무관, 확인필요, 해석불가."""
    t = clean_title(n.title)
    if not RELATED.search(t) or EXCLUDE.search(t) or (EXCLUDE_IF_NO_CORE.search(t) and not CORE.search(t)):
        return "무관", {}
    sale = bool(SALE.search(t) and SALE_CONTEXT.search(t))
    if not n.is_gosi and not sale:  # 공고는 정비사업 분양신청 공고만 단계 정보로 쓴다
        return "무관", {}
    if SALE.search(t) and not sale:
        return "무관", {}
    if CANCEL.search(t):
        name = zone_name(t, n.gu)
        return "확인필요", {"사유": "취소·폐지·해산·실효·해제 고시", "취소": True,
                          "구역키워드": zone_keyword(name) if name else ""}
    if OPERATOR.search(t):
        return "확인필요", {"사유": "사업시행자 지정(신탁·모아타운 등). 사업시행계획인가가 아님"}
    stage = "분양신청" if sale else normalize_stage(stage_text(t))
    if not stage:
        return "해석불가", {"사유": "단계를 못 찾거나 여러 단계가 섞임"}
    name = zone_name(t, n.gu)
    if not name:  # 제목에 구역명이 없으면 본문 「…」에서
        for m in BODY_NAME.finditer(n.detail_text or ""):
            if "법" not in m.group(1)[-3:] and "조례" not in m.group(1):
                name = zone_name(m.group(1), n.gu)
                if name:
                    break
    kw = zone_keyword(name) if name else ""
    if len(kw) < 2:
        return "해석불가", {"사유": "제목에서 구역명을 못 뽑음", "단계": stage}
    if not n.date:
        return "해석불가", {"사유": "고시일 없음", "단계": stage}
    return "대상", {"단계": stage, "구역명": name, "구역키워드": kw, "변경": bool(CHANGE.search(t)),
                  "사업유형": business_type(t)}


# ---------------------------------------------------------------- 병합
def read_table(csv_path: Path) -> list[dict]:
    if not Path(csv_path).exists():
        return []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_table(csv_path: Path, rows: list[dict]):
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def merge(notices: list[Notice], csv_path: Path, dry_run: bool = False, log=print) -> dict:
    """고시를 zone_stage.csv에 병합하고 구별 집계를 돌려준다.

    병합 키는 (자치구, 구역키워드, 출처). 같은 키는 고시일이 더 늦을 때만 갱신한다.
    서울시 자료 행과 사람이 적은 행(다른 출처)은 건드리지 않는다.
    같은 구역에 이미 더 높은 단계가 기록돼 있으면(변경 고시가 단계를 되돌리는 경우 등) 쓰지 않고 '확인 필요'로 남긴다.
    """
    rows = read_table(csv_path)
    index = {((r.get("자치구") or "").strip(), (r.get("구역키워드") or "").strip(), (r.get("출처") or "").strip()): i
             for i, r in enumerate(rows)}
    stats: dict[str, dict] = {}
    # 같은 구역에 더 늦은 취소·폐지 고시가 있으면 그 앞의 고시는 쓰지 않는다 (예: 조합설립인가 → 조합설립인가 취소)
    cancelled: dict[tuple, str] = {}
    for n in notices:
        r, info = classify(n)
        if info.get("취소") and info.get("구역키워드"):
            k = (n.gu, info["구역키워드"])
            cancelled[k] = max(cancelled.get(k, ""), n.date)
    for n in notices:
        st = stats.setdefault(n.gu, {"읽음": 0, "추가": 0, "갱신": 0, "건너뜀": 0, "해석불가": 0, "확인필요": 0, "무관": 0})
        st["읽음"] += 1
        result, info = classify(n)
        if result == "무관":
            st["무관"] += 1
            continue
        if result != "대상":
            st[result] += 1
            log(f"  [{n.gu}] {result}: {n.number} {n.title} ({info.get('사유', '')}) {n.url}")
            continue
        kw, stage, gu = info["구역키워드"], info["단계"], n.gu
        if n.date <= cancelled.get((gu, kw), ""):
            st["확인필요"] += 1
            log(f"  [{gu}] 확인필요: {n.number} {n.title} → 이후 취소·폐지 고시({cancelled[(gu, kw)]})가 있어 쓰지 않음")
            continue
        same_zone = [r for r in rows if (r.get("자치구") or "").strip() == gu and (r.get("구역키워드") or "").strip() == kw]
        known = [normalize_stage(r.get("단계")) for r in same_zone]
        top = max((STAGE_ORDER[s] for s in known if s), default=-1)
        if STAGE_ORDER[stage] < top:
            st["확인필요"] += 1
            log(f"  [{gu}] 확인필요: {n.number} {n.title} → '{stage}'이지만 '{kw}'에 더 높은 단계가 이미 있어 쓰지 않음")
            continue
        kind = next((r.get("사업유형") for r in same_zone if (r.get("사업유형") or "").strip()), "") or info["사업유형"]
        notes = []
        if info["변경"]:
            notes.append("변경")
        if "정정" in n.title:
            notes.append("정정")
        rd = rights_date(n.detail_text)
        if rd:
            notes.append(f"권리산정기준일 {rd}")
        notes += [f"원 제목 {clean_title(n.title)}", f"URL {n.url}" if n.url else ""]
        date = notice_date(n.detail_text) or n.date  # 본문 고시일이 있으면 그것, 없으면 게시일
        new = {"구역키워드": kw, "자치구": gu, "단계": stage, "확인일": date, "출처": n.source, "사업유형": kind,
               "비고": "; ".join(x for x in notes if x)}
        key = (gu, kw, n.source)
        if key in index:
            if date > (rows[index[key]].get("확인일") or ""):
                rows[index[key]] = new
                st["갱신"] += 1
            else:
                st["건너뜀"] += 1
        else:
            index[key] = len(rows)
            rows.append(new)
            st["추가"] += 1
    if not dry_run:
        write_table(csv_path, rows)
    return stats


# ---------------------------------------------------------------- 실행
def needs_detail(n: Notice) -> bool:
    r, _ = classify(n)
    return r == "대상" and bool(n.url)


def collect_online(gu: str, keywords: list[str], pages: int, log=print) -> list[Notice]:
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    ad = ADAPTERS[gu](s)
    seen, out = set(), []
    for kw in keywords:
        for p in range(1, pages + 1):
            items = ad.parse_list(ad.fetch_list(kw, p))
            ad.wait()
            for n in items:
                if n.detail_id and n.detail_id not in seen:
                    seen.add(n.detail_id)
                    out.append(n)
            if not items:
                break
    for n in out:
        if needs_detail(n):
            try:
                n.detail_text = ad.parse_detail(ad.fetch_detail(n))
            except Exception as e:  # noqa: BLE001 - 상세를 못 읽어도 목록 정보로 병합한다
                log(f"  [{gu}] 상세 읽기 실패: {n.number} ({e})")
            ad.wait()
    return out


def collect_from_json(path: Path) -> list[Notice]:
    """브라우저에서 저장한 묶음: {"gu": .., "pages": [{"html": ..}], "details": {id: html}}"""
    b = json.loads(Path(path).read_text(encoding="utf-8"))
    ad = ADAPTERS[b["gu"]]()
    seen, out = set(), []
    for pg in b.get("pages", []):
        for n in ad.parse_list(pg["html"]):
            if n.detail_id not in seen:
                seen.add(n.detail_id)
                out.append(n)
    for n in out:
        if n.detail_id in b.get("details", {}):
            n.detail_text = ad.parse_detail(b["details"][n.detail_id])
    return out


def main():
    from match_auctions import load_config
    ap = argparse.ArgumentParser(description="구청 고시로 zone_stage.csv의 사업 단계 최신화")
    ap.add_argument("--districts", help="쉼표로 구분한 구 (기본: DISTRICTS)")
    ap.add_argument("--pages", type=int, default=1, help="키워드별로 읽을 쪽 수 (기본 1, 처음에는 3 정도)")
    ap.add_argument("--keywords", default=",".join(KEYWORDS), help="제목 검색어 (쉼표 구분)")
    ap.add_argument("--from-json", nargs="*", help="브라우저로 저장한 게시판 묶음 JSON (네트워크 안 씀)")
    ap.add_argument("--dry-run", action="store_true", help="zone_stage.csv에 쓰지 않음")
    args = ap.parse_args()
    cfg = load_config()
    csv_path = cfg["stage_file"]

    notices: list[Notice] = []
    failed = {}
    if args.from_json:
        for p in args.from_json:
            notices += collect_from_json(Path(p))
    else:
        districts = ([x.strip() for x in args.districts.split(",") if x.strip()] if args.districts
                     else cfg["districts"])
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
        targets = [g for g in districts if g in ADAPTERS]
        skipped = [g for g in districts if g not in ADAPTERS]
        if skipped:
            print(f"어댑터가 없어 건너뜀: {', '.join(skipped)} (구청마다 게시판 형식이 달라 구별로 추가해야 함)")
        for i, gu in enumerate(targets):
            if i:
                time.sleep(random.uniform(6, 10))
            try:
                got = collect_online(gu, keywords, args.pages)
                print(f"[{gu}] 게시물 {len(got)}건 읽음")
                notices += got
            except Exception as e:  # noqa: BLE001 - 한 구가 깨져도 다른 구는 계속
                failed[gu] = str(e)
                print(f"[{gu}] 실패: {e}")

    stats = merge(notices, csv_path, dry_run=args.dry_run)
    print(f"{'(저장 안 함) ' if args.dry_run else ''}{csv_path}")
    for gu, st in stats.items():
        print(f"  {gu}: " + ", ".join(f"{k} {v}" for k, v in st.items()))
    for gu, e in failed.items():
        print(f"  {gu}: 실패 ({e})")
    return 1 if failed and not stats else 0


if __name__ == "__main__":
    sys.exit(main())
