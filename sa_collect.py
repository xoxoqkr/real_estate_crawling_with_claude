"""스피드옥션 종합검색 결과를 구별로 자동 수집해 input 폴더에 저장하고, 이어서 정비구역 매칭을 실행합니다.

동작
  1. Playwright로 Chrome을 띄우고(로그인 세션은 state/browser_profile에 유지) 로그인 상태를 확인
     - 로그아웃 상태면 .env.credential의 SA_ID/SA_PW로 로그인. 비어 있으면 열린 창에서 직접 로그인할 때까지 기다림
  2. 종합검색 화면에서 소재지(서울 / 구)를 고르고, 화면이 쓰는 검색 요청
     (POST /ajax?post&ajax.id=search@auction_result.ajax)을 150건씩 페이지마다 보냄
  3. 결과표를 단순한 HTML 표로 바꿔 input\\speed_<구>_<시각>.html로 저장 → match_auctions.run_once 실행
  4. 정비구역 관련 물건을 관심경매물건 폴더(기본 '박태준')에 메모와 함께 저장 (sa_interest.py)

사용법
  python sa_collect.py --headed       # 처음 한 번: 창을 보면서 실행 (로그인 확인)
  python sa_collect.py                # 이후: 창 없이 수집 + 매칭
  python sa_collect.py --collect-only # 수집만
  python sa_collect.py --districts 광진구,관악구
"""
from __future__ import annotations

import argparse
import contextlib
import html
import os
import random
import sys
import time
from datetime import datetime

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from match_auctions import load_config, run_once

BASE = "https://www.speedauction.co.kr"
SEARCH_URL = BASE + "/search/sub/!/auction/01"
LOGIN_URL = BASE + "/cscenter/sub/!/members/05"
RESULT_AJAX = "/ajax?post&ajax.id=search@auction_result.ajax"
SEOUL_CODE = "1100000000"
HEADER = ["법원명 사건번호 물건번호", "소재지", "용도", "감정가 최저가 [낙찰가]",
          "매각기일 (개찰기일) [입찰인원]", "결과 유찰수 %", "조회"]

# 페이지 안에서 실행: 소재지 선택
JS_SET_REGION = """
async ([seoul, gu]) => {
  const fire = el => { if (window.jQuery) jQuery(el).trigger('change'); else el.dispatchEvent(new Event('change', {bubbles: true})); };
  const a1 = document.getElementById('addr1'); a1.value = seoul; fire(a1);
  const a2 = document.getElementById('addr2');
  for (let i = 0; i < 40 && a2.options.length < 2; i++) await new Promise(r => setTimeout(r, 250));
  const all = [...a2.options].filter(o => o.value && o.value !== '0').map(o => [o.value, o.text.trim()]);
  if (!gu) return {all};
  const o = [...a2.options].find(o => o.text.trim() === gu);
  if (!o) return {all, error: 'no-gu'};
  a2.value = o.value; fire(a2);
  await new Promise(r => setTimeout(r, 500));
  return {all, code: o.value};
}
"""

# 페이지 안에서 실행: 검색 폼 그대로 결과 요청 → 행 텍스트 추출
JS_FETCH_PAGE = """
async ([url, page, cnt]) => {
  const fd = new FormData(document.getElementById('searchform'));
  fd.set('page', String(page)); fd.set('list_view_cnt', String(cnt));
  const r = await fetch(url, {method: 'POST', body: new URLSearchParams(fd), credentials: 'include',
    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'}});
  const t = await r.text();
  const d = new DOMParser().parseFromString(t, 'text/html');
  const total = ((d.body.innerText.match(/총\\s*([\\d,]+)\\s*건/) || [])[1] || '0').replace(/,/g, '');
  const rows = [];
  for (const tr of d.querySelectorAll('.tbl_result tbody tr')) {
    const tds = [...tr.querySelectorAll(':scope > td')].map(td => td.innerText.replace(/\\s+/g, ' ').trim());
    const s = tds.findIndex(x => /서울(중앙|동부|서부|남부|북부)/.test(x) && /\\d{4}-\\d+/.test(x));
    if (s >= 0) rows.push(tds.slice(s, s + 7));
  }
  return {status: r.status, total: parseInt(total), rows};
}
"""


def is_logged_in(page) -> bool:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
        # 로그인 폼이 화면에 보이면 로그아웃 상태
        if page.locator("form[name=loginform] #pw").first.is_visible():
            return False
        return page.get_by_text("로그아웃").first.is_visible()
    except PWTimeout:
        return False


def ensure_login(page, headed: bool):
    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    if is_logged_in(page):
        return
    sa_id, sa_pw = os.getenv("SA_ID", ""), os.getenv("SA_PW", "")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    if sa_id and sa_pw:
        page.fill("form[name=loginform] #id", sa_id)
        page.fill("form[name=loginform] #pw", sa_pw)
        # 로그인 버튼은 <button class="login_btn">이고 type 속성이 없음 (2026-11-19 확인)
        btn = page.locator("form[name=loginform] button.login_btn, form[name=loginform] button").first
        try:
            btn.click(timeout=5_000)
        except PWTimeout:
            page.press("form[name=loginform] #pw", "Enter")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass
        time.sleep(2)
    elif headed:
        print("열린 창에서 스피드옥션에 로그인해 주세요. (최대 3분 대기)")
        for _ in range(90):
            time.sleep(2)
            if is_logged_in(page):
                break
    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    if not is_logged_in(page):
        raise RuntimeError("로그인하지 못했습니다. .env.credential의 SA_ID/SA_PW를 확인하거나 --headed로 직접 로그인하세요.")


def write_html(path, rows: list[list[str]]):
    out = ["<html><head><meta charset='utf-8'></head><body><table class='tbl_result'><thead><tr>"]
    out += [f"<th>{h}</th>" for h in HEADER]
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>")
    out.append("</tbody></table></body></html>")
    path.write_text("".join(out), encoding="utf-8")


@contextlib.contextmanager
def browser_session(cfg: dict, headed: bool):
    """로그인된 스피드옥션 페이지(Playwright page)를 돌려줍니다. 세션은 state/browser_profile에 유지."""
    profile = cfg["state"] / "browser_profile"
    profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=not headed, locale="ko-KR",
                                                   viewport={"width": 1400, "height": 900})
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ensure_login(page, headed)
            yield page
        finally:
            ctx.close()


def collect(cfg: dict, districts: list[str], page, per_page: int = 150) -> list:
    cfg["input"].mkdir(parents=True, exist_ok=True)
    saved = []
    if not districts:  # 서울 전체: 구 목록을 화면에서 가져옴
        page.goto(SEARCH_URL, wait_until="domcontentloaded")
        info = page.evaluate(JS_SET_REGION, [SEOUL_CODE, ""])
        districts = [name for _, name in info["all"]]
    print(f"수집 대상: {', '.join(districts)}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    for gu in districts:
        page.goto(SEARCH_URL, wait_until="domcontentloaded")
        info = page.evaluate(JS_SET_REGION, [SEOUL_CODE, gu])
        if info.get("error"):
            print(f"  {gu}: 구 이름을 찾지 못함, 건너뜀")
            continue
        rows, pg, total = [], 1, None
        while True:
            res = page.evaluate(JS_FETCH_PAGE, [RESULT_AJAX, pg, per_page])
            if res["status"] != 200:
                print(f"  {gu} {pg}쪽: HTTP {res['status']}, 중단")
                break
            total = res["total"] if total is None else total
            rows += res["rows"]
            if not res["rows"] or len(rows) >= total:
                break
            pg += 1
            time.sleep(random.uniform(3, 6))  # 사람이 넘기는 정도의 간격
        path = cfg["input"] / f"speed_{gu}_{stamp}.html"
        write_html(path, rows)
        saved.append(path)
        print(f"  {gu}: {len(rows)}/{total}건 → {path.name}")
        time.sleep(random.uniform(6, 10))
    return saved


def main():
    ap = argparse.ArgumentParser(description="스피드옥션 구별 수집 + 정비구역 매칭")
    ap.add_argument("--districts", help="쉼표로 구분한 구 이름. 빈 문자열이면 서울 전체 (.env의 DISTRICTS보다 우선)")
    ap.add_argument("--headed", action="store_true", help="브라우저 창을 띄워 실행 (첫 로그인 확인용)")
    ap.add_argument("--collect-only", action="store_true", help="수집만 하고 매칭은 하지 않음")
    ap.add_argument("--no-interest", action="store_true", help="관심물건 저장을 하지 않음")
    args = ap.parse_args()
    cfg = load_config()
    if args.districts is not None:
        cfg["districts"] = [d.strip() for d in args.districts.split(",") if d.strip()]
    save_interest = not args.no_interest and os.getenv("SAVE_INTEREST", "1") == "1"
    with browser_session(cfg, args.headed) as page:
        saved = collect(cfg, cfg["districts"], page)
        if not saved or args.collect_only:
            return
        result = run_once(cfg)
        if result and save_interest:
            from sa_interest import save_interests
            page.goto(SEARCH_URL, wait_until="domcontentloaded")  # jQuery가 있는 화면에서 저장 요청
            save_interests(cfg, result, page)


if __name__ == "__main__":
    sys.exit(main())
