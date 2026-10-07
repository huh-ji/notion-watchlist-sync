#!/usr/bin/env python3
"""노션 Watchlist DB를 TMDB 정보로 채우고 주기적으로 갱신한다.

사용법:
    python watchlist_sync.py auto      # ';' 항목 채우기 + 필요할 때만 전체 갱신 (예약 실행용)
    python watchlist_sync.py trigger   # Name이 ';'로 끝나는 행만 찾아서 채우기
    python watchlist_sync.py sync      # ID가 있는 모든 행을 TMDB 최신 정보로 갱신

auto의 전체 갱신 조건 (마지막 전체 갱신 시각은 .sync-state.json에 저장):
    - 마지막 전체 갱신 후 FULL_SYNC_EVERY_DAYS(기본 14)일이 지났거나
    - 새 ';' 항목이 있었고, 마지막 전체 갱신 후 FULL_SYNC_STALE_DAYS(기본 3)일이 지났을 때

환경 변수:
    NOTION_TOKEN        노션 내부 통합(integration) 시크릿
    NOTION_DATABASE_ID  Watchlist DB의 ID 또는 URL
    TMDB_API_KEY        TMDB API 읽기 토큰(v4) 또는 API 키(v3)
    TMDB_LANGUAGE       장르·줄거리 언어 (기본 en-US)
    CERT_COUNTRY        관람 등급 기준 국가 (기본 US)
    WATCH_REGION        VOD에 넣을 스트리밍 서비스 기준 국가 (기본 KR)
    DRY_RUN=1           노션에 쓰지 않고 바뀔 내용만 출력
    LOG_TITLES=1        로그에 작품 제목 표시 (공개 저장소에서는 끄는 것을 권장)
"""
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
TMDB_API = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/original"

# 노션 DB 속성 이름 (원래 Notion Watchlist 서비스와 같은 이름)
P_NAME = "Name"
P_ID = "ID"
P_TYPE = "Type"
P_WATCH = "Watch Status"
P_LAST_EP = "Last Episode"
P_VOD = "VOD"
P_IMG = "IMG"
P_KO_TITLE = os.environ.get("KO_TITLE_PROP", "한국어 제목")

TYPE_TV, TYPE_MOVIE = "TV Series", "Movie"

log = logging.getLogger("watchlist")
DRY_RUN = bool(os.environ.get("DRY_RUN"))
LOG_TITLES = bool(os.environ.get("LOG_TITLES"))

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".sync-state.json"
FULL_SYNC_EVERY = timedelta(days=float(os.environ.get("FULL_SYNC_EVERY_DAYS") or 14))
FULL_SYNC_STALE = timedelta(days=float(os.environ.get("FULL_SYNC_STALE_DAYS") or 3))

_vod = json.loads((HERE / "vod.json").read_text(encoding="utf-8"))
VOD_KEEP = set(_vod["keep"])
VOD_ALIASES = _vod["aliases"]
WATCH_REGION = os.environ.get("WATCH_REGION", "KR")
SEEN_PROVIDERS = set()  # 로그용: TMDB가 알려 준 한국 플랫폼 이름들


def vod_name(name):
    name = name.strip()
    return VOD_ALIASES.get(name, name)


class Fatal(Exception):
    """설정·권한 문제처럼 계속 진행해도 의미 없는 오류."""


def http(session, method, url, **kw):
    """429·5xx면 잠깐 기다렸다 다시 시도한다."""
    for attempt in range(6):
        try:
            r = session.request(method, url, timeout=30, **kw)
        except requests.RequestException:
            if attempt == 5:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 60))
            continue
        return r
    return r


# ───────────────────────────── Notion ─────────────────────────────

def normalize_id(raw):
    m = re.search(r"[0-9a-fA-F]{32}", raw.replace("-", ""))
    if not m:
        raise Fatal("NOTION_DATABASE_ID 형식이 올바르지 않습니다")
    return m.group(0)


class Notion:
    def __init__(self, token, database_id):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self.database_id = normalize_id(database_id)
        self._last_write = 0.0

    def call(self, method, path, body=None):
        r = http(self.s, method, NOTION_API + path, json=body)
        if r.status_code in (401, 403):
            raise Fatal(f"노션 권한 오류({r.status_code}). NOTION_TOKEN을 확인하세요.")
        if r.status_code == 404 and path.startswith("/databases/"):
            raise Fatal("DB를 찾을 수 없습니다. DB 페이지의 ⋯ > 연결(Connections)에서 통합을 추가했는지 확인하세요.")
        if not r.ok:
            raise RuntimeError(f"Notion {method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json()

    def load_schema(self):
        db = self.call("GET", f"/databases/{self.database_id}")
        sources = db.get("data_sources") or []
        if not sources:
            raise Fatal("DB에 데이터 소스가 없습니다")
        self.ds_id = sources[0]["id"]
        ds = self.call("GET", f"/data_sources/{self.ds_id}")
        self.schema = {name: p["type"] for name, p in ds["properties"].items()}
        for need, ptype in ((P_NAME, "title"), (P_ID, "number")):
            if self.schema.get(need) != ptype:
                raise Fatal(f"DB에 '{need}' ({ptype}) 속성이 필요합니다")

    def query(self, flt):
        body = {"page_size": 100, "filter": flt}
        while True:
            res = self.call("POST", f"/data_sources/{self.ds_id}/query", body)
            yield from res["results"]
            if not res.get("has_more"):
                return
            body["start_cursor"] = res["next_cursor"]

    def update(self, page_id, props):
        # 노션 API는 초당 3회 정도로 제한된다
        wait = 0.35 - (time.time() - self._last_write)
        if wait > 0:
            time.sleep(wait)
        self.call("PATCH", f"/pages/{page_id}", {"properties": props})
        self._last_write = time.time()


def read_prop(prop):
    """노션 속성 값을 비교하기 쉬운 파이썬 값으로."""
    if not prop:
        return None
    t = prop["type"]
    v = prop.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v).strip() or None
    if t == "select":
        return v["name"] if v else None
    if t == "multi_select":
        return [x["name"] for x in v] or None
    if t == "date":
        return v["start"] if v else None
    if t == "files":
        return [(f.get("external") or {}).get("url") or "notion-file" for f in v] or None
    if t in ("number", "url"):
        return v
    return None


def option_name(s):
    # 노션 선택 옵션 이름에는 쉼표를 쓸 수 없다
    return s.replace(",", " ").strip()[:100]


def rich(s):
    return [{"type": "text", "text": {"content": s[:2000]}}] if s else []


SKIP = object()


def to_notion(ptype, value):
    if ptype == "title":
        return {"title": rich(value)}
    if ptype == "rich_text":
        return {"rich_text": rich(value)}
    if ptype == "number":
        if value is not None and not isinstance(value, (int, float)):
            return SKIP
        return {"number": value}
    if ptype == "select":
        return {"select": {"name": option_name(value)} if value else None}
    if ptype == "multi_select":
        return {"multi_select": [{"name": option_name(v)} for v in value or []]}
    if ptype == "date":
        return {"date": {"start": value} if value else None}
    if ptype == "url":
        return {"url": value or None}
    if ptype == "files":
        return {"files": [{"type": "external", "name": "poster", "external": {"url": value}}] if value else []}
    return SKIP


def same(ptype, cur, new):
    if cur in ("", []):
        cur = None
    if new in ("", []):
        new = None
    if cur is None or new is None:
        return cur is new
    if ptype == "select":
        return cur == option_name(new)
    if ptype == "multi_select":
        return sorted(cur) == sorted(option_name(v) for v in new)
    if ptype == "number":
        return isinstance(new, (int, float)) and abs(cur - new) < 1e-9
    if ptype == "files":
        return cur == [new]
    if ptype in ("title", "rich_text"):
        return cur == new[:2000].strip()
    return cur == new


# ───────────────────────────── TMDB ─────────────────────────────

class TMDB:
    def __init__(self, key, language):
        self.s = requests.Session()
        self.params = {}
        if key.startswith("eyJ"):  # v4 읽기 토큰
            self.s.headers["Authorization"] = f"Bearer {key}"
        else:
            self.params["api_key"] = key
        self.language = language
        self._langs = None
        self._countries = None

    def get(self, path, **params):
        r = http(self.s, "GET", TMDB_API + path, params={**self.params, "language": self.language, **params})
        if r.status_code == 401:
            raise Fatal("TMDB 키가 올바르지 않습니다. TMDB_API_KEY를 확인하세요.")
        if r.status_code == 404:
            return None
        if not r.ok:
            raise RuntimeError(f"TMDB {path} → {r.status_code}: {r.text[:300]}")
        return r.json()

    def language_name(self, code):
        if self._langs is None:
            self._langs = {x["iso_639_1"]: x.get("english_name") or x.get("name")
                           for x in self.get("/configuration/languages") or []}
        return self._langs.get(code) or code

    def country_name(self, code):
        if self._countries is None:
            self._countries = {x["iso_3166_1"]: x.get("english_name")
                               for x in self.get("/configuration/countries") or []}
        return self._countries.get(code) or code

    def details(self, kind, tmdb_id):
        extra = "credits,external_ids,videos,translations,watch/providers," + (
            "content_ratings" if kind == "tv" else "release_dates")
        return self.get(f"/{kind}/{tmdb_id}", append_to_response=extra,
                        include_video_language="en,ko,ja,null")

    def find(self, query, kind=None, year=None):
        """(kind, tmdb_id) 또는 None."""
        if re.fullmatch(r"tt\d{5,}", query, re.I):
            res = self.get(f"/find/{query.lower()}", external_source="imdb_id") or {}
            for k in ("movie", "tv"):
                if (kind in (None, k)) and res.get(f"{k}_results"):
                    return k, res[f"{k}_results"][0]["id"]
            return None
        if kind:
            params = {"query": query}
            if year:
                params["primary_release_year" if kind == "movie" else "first_air_date_year"] = year
            results = (self.get(f"/search/{kind}", **params) or {}).get("results") or []
            return (kind, results[0]["id"]) if results else None
        for r in (self.get("/search/multi", query=query) or {}).get("results") or []:
            if r.get("media_type") in ("movie", "tv"):
                return r["media_type"], r["id"]
        return None


FILTER_RE = re.compile(r"(?P<k1>[tm])(?P<y1>\d{4})?|(?P<y2>\d{4})(?P<k2>[tm])", re.I)


def parse_trigger(text):
    """'Home Alone[m1990;' → ('Home Alone', 'movie', 1990)"""
    text = text.strip().rstrip(";").strip()
    m = re.fullmatch(r"(.*)\[([^\[\]]*)", text, re.S)
    if m:
        f = FILTER_RE.fullmatch(m.group(2).strip())
        if f:
            k = (f.group("k1") or f.group("k2")).lower()
            y = f.group("y1") or f.group("y2")
            return m.group(1).strip(), "tv" if k == "t" else "movie", int(y) if y else None
    return text, None, None


THEATER = "영화관"
THEATER_WINDOW = (timedelta(days=-int(os.environ.get("THEATER_DAYS") or 120)), timedelta(days=60))


def in_theaters(d):
    """한국 극장 개봉일(TMDB release_dates 유형 2·3)이 최근 THEATER_DAYS일 ~ 60일 뒤 사이인지."""
    today = datetime.now(timezone.utc).date()
    for r in (d.get("release_dates") or {}).get("results") or []:
        if r.get("iso_3166_1") != WATCH_REGION:
            continue
        for x in r.get("release_dates") or []:
            if x.get("type") in (2, 3) and x.get("release_date"):
                day = datetime.fromisoformat(x["release_date"][:10]).date()
                if THEATER_WINDOW[0] <= day - today <= THEATER_WINDOW[1]:
                    return True
    return False


def episode_label(ep):
    if not ep:
        return None
    label = f"S{ep.get('season_number')}, E{ep.get('episode_number')}"
    return f"{label}: {ep['name']}" if ep.get("name") else label


def episode_number(label):
    m = re.match(r"S(\d+), E(\d+)", label or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def build_values(tmdb, kind, d):
    """TMDB 상세 정보를 {노션 속성 이름: 값}으로."""
    tv = kind == "tv"
    credits = d.get("credits") or {}
    crew = credits.get("crew") or []

    def crew_names(pred, limit):
        names = []
        for c in crew:
            if pred(c) and c["name"] not in names:
                names.append(c["name"])
        return ", ".join(names[:limit]) or None

    original = d.get("original_name" if tv else "original_title") or ""
    english = d.get("name" if tv else "title") or ""
    first = d.get("first_air_date" if tv else "release_date") or None
    last = d.get("last_air_date") if tv else None

    if tv:
        y1 = first[:4] if first else ""
        if d.get("in_production") or d.get("status") in ("Returning Series", "In Production", "Planned", "Pilot"):
            year = f"{y1} -" if y1 else None
        else:
            year = f"{y1} - {last[:4]}" if y1 and last else (y1 or None)
    else:
        year = first[:4] if first else None

    country = os.environ.get("CERT_COUNTRY", "US")
    rating = None
    if tv:
        for r in (d.get("content_ratings") or {}).get("results") or []:
            if r.get("iso_3166_1") == country and r.get("rating"):
                rating = r["rating"]
    else:
        for r in (d.get("release_dates") or {}).get("results") or []:
            if r.get("iso_3166_1") == country:
                rating = next((x["certification"] for x in r.get("release_dates") or []
                               if x.get("certification")), None)

    videos = [v for v in (d.get("videos") or {}).get("results") or [] if v.get("site") == "YouTube"]
    trailer = max(videos, default=None, key=lambda v: (
        v.get("type") == "Trailer", v.get("type") == "Teaser", bool(v.get("official")),
        v.get("iso_639_1") == "en", v.get("published_at") or ""))

    countries = d.get("origin_country") or [c["iso_3166_1"] for c in d.get("production_countries") or []]
    imdb_id = (d.get("external_ids") or {}).get("imdb_id") or d.get("imdb_id")

    runtime = None
    if tv:
        runtime = (d.get("episode_run_time") or [None])[0] or (d.get("last_episode_to_air") or {}).get("runtime")
    else:
        runtime = d.get("runtime") or None

    nxt = d.get("next_episode_to_air") if tv else None
    upcoming = episode_label(nxt)
    if tv and not upcoming and d.get("status") in ("Returning Series", "In Production", "Planned"):
        upcoming = "TBA"

    vote = d.get("vote_average")
    values = {
        P_NAME: original or english,
        "Eng Name": english if english and english != original else None,
        P_TYPE: TYPE_TV if tv else TYPE_MOVIE,
        P_ID: d["id"],
        "Release Date": first,
        "Status": d.get("status") or None,
        "Genre": [g["name"] for g in d.get("genres") or []],
        "Language": tmdb.language_name(d.get("original_language")) if d.get("original_language") else None,
        "Year": year,
        "Runtime": runtime,
        P_IMG: POSTER_BASE + d["poster_path"] if d.get("poster_path") else None,
        "Director": crew_names(lambda c: c.get("job") == "Director", 5),
        "Writer": crew_names(lambda c: c.get("department") == "Writing", 5)
                  or (", ".join(c["name"] for c in d.get("created_by") or []) or None),
        "Producer": crew_names(lambda c: c.get("job") in (("Executive Producer", "Producer") if tv else ("Producer",)), 5),
        "Trailer": f"https://www.youtube.com/watch?v={trailer['key']}" if trailer else None,
        "Synopsis": d.get("overview") or None,
        "Cast": ", ".join(c["name"] for c in (credits.get("cast") or [])[:10]) or None,
        "Content Rating": rating,
        "TMDB Rating": round(vote, 1) if vote else None,
        "IMDb ID": imdb_id or None,
        "IMDb Page": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None,
        "Homepage": d.get("homepage") or None,
        "Country of origin": ", ".join(tmdb.country_name(c) for c in countries) or None,
        # TV 전용 (영화면 비워 둠)
        "Last Air Date": last or None,
        P_LAST_EP: episode_label(d.get("last_episode_to_air")) if tv else None,
        "Upcoming Episode": upcoming,
        "Next Air Date": (nxt or {}).get("air_date") or None,
        "Episodes": d.get("number_of_episodes") if tv else None,
        "Seasons": d.get("number_of_seasons") if tv else None,
    }
    # VOD 후보: 한국 방송사 + keep 목록의 OTT 방송사 + 한국에서 구독·무료로 볼 수 있는 서비스
    networks = []
    for n in (d.get("networks") or []) if tv else []:
        name = vod_name(n["name"])
        if n.get("origin_country") == "KR" or name in VOD_KEEP:
            networks.append(name)
    region = ((d.get("watch/providers") or {}).get("results") or {}).get(WATCH_REGION) or {}
    for kind_ in ("flatrate", "free", "ads"):
        networks += [vod_name(p["provider_name"]) for p in region.get(kind_) or []]
    for kind_ in ("flatrate", "free", "ads", "rent", "buy"):
        SEEN_PROVIDERS.update(f"{p['provider_name']} ({kind_})" for p in region.get(kind_) or [])
    # OTT에 없는 영화가 한국에서 극장 개봉 중(또는 곧 개봉)이면 영화관
    if not tv and not networks and in_theaters(d):
        networks.append(THEATER)

    ko_title = None
    for t in (d.get("translations") or {}).get("translations") or []:
        if t.get("iso_639_1") == "ko":
            ko_title = (t.get("data") or {}).get("name" if tv else "title") or None
            break
    return values, networks, ko_title


# ───────────────────────────── 동기화 ─────────────────────────────

class Runner:
    def __init__(self, notion, tmdb):
        self.notion = notion
        self.tmdb = tmdb
        self.stats = {"채움": 0, "갱신": 0, "변경없음": 0, "못찾음": 0, "오류": 0}

    def label(self, page, name=None):
        if LOG_TITLES and name:
            return f"{name} ({page['id'][:8]})"
        return page["id"][:8]

    def write(self, page, values, skip_img_if_set):
        props = page["properties"]
        out = {}
        for name, value in values.items():
            ptype = self.notion.schema.get(name)
            if not ptype:
                continue  # DB에 없는 속성은 건너뜀
            cur = read_prop(props.get(name))
            if ptype == "files" and skip_img_if_set and cur:
                continue  # 이미 있는 포스터는 그대로 둠
            if same(ptype, cur, value):
                continue
            conv = to_notion(ptype, value)
            if conv is SKIP:
                continue
            out[name] = conv
        if out:
            log.info("    바뀜: %s", ", ".join(out))
            if P_VOD in out:
                log.info("    VOD: %s → %s", read_prop(props.get(P_VOD)) or [], [x["name"] for x in out[P_VOD]["multi_select"]])
            if not DRY_RUN:
                self.notion.update(page["id"], out)
        return bool(out)

    def set_title(self, page, text):
        if not DRY_RUN:
            self.notion.update(page["id"], {P_NAME: {"title": rich(text)}})

    def merged_extras(self, page, values, networks, ko_title):
        props = page["properties"]
        # 기존 값은 keep 목록(OTT·영화관 등)만 남기고 해외 방송사는 지움. 방송사는 TMDB에서 매번 다시 계산.
        vod = []
        for v in [vod_name(x) for x in read_prop(props.get(P_VOD)) or [] if vod_name(x) in VOD_KEEP] + networks:
            if option_name(v) not in [option_name(x) for x in vod]:
                vod.append(v)
        values[P_VOD] = vod
        if ko_title and not read_prop(props.get(P_KO_TITLE)):
            values[P_KO_TITLE] = ko_title

    def trigger(self):
        flt = {"property": P_NAME, "title": {"ends_with": ";"}}
        seen = 0
        for page in self.notion.query(flt):
            raw = read_prop(page["properties"].get(P_NAME)) or ""
            query, kind, year = parse_trigger(raw)
            if not query:
                continue
            seen += 1
            log.info("[채우기] %s", self.label(page, raw))
            try:
                hit = self.tmdb.find(query, kind, year)
                if not hit:
                    log.info("    TMDB에서 찾지 못함")
                    self.set_title(page, f"{query} | No Title Found!")
                    self.stats["못찾음"] += 1
                    continue
                kind, tmdb_id = hit
                d = self.tmdb.details(kind, tmdb_id)
                values, networks, ko_title = build_values(self.tmdb, kind, d)
                self.merged_extras(page, values, networks, ko_title)
                if not read_prop(page["properties"].get(P_WATCH)):
                    values[P_WATCH] = "Unwatched"
                self.write(page, values, skip_img_if_set=False)
                self.stats["채움"] += 1
            except Fatal:
                raise
            except Exception:
                log.exception("    오류")
                self.stats["오류"] += 1
                try:
                    self.set_title(page, f"{query} | Error!")
                except Exception:
                    pass
        return seen

    def sync(self):
        flt = {"and": [
            {"property": P_ID, "number": {"is_not_empty": True}},
            {"property": P_NAME, "title": {"does_not_contain": "|"}},
        ]}
        for page in self.notion.query(flt):
            props = page["properties"]
            name = read_prop(props.get(P_NAME)) or ""
            if name.endswith(";"):
                continue  # trigger에서 처리
            tmdb_id = read_prop(props.get(P_ID))
            ptype = read_prop(props.get(P_TYPE))
            kinds = ["tv"] if ptype == TYPE_TV else ["movie"] if ptype == TYPE_MOVIE else ["tv", "movie"]
            try:
                d = kind = None
                for kind in kinds:
                    d = self.tmdb.details(kind, int(tmdb_id))
                    if d:
                        break
                if not d:
                    log.info("[갱신] %s: TMDB ID가 더 이상 없음", self.label(page, name))
                    self.set_title(page, f"{name} | Invalid ID!")
                    self.stats["오류"] += 1
                    continue
                values, networks, ko_title = build_values(self.tmdb, kind, d)
                values.pop(P_NAME)  # 이름은 처음 채울 때만 정하고, 이후엔 건드리지 않음
                self.merged_extras(page, values, networks, ko_title)

                # 다 본 시리즈에 새 에피소드가 나오면 다시 '안 봄'으로
                if kind == "tv" and read_prop(props.get(P_WATCH)) == "Watched":
                    old = episode_number(read_prop(props.get(P_LAST_EP)))
                    new = episode_number(values.get(P_LAST_EP))
                    if old and new and new > old:
                        values[P_WATCH] = "Unwatched"
                        log.info("[새 에피소드] %s → Unwatched", self.label(page, name))

                if self.write(page, values, skip_img_if_set=True):
                    log.info("[갱신] %s", self.label(page, name))
                    self.stats["갱신"] += 1
                else:
                    self.stats["변경없음"] += 1
            except Fatal:
                raise
            except Exception:
                log.exception("[갱신 오류] %s", self.label(page, name))
                self.stats["오류"] += 1


def load_last_full_sync():
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))["last_full_sync"]
        return datetime.fromisoformat(raw)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_last_full_sync(when):
    STATE_FILE.write_text(json.dumps({"last_full_sync": when.isoformat(timespec="seconds")}) + "\n",
                          encoding="utf-8")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in ("auto", "trigger", "sync"):
        sys.exit("사용법: watchlist_sync.py [auto|trigger|sync]")
    try:
        token = os.environ.get("NOTION_TOKEN")
        db_id = os.environ.get("NOTION_DATABASE_ID")
        key = os.environ.get("TMDB_API_KEY")
        missing = [n for n, v in (("NOTION_TOKEN", token), ("NOTION_DATABASE_ID", db_id), ("TMDB_API_KEY", key)) if not v]
        if missing:
            # 시크릿 등록 전에는 실패 메일이 쌓이지 않도록 조용히 건너뜀
            log.warning("Secrets가 없어 건너뜁니다: %s", ", ".join(missing))
            return
        notion = Notion(token, db_id)
        notion.load_schema()
        runner = Runner(notion, TMDB(key, os.environ.get("TMDB_LANGUAGE", "en-US")))
        if DRY_RUN:
            log.info("DRY_RUN: 노션에 쓰지 않습니다")

        new_items = runner.trigger() if mode in ("auto", "trigger") else 0

        full = mode == "sync"
        if mode == "auto":
            now = datetime.now(timezone.utc)
            last = load_last_full_sync()
            age = now - last if last else None
            if age is None:
                log.info("전체 갱신 기록이 없어 전체 갱신합니다")
                full = True
            elif age >= FULL_SYNC_EVERY:
                log.info("마지막 전체 갱신 후 %d일 지나 전체 갱신합니다", age.days)
                full = True
            elif new_items and age >= FULL_SYNC_STALE:
                log.info("새 항목이 있고 마지막 전체 갱신 후 %d일 지나 전체 갱신합니다", age.days)
                full = True
        if full:
            started = datetime.now(timezone.utc)
            runner.sync()
            if not DRY_RUN:
                save_last_full_sync(started)
        if SEEN_PROVIDERS:
            log.info("TMDB 한국 플랫폼: %s", ", ".join(sorted(SEEN_PROVIDERS)))
        log.info("완료 — %s", ", ".join(f"{k} {v}" for k, v in runner.stats.items()))
    except Fatal as e:
        log.error("중단: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
