# Notion Watchlist Sync

노션 Watchlist DB의 `Name`에 제목을 적고 `;`로 끝내면 TMDB에서 영화·TV 정보를 찾아 채워 줍니다.
가끔 전체 목록을 다시 확인해서 새 에피소드, 방영 상태, 다음 방영일 같은 정보도 최신으로 유지합니다.
예전 [Notion Watchlist](https://nwatchlist.notion.site/Guide-d3e0cc0c1949463bbef2df40f0f515c5) 서비스와 같은 DB 구조를 쓰고, 서버 없이 GitHub Actions에서 돌아갑니다.

## 쓰는 법

| `Name`에 적는 값 | 결과 |
| --- | --- |
| `The Batman;` | 제목으로 검색 (영화·TV 중 가장 맞는 것) |
| `ナルト;` | 원어 제목도 됨 |
| `tt5180504;` | IMDb ID로 찾기 |
| `The Batman[t;` / `The Batman[m;` | TV만 / 영화만 |
| `Home Alone[m1992;` 또는 `Home Alone[1992m;` | 영화 + 연도 (연도는 t/m과 같이 써야 함) |

- 5분마다 확인하므로 GitHub 상황에 따라 채워지기까지 5~15분 정도 걸립니다.
- 다시 찾고 싶으면 `Name`을 새로 적고 `;`를 붙이면 됩니다.
- 못 찾으면 `제목 | No Title Found!`, 오류면 `제목 | Error!`, TMDB에서 ID가 사라졌으면 `제목 | Invalid ID!`로 바뀝니다. 고쳐서 다시 `;`를 붙이세요.

### 전체 갱신 시점

5분마다 `;` 항목을 확인하고, 전체 갱신은 아래 경우에만 합니다. 마지막 전체 갱신 시각은 `.sync-state.json`에 기록됩니다.

- 마지막 전체 갱신 후 **14일**이 지났을 때
- 새 `;` 항목을 추가했는데 마지막 전체 갱신 후 **3일** 이상 지났을 때 (3일 안이면 새 항목만 채움)
- Actions에서 `mode: sync`로 직접 실행했을 때

기간은 워크플로의 `FULL_SYNC_EVERY_DAYS`, `FULL_SYNC_STALE_DAYS`로 바꿀 수 있습니다.

### 자동으로 관리하는 것

- 전체 갱신 때: 상태(`Returning Series` → `Ended` 등), `Last Episode`, `Upcoming Episode`, `Next Air Date`, 시즌·에피소드 수, 평점 등
- `Watch Status`가 `Watched`인 시리즈에 새 에피소드가 나오면 `Unwatched`로 바꿈
- `VOD`는 한국에서 볼 수 있는 것만 넣습니다 (규칙은 [`vod.json`](vod.json)):
  - TMDB 기준 한국에서 구독·무료로 볼 수 있는 서비스 (넷플릭스, 티빙, 웨이브, 쿠팡플레이, 왓챠, 라프텔, 디즈니+ 등)
  - 한국 방송사 (KBS, MBC, SBS, JTBC, tvN, ENA, EBS …)
  - 우회 가입으로 볼 수 있는 해외 OTT (Prime Video, HBO Max, Hulu, Peacock, Paramount+, Crunchyroll)
  - 직접 넣은 값 중 `keep` 목록에 있는 것(영화관, 주요ott외/없음 등)은 유지하고, 후지TV 같은 해외 방송사는 지웁니다.
  - 영화도 한국 OTT를 넣고, OTT에 아직 없는데 한국 극장 개봉일이 최근 120일 ~ 60일 뒤 사이면 `영화관`을 넣습니다 (`THEATER_DAYS`로 조정).
  - 한번 들어간 OTT·영화관은 나중에 서비스가 끝나도 지우지 않습니다 (어디서 봤는지 기록용).
- `한국어 제목`이 비어 있으면 TMDB의 한국어 제목으로 채웁니다. 이미 적은 값은 건드리지 않습니다.
- `Name`과 이미 있는 포스터(`IMG`)는 처음 채울 때만 정하고, 이후 갱신에서는 바꾸지 않습니다.
- `Number Rating`, `시청날짜`처럼 직접 만든 속성은 건드리지 않습니다. 값이 바뀐 속성만 씁니다.

## 설정

### 1. TMDB 읽기 토큰
[themoviedb.org](https://www.themoviedb.org/settings/api) 로그인 → 설정 → API에서 **API 읽기 액세스 토큰**(`eyJ...`로 시작하는 긴 값)을 복사합니다. 짧은 API 키(v3)도 되지만 주소에 붙어서 전송되는 방식이라 토큰을 권장합니다.

### 2. 노션 통합(integration) 만들기
1. <https://www.notion.so/profile/integrations> → **새 통합** → 유형 **내부(Internal)**, 워크스페이스 선택
2. 기능: 콘텐츠 읽기·업데이트·삽입 체크 → 저장 → **내부 통합 시크릿**(`ntn_...`) 복사
3. 노션에서 Watchlist DB 페이지를 열고 오른쪽 위 `⋯` → **연결(Connections)** → 방금 만든 통합 추가
4. 예전 *Notion Watchlist* 통합은 같은 메뉴에서 연결을 끊어 두세요 (둘이 동시에 쓰면 서로 덮어씁니다)

### 3. GitHub Secrets 등록
저장소 → **Settings → Secrets and variables → Actions → Repository secrets → New repository secret** (Environment secrets가 아님)

| 이름 | 값 |
| --- | --- |
| `NOTION_TOKEN` | 노션 통합 시크릿 |
| `NOTION_DATABASE_ID` | DB 주소의 32자리 ID (또는 DB 링크 전체) |
| `TMDB_API_KEY` | TMDB 읽기 액세스 토큰 (또는 API 키) |

### 4. 처음 실행
**Actions → Watchlist sync → Run workflow**. `dry_run`을 켜면 노션에 쓰지 않고 바뀔 속성 이름만 로그에 나옵니다.
이후에는 5분마다 자동으로 실행됩니다.

## 개인정보

- 토큰·키·DB ID는 모두 Secrets에 들어가며 코드나 로그에 나오지 않습니다 (로그에서 `***`로 가려짐).
- 공개 저장소의 Actions 로그는 누구나 볼 수 있으므로, 로그에는 작품 제목 대신 페이지 ID 앞 8자리만 남깁니다.
  제목까지 보고 싶으면 워크플로의 `env`에 `LOG_TITLES: "1"`을 추가하세요.

## 선택 설정 (워크플로 `env`)

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `TMDB_LANGUAGE` | `en-US` | 장르·줄거리·영어 제목 언어 (`ko-KR`로 바꾸면 한국어로, 단 장르 옵션 이름도 한국어로 새로 생김) |
| `CERT_COUNTRY` | `US` | `Content Rating` 기준 국가 |
| `WATCH_REGION` | `KR` | VOD에 넣을 스트리밍 서비스 기준 국가 |
| `KO_TITLE_PROP` | `한국어 제목` | 한국어 제목을 채울 속성 이름 (없으면 건너뜀) |
