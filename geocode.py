"""브이월드(국토교통부) 지오코더로 주소를 좌표로 바꿉니다. 결과는 파일에 캐시합니다.

인증키: https://www.vworld.kr → 인증키 발급 (무료). 서비스 'Geocoder API', 'WFS/데이터 API' 선택.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

GEOCODE_URL = "https://api.vworld.kr/req/address"
DATA_URL = "https://api.vworld.kr/req/data"


class VWorld:
    def __init__(self, key: str, cache_path: Path, domain: str = "", pause: float = 0.15):
        if not key:
            raise ValueError("VWORLD_KEY가 비어 있습니다. .env 파일을 확인하세요.")
        self.key = key
        # 2D데이터 API는 domain 값이 없으면 INCORRECT_KEY를 돌려줌 (2026-11-19 확인)
        self.domain = domain or "http://localhost"
        self.pause = pause
        self.cache_path = cache_path
        self.cache: dict = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        self.session = requests.Session()

    def save(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=0), encoding="utf-8")

    def _get(self, url: str, params: dict) -> dict:
        params = {**params, "key": self.key, "format": "json"}
        if self.domain:
            params["domain"] = self.domain
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=15)
                r.raise_for_status()
                time.sleep(self.pause)
                return r.json()
            except (requests.RequestException, ValueError):
                time.sleep(1.5 * (attempt + 1))
        return {}

    def geocode(self, address: str, kind: str = "parcel") -> dict | None:
        """{'lon','lat','refined'} 또는 None. 지번으로 실패하면 도로명으로 한 번 더 시도합니다."""
        ck = f"{kind}:{address}"
        if ck in self.cache:
            return self.cache[ck]
        result = None
        for t in (kind, "road" if kind == "parcel" else "parcel"):
            j = self._get(GEOCODE_URL, {
                "service": "address", "request": "getcoord", "version": "2.0",
                "crs": "epsg:4326", "address": address, "refine": "true",
                "simple": "false", "type": t,
            })
            resp = j.get("response", {})
            if resp.get("status") == "OK":
                pt = resp["result"]["point"]
                refined = resp.get("refined", {}).get("text", "")
                result = {"lon": float(pt["x"]), "lat": float(pt["y"]), "refined": refined, "type": t}
                break
        self.cache[ck] = result
        return result

    def parcel_polygon(self, lon: float, lat: float) -> dict | None:
        """좌표가 들어 있는 필지 경계(연속지적도)를 GeoJSON geometry로 돌려줍니다."""
        ck = f"pnu:{lon:.6f},{lat:.6f}"
        if ck in self.cache:
            return self.cache[ck]
        j = self._get(DATA_URL, {
            "service": "data", "request": "GetFeature", "data": "LP_PA_CBND_BUBUN",
            "geomFilter": f"POINT({lon} {lat})", "crs": "EPSG:4326",
            "geometry": "true", "attribute": "true", "size": "1",
        })
        out = None
        try:
            feats = j["response"]["result"]["featureCollection"]["features"]
            if feats:
                out = {"geometry": feats[0]["geometry"], "pnu": feats[0]["properties"].get("pnu", "")}
        except (KeyError, TypeError):
            out = None
        self.cache[ck] = out
        return out
