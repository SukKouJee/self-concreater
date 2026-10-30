# S3 저장소 인터페이스 설계 (DocStore · ObjectStore · SharedCache)

작성일: 2026-10-31 / 기준: `feat/s3-store` (S2 `scripts/ctx.py` 완료 후)
상위 설계: `2026-10-31-multiuser-server-design.md` §2.1·§2.2·§6 S3
상태: 완료(2026-10-31). 상위 설계 §2.1의 키 예시를 이 문서 ADR-1에 맞춰 고쳤다. 게이트: `tests/test_no_direct_paths.py`, `count_work_root`의 `outside == 0`.

## 0. 목표와 완료 정의

- 끝나면 **`scripts/filestore.py`(와 아래 허용 목록) 밖의 모듈은 `work_root()`·`workspace_dir()`·`category_dir()`·
  `existing_category_dir()`·`move_to_trash()`를 부르지 않고, 작업 폴더 경로(`Path`)를 만들지 않는다.**
- 로컬판의 작업 폴더 구조·파일 바이트·CLI JSON 출력·UI API JSON은 **한 바이트도 바뀌지 않는다.**
  예외는 1개: 지금 원자적이지 않은 쓰기(`path.write_bytes`·`write_text`)가 모두 임시 파일 + 교체로 바뀐다(최종 바이트 동일).
- 각 단계가 끝날 때 `pytest tests/` 전부 통과.

## 1. 결정 기록(ADR)

| # | 결정 | 이유 | 대안(기각) |
|---|---|---|---|
| ADR-1 | **키 = 작업 폴더 기준 POSIX 상대 경로 문자열 그대로**(`refs/ref_travel/videos/Q8PSsikq8PA.md`, `workspace/frames/<id>/0003.jpg`) | 이 문자열이 이미 프런트매터(`draft`, `html`, `ref_frame`, `ref_auto.frame`, `art.file`, mine `target`, cuts json `scenes[].frame`), CLI 출력(`path`, `md`), UI API JSON, `/files/<rel>` URL에 저장·노출된다. 키를 따로 만들면 변환층이 생기고 바이트 동일성이 깨질 위험만 는다. 서버판 키 변환은 S5의 PgStore/S3ObjectStore 안에서 한다 | 상위 설계 §2.1의 `refs/travel/videos/<id>`(확장자·`ref_` 없음): S5에서 매핑 함수로 해결 |
| ADR-2 | **저장소는 바이트 계층.** 직렬화(YAML 프런트매터, JSON indent, 줄바꿈)는 지금처럼 모듈·`refstore`가 만든다 | 바이트 보존 규칙(`join_frontmatter`, CRLF 유지, BOM 처리, JSON `indent=1/2`)이 모듈마다 달라 저장소가 맡으면 반드시 어긋난다 | `put(key, front, body)`: S5에서 PgStore가 파싱해 JSONB로 넣으면 된다 |
| ADR-3 | 없는 키는 **`FileNotFoundError`**(OSError) | 지금 호출부가 `except OSError`·`except (OSError, ValueError)`로 잡는다. 새 예외를 만들면 수십 곳을 고쳐야 한다 | `ScriptError("not_found")` |
| ADR-4 | 버전(낙관적 잠금) = **`sha256(raw).hexdigest()`** | `conti.content_hash`·`videomd._file_hash`·`api_refs` hash·`polish` expected_hash가 모두 이 값이고 UI가 이미 들고 있다 | mtime, 정수 버전 |
| ADR-5 | 키 생성은 **`scripts/keys.py` 한 곳**(순수 함수, IO 없음). `Path` 조립은 **`scripts/filestore.py` 한 곳** | "경로 문자열은 FileStore 안에만"(S3 완료 조건)을 텍스트·AST 검사로 증명 가능하게 | 모듈마다 상수 |
| ADR-6 | 실제 파일 경로가 꼭 필요한 도구(OpenCV, PyAV, Remotion, claude CLI Read, `/files` Range)는 **`local_path`/`write_path` 컨텍스트 관리자**로만 얻는다 | 로컬판은 진짜 경로를 그대로(복사 없음), 서버판은 임시 파일 다운로드/업로드로 같은 코드를 돌린다 | 스트림 API: 외부 도구가 경로만 받음 |
| ADR-7 | Ctx에 저장소를 **지연 생성 속성**으로 붙이고, Ctx가 없을 때는 지금처럼 `YT_COACH_HOME`/cwd로 **호출마다** 임시 Ctx를 만든다(`ctx.here()`) | 테스트 33개 파일이 `YT_COACH_HOME`만 바꾸고 `ctx.use` 없이 함수를 부른다. 캐시하면 테스트 간에 root가 샌다 | 프로세스 전역 저장소 |

## 2. 저장 대상 인벤토리

분류: **Doc**=DocStore(md, 사용자별) · **Obj**=ObjectStore(사용자별 바이너리·JSON·HTML) · **Shared**=SharedCache(영상 ID 기준, 사용자 무관) ·
**Scratch**=로컬 전용/임시(서버판은 프로세스 임시 폴더나 전역 설정) · **밖**=저장소가 다루지 않음.
`<cat>`은 `^[a-z0-9][a-z0-9_-]*$`(지금 `common._CATEGORY_RE`), `<vid>`는 `[A-Za-z0-9_-]{1,64}`.

| 경로 / 키 형식 (= 키) | 분류 | 키 함수(`keys.py`) | 쓰는 모듈 | 비고 |
|---|---|---|---|---|
| `refs/ref_<cat>/` (폴더) | Doc 폴더 | `category(cat)` → `refs/ref_<cat>/` | refstore, 거의 전부(`existing_category_dir`) | 카테고리 존재 = 폴더 존재. `folder_exists`/`ensure_folder` |
| `refs/ref_<cat>/videos/<vid>.md` | Doc | `video_md(cat, vid)` | videomd, features, shots, refstore, api_refs/add/diagnose, similarity | `role: ref|mine`. 목록은 mtime 정렬 있음(examples) |
| `refs/ref_<cat>/index.md` | Doc | `index_md(cat)` | refstore(단계), cluster, motif, draft, similarity, status, mission, api_typify | `INDEX_LOCK` 유지 |
| `refs/ref_<cat>/techniques.md` | Doc | `techniques_md(cat)` | videomd, motif, library | |
| `drafts/<name>.md` | Doc | `draft_md(name)` | draft, polish, conti, status, mission, api_draft | |
| `drafts/<name>-conti.md` | Doc | `conti_md(draft_key)` | conti, conti_history, conti_art, footage_*, captions, api_conti | stem 접미사 `-conti` 규칙 유지 |
| `drafts/<name>-conti.html` | Obj | `conti_html(conti_key)` | conti(`_write_html`), status(recent) | **Windows에서 CRLF**(write_text 기본 줄바꿈) |
| `drafts/<name>-{captions,lines}.srt` | Obj | `srt(conti_key, suffix)` | conti(srt), api_conti(stale) | CRLF 고정(newline="" + 수동 치환) |
| `drafts/<name>-captions.mp4` | Obj(write_path) | `captions_mp4(conti_key)` | captions(Remotion), status | |
| `drafts/<name>-remaining.html`, `-footage.csv` | Obj | `remaining_html`, `footage_csv` | footage_out | csv는 UTF-8 BOM |
| `drafts/<name>-cuts/<…>.mp4` (폴더) | Obj(write_path, prefix) | `cut_files_dir(conti_key)` | footage_out(PyAV) | 폴더째 휴지통 |
| `mine/<name>.md` | Doc | `mine_md(name)` | similarity, report, status, mission, api_diagnose | `target`에 다른 키 문자열 저장 |
| `missions/<YYYY-MM-DD>.md` | Doc | `mission_md(day)` | mission, status, api_mission | |
| `reviews/<YYYY-Www>.md` | Doc | `review_md(week)` | mission, api_report | |
| `reports/<cat>-<day>.html` | Obj | `report_html(cat, day)` | report, api_report, status | `/files` 대상 |
| `profile.md` | Doc | `PROFILE` | motif, mission, api_help/motif/mission | |
| `yt-coach.json` | Obj | `CONFIG` | llm | |
| `library.json` | Obj | `LIBRARY_CONFIG` | library | 공용 라이브러리 설정·원장. `indent=2 + "\n"` |
| `.gitignore`, `.git/`(존재만) | Obj / 폴더 확인 | `GITIGNORE`, `GIT_DIR` | refstore.scaffold | 로컬 전용 의미 |
| `workspace/raw/<vid>.json` | Shared | `raw(vid)` | fetch, videomd, conti(speech_rate) | CRLF(Windows), indent=2 |
| `workspace/cuts/<vid>.json` | Shared | `cuts(vid)` | cuts, videomd, conti | |
| `workspace/frames/<vid>/<nnnn>.jpg` (폴더) | Shared(write_path) | `frames_dir(vid)` | cuts(OpenCV), shots, conti, api_add | 문자열이 cuts json·md·콘티 fm에 저장됨 |
| `workspace/shots/<vid>.json` | Shared | `shots(vid)` | shots, conti(frame_pool) | indent=1, CRLF(Windows) |
| `workspace/typify/<cat>{,.applied,.preview}.json` | Obj | `typify(cat, variant=None)` | cluster, motif, similarity, report, videomd | |
| `workspace/conti_history/<conti stem>/NNNN.json` | Obj | `history_dir(conti_key)`, `history_version(conti_key, n)` | conti_history | indent=1, `newline="\n"` 고정 |
| `workspace/conti-art/<conti stem>/<id>-<basis8>-<sha6>.svg` | Obj | `art(conti_key, cut_id, basis, sha6)` | conti_art, conti(asset_url, data uri) | `conti.ART_DIR` 검증 규칙 그대로 |
| `workspace/footage/<fid>/{scenes.json,transcript.json,frames/…,montage/…}` | Obj(write_path) | `footage_dir(fid)` | footage | `rmtree(leftover)` → `delete_prefix` |
| `workspace/footage_match/<conti stem>.json` | Obj | `footage_match(conti_key)` | footage_match, conti.trash_targets | |
| `workspace/trash/<stamp>[-n]/<원래 키>` | Trash | (FileTrash 내부) | conti, draft, videomd | 반환 문자열 형식 유지 |
| `workspace/tmp/*` | Scratch | `scratch.tmp_path(name)` | cuts, stt, captions, library, api_* 9곳 | 임시 핸드오프 JSON |
| `workspace/timings.json`, `workspace/.last_fetch` | Scratch(local) | `scratch.local_path(...)` | timings, fetch | 기계별 통계·전역 속도 제한 |
| `workspace/library/`(git clone), `workspace/library.lock` | Scratch(local) | `scratch.local_path("library")` | library, library_mcp | git이 실제 폴더 필요 |
| `workspace/ui.lock`, `workspace/ui.log` | 밖(UI 프로세스) | — | ui/__main__, ui/server | 허용 목록 |
| 촬영 원본 폴더(사용자 PC 절대 경로) | 밖 | — | footage, footage_out | 사용자 입력 경로, 읽기만 |
| `remotion/`(플러그인 폴더), `templates/`, `skills/` | 밖 | — | captions, 전부 | 작업 폴더가 아님(`PLUGIN_ROOT`) |
| CLI 인자 파일(`--analysis`, `--changes`, `--notes`, `--edits`, conti JSON) | 밖(인자) | `scratch.arg_path(value)` | videomd, similarity, motif, mission, conti, library, shots, draft | 상대면 작업 폴더 기준 해석(지금 동작) |

## 3. 인터페이스 (`scripts/stores.py`)

```python
@dataclass(frozen=True)
class Meta:
    key: str
    size: int
    mtime: float          # os.stat().st_mtime 그대로 (ns/1e9로 다시 만들지 않는다: 마지막 자리 차이)
    mtime_ns: int         # conti_history 캐시 스탬프용

@dataclass(frozen=True)
class Doc:
    key: str
    raw: bytes            # 디스크 바이트 그대로(BOM 포함)
    label: str            # 오류 메시지용 표시 문자열. FileStore = str(root / key) (지금 메시지와 동일)
    @property
    def version(self) -> str: ...        # sha256(raw).hexdigest()
    @cached_property
    def parsed(self) -> tuple[dict, str]: ...   # refstore.parse_md(raw, label). 파싱 오류는 지금과 같은 ScriptError
    @property
    def nl(self) -> str: ...             # raw(BOM 제거)가 b"---\r\n"으로 시작하면 "\r\n" 아니면 "\n"
                                         # 주의: videomd.write는 일부러 Doc.nl을 쓰지 않는다. 지금 규칙은 BOM을 떼지 않고
                                         # raw.startswith(b"---\r\n")로 본다(BOM+CRLF 파일은 LF로 다시 씀). 바이트 보존을 위해 그대로 둔다.

class VersionConflict(Exception):        # 모듈이 자기 ScriptError 코드(conti_changed, ref_changed …)로 바꿔 던진다
    pass

class DocStore(Protocol):                # 키는 반드시 ".md"로 끝난다
    def read(self, key: str) -> Doc: ...                                   # 없으면 FileNotFoundError
    def exists(self, key: str) -> bool: ...
    def stat(self, key: str) -> Meta: ...
    def write(self, key: str, data: bytes, *, expect_version: str | None = None) -> str: ...  # 원자적, 새 version. 내 메모 가드(§4.1) 위반이면 ScriptError("memo_lost")
    def create(self, key: str, data: bytes) -> str: ...                   # 없을 때만 만든다(Task 5 추가). 있으면 FileExistsError, 새 version. File = O_CREAT|O_EXCL
    def list(self, prefix: str, pattern: str = "*.md") -> list[Meta]: ...  # prefix 바로 아래, 순서 = 아래 3.1
    def folders(self, prefix: str, pattern: str) -> list[str]: ...         # "refs/" + "ref_*" → ["refs/ref_a/", …]
    def folder_exists(self, prefix: str) -> bool: ...
    def ensure_folder(self, prefix: str) -> None: ...                      # init_category의 빈 videos/, scaffold
    def label(self) -> str: ...                                            # 화면·status의 "work_root" 표시. File = str(root)
    def local_path(self, key: str) -> ContextManager[Path]: ...            # ctx.local_file이 md 키에 쓴다(claude CLI Read). File = 진짜 경로

class ObjectStore(Protocol):             # 키는 ".md"로 끝나지 않는다
    def get(self, key: str) -> bytes: ...                                  # 없으면 FileNotFoundError
    def put(self, key: str, data: bytes) -> None: ...                      # 원자적(refstore.write_atomic), 부모 폴더 생성
    def exists(self, key: str) -> bool: ...
    def stat(self, key: str) -> Meta: ...
    def list(self, prefix: str, pattern: str = "*") -> list[Meta]: ...     # 파일만, prefix 바로 아래
    def folder_exists(self, prefix: str) -> bool: ...
    def delete(self, key: str) -> None: ...                                # missing_ok
    def delete_prefix(self, prefix: str) -> None: ...                      # rmtree(ignore_errors) 대응
    def local_path(self, key: str) -> ContextManager[Path]: ...            # 읽기용 실제 파일. File = 진짜 경로(복사 없음)
    def write_path(self, key: str) -> ContextManager[Path]: ...            # 외부 도구가 쓸 경로. File = 최종 경로 그대로
    def display_path(self, key: str) -> str: ...                           # abs_html·abs_path 출력용. File = str(root / key)

class SharedCache(ObjectStore, Protocol):  # 키는 workspace/{raw,cuts,frames,shots}/ 아래만(그 밖이면 ValueError)
    pass

class Scratch(Protocol):                 # 로컬 실행 보조. 서버판은 프로세스 임시 폴더, local_path·workdir는 NotImplementedError
    def tmp_path(self, name: str) -> Path: ...        # workspace/tmp/<name>, 부모 생성
    def tmp_file(self, key: str) -> ContextManager[Path]: ...  # keys.tmp(name) 키 → tmp_path(name)(서버판도 동작)
    def local_path(self, *parts: str) -> Path: ...    # workspace/<parts> (library clone·lock, timings, .last_fetch)
    def arg_path(self, value: str | Path) -> Path: ...  # CLI 인자: 절대면 그대로, 상대면 root 기준
    def key_of(self, value: str | Path) -> str: ...   # 절대/상대 경로 → 키. root 밖이면 ScriptError("bad_path")
                                                      # 단, 링크로 옮긴 workspace/·workspace/<이름>/ 아래 작업 폴더 안 경로는 받는다
    def workdir(self) -> Path: ...                    # claude CLI cwd (claude-cli 엔진 전용)

class Trash(Protocol):
    def move(self, keys: list[str], last: str) -> dict: ...  # {"trash": "workspace/trash/<stamp>", "moved": [...]}

class Backend(Protocol):                 # 서버판(S5)이 Ctx(backend=...)로 주입. 사용자별 저장소를 만든다(S5에서 바뀔 수 있음)
    def store(self, user_id: str | None) -> DocStore: ...
    def objects(self, user_id: str | None) -> ObjectStore: ...
    def shared(self, user_id: str | None) -> SharedCache: ...
    def scratch(self, user_id: str | None) -> Scratch: ...
    def trash(self, user_id: str | None) -> Trash: ...
```

- 문서 키 판정은 `keys.is_doc(key)` = `key.lower().endswith(".md")`: Windows glob(`*.md`)이 대소문자를 무시하므로 `Idea.MD`도 문서다.
  `keys.kind`·DocStore/ObjectStore의 키 종류 검사가 모두 이것을 쓴다. `keys.kind`는 `workspace/tmp/`(`keys.TMP`) 아래를
  확장자와 상관없이 `"scratch"`로 본다(임시 키는 `keys.tmp(name)`이 만든다). 종류가 맞지 않는 키(DocStore에 비 md, ObjectStore에 md,
  SharedCache에 공유 접두사 밖)는 `ValueError`(프로그램 오류).

텍스트 쓰기 도우미(`refstore.text_bytes(text, newline=None) -> bytes`): `Path.write_text(text, encoding="utf-8", newline=newline)`과
**같은 바이트**를 만든다(newline None이면 `"\n"` → `os.linesep`). 지금 `write_text`를 쓰는 곳은 모두 이걸 거쳐 `put`한다.

### 3.1 목록 순서·일치 규칙(FileStore 계약)
- `list`/`folders`는 `sorted(Path(root/prefix).glob(pattern))` 순서와 같아야 한다. Windows의 `WindowsPath` 정렬·glob은
  대소문자를 무시하므로 **문자열 정렬로 바꾸지 않는다**. 반환 키는 `p.relative_to(root).as_posix()`.
- `prefix`는 `/`로 끝나고 바로 아래만 본다(재귀 없음). 재귀 glob(`ref_*/videos/<vid>.md`)은 `folders` + `list`로 푼다.
- mtime 정렬이 필요한 곳(videomd.examples, mission, status.recent)은 호출부가 `Meta.mtime`으로 정렬한다.

### 3.2 Ctx 확장 (`scripts/ctx.py`)
```python
@dataclass(frozen=True)
class Ctx:
    root: Path | None = None              # 로컬판만. 서버판 None
    user_id: str | None = None
    backend: Backend | None = field(default=None, compare=False, repr=False)   # 서버판이 주입
    @cached_property
    def store(self) -> DocStore: ...      # backend 없으면 filestore.FileDocStore(root)
    objects: ObjectStore / shared: SharedCache / scratch: Scratch / trash: Trash   # 같은 방식
    def for_key(self, key) -> DocStore | ObjectStore: ...    # keys.kind 한 곳: doc→store, shared→shared, object→objects(scratch는 ValueError)
    def local_file(self, key) -> ContextManager[Path]: ...   # scratch 키는 scratch.tmp_file, 나머지는 for_key(key).local_path

def here() -> Ctx: ...    # current() or Ctx(root=Path(YT_COACH_HOME or cwd))  ← common.work_root의 대체 로직을 이리로 옮김
```
- `common.work_root()`는 `ctx.here().root`를 돌려준다(동작 동일). `cached_property`는 frozen dataclass에서도 `__dict__`에 써서 동작한다.
- 모듈은 `from scripts import ctx` 후 `ctx.here().store.read(keys.index_md(cat))` 형태로만 쓴다.

## 4. 파일 구현 (`scripts/filestore.py`)

- `FileDocStore/FileObjectStore/FileSharedCache/FileScratch/FileTrash(root: Path)`. 모두 `root / key`(키 검증 후)로만 경로를 만든다.
- 키 검증(`keys.check`): 상대, `/` 구분, 빈 조각·`.`·`..`·`\`·`:`·NUL 금지, 점·공백으로 끝나는 조각과 Windows 예약 이름
  (CON·PRN·AUX·NUL·COM1~9·LPT1~9, 확장자·대소문자 무관) 금지. 위반은 `ScriptError("bad_path")`. 폴더 키의 끝 `/` 하나는 허용.
- 읽기: `path.read_bytes()`(옛 `refstore.read_md`와 동일. BOM은 `parse_md`의 `utf-8-sig`가 처리).
- 쓰기: `refstore.write_atomic(path, data)` 재사용(임시 이름 `<name>.tmp`, Windows 재시도). `conti_history`는 지금 임시 이름
  `.<name>.<pid>.<tid>.tmp`를 쓴다 → 동시 쓰기 안전을 위해 `write_atomic`의 임시 이름에 pid·tid를 붙이도록 함께 바꾼다
  (최종 바이트 무관, `glob("*.md")`·`_VERSION_RE`에 걸리지 않음을 테스트).
- `expect_version`: 키별 잠금(파일 절대 경로 해시로 고르는 고정 64개 줄무늬 `threading.Lock`) 안에서 현재 바이트 sha256 비교 → 다르면 `VersionConflict` → 쓰기.
- `local_path`/`write_path`: `root / key`를 그대로 yield(`write_path`는 부모 폴더 생성). 복사·임시 파일 없음.
- `FileTrash.move`: 지금 `common.move_to_trash` 본문을 그대로 옮긴다(키 → `root/key`). 반환 문자열·오류 코드(`bad_path`, `trash_failed`)·
  되돌리기 동작 동일. 폴더 키(`workspace/frames/<vid>`, history 폴더, `-cuts` 폴더)는 끝 `/` 없이 받는다(지금 `moved` 형식).
- `FileDocStore.label()` = `str(root)`, `Doc.label` = `str(root / key)`: 지금 오류 메시지·`status.overview["work_root"]`와 같은 문자열.

### 4.1 내 메모 가드 (단계 15)
`DocStore.write`는 쓰기 잠금 안에서 지금 바이트를 읽어 `expect_version`을 먼저 비교하고(다르면 `VersionConflict`), 이어서
**기존 문서의 `## 내 메모` 내용이 새 바이트에 그대로 있는지** 본다. 아니면 `ScriptError("memo_lost")`를 던지고 아무것도 쓰지 않는다
(임시 파일도 남기지 않는다). 규칙은 바이트만 보는 순수 함수 `refstore.memo_text`·`check_memo`에 있고, 서버판(S5) 저장소도 같은 함수를 쓴다.
- 메모 = 프런트매터 뒤 본문에서 `## 내 메모` 줄(줄 끝 공백 무시) **다음부터 다음 `## ` 제목 줄 전까지**. 제목 줄이 없으면 메모 없음.
  제목 판정은 정확한 줄 일치뿐이다(`## 내 메모장`·`### 내 메모`는 메모가 아니다). 메모를 옮기는 쪽(`videomd.memo_from_body`,
  `similarity._memo`, `mission._memo_tail`)과 메모를 건너뛰는 쪽(`drop_video_techniques`, `motif.parse_techniques`)도
  같은 판정(`refstore.memo_span`·`memo_after`)을 쓴다.
- 비교 전 정규화: BOM 제거, CRLF→LF, 줄마다 끝 공백 제거, 앞뒤 빈 줄 제거. 깨진 UTF-8은 대체 문자로 읽는다(같은 바이트면 같은 결과).
- 기존 메모가 없거나 비어 있으면 검사하지 않는다(새 문서, 메모 칸만 있는 문서). `create`는 기존 문서를 덮지 않으므로 검사가 필요 없다.
- 정규화 범위는 정상 흐름이 메모를 옮기는 방식의 차이에서 정했다(`tests/test_memo_writers.py`가 흐름마다 확인):
  영상 md 다시 분석·콘티 다시 그리기(`memo_from_body`: 앞 줄바꿈 버림, 메모 뒤 절까지 통째로 옮김), 콘티 `render_md`(끝 공백 버림),
  진단·회고 다시 저장(CRLF→LF, 메모부터 끝까지), `update_frontmatter`·`update_stage`·유형화·모티브 카드·별표·기법 합치기·
  미션 제출/건너뛰기·바꾸기(swap)·프로필 반영·다듬기(메모 밖 절만 고침), 기법 목록 갱신(메모 절은 건너뜀). 이 흐름들은 모두 통과한다.
- 로컬판 바이트 변경(의도, §0의 예외): ① 미션 바꾸기(swap)는 기존 `## 내 메모`(와 그 뒤)를 회고 다시 저장처럼 새 미션 끝에 옮긴다
  (전에는 메모가 조용히 사라졌다. 메모가 없으면 바이트는 전과 같다). ② techniques.md 메모 안의 기법 형식 줄은 다시 분석·영상 삭제가
  고치지 않는다(전에는 출처 링크를 뺐다). ③ 메모 제목 판정이 부분 문자열(`find`)에서 정확한 줄 일치로 바뀌어, `## 내 메모장` 같은
  다른 절을 메모로 옮기지 않고, `## 내 메모  `(끝 공백) 제목 아래 메모를 옮길 때 첫 줄 앞 공백 줄이 남지 않는다.

## 5. 저장소 밖에 남는 것과 "직접 호출 0"의 정의

**허용 목록(경로를 직접 다뤄도 되는 모듈)**: `scripts/ctx.py`, `scripts/common.py`(`work_root` 정의, `scrub`의 경로 가리기),
`scripts/filestore.py`, `scripts/keys.py`(문자열만), `scripts/count_work_root.py`, `scripts/ui/server.py`(`/files`·lock),
`scripts/ui/__main__.py`(ui.lock·ui.log·시작).

| 남는 것 | 이유 | 처리 |
|---|---|---|
| `/files/<rel>` 라우트 | 로컬 UI 전용. Range 응답이 파일 핸들을 쓴다 | `req.app.root` 기준 `_contained` + `FILE_DIRS` 그대로. 서버판(S6)은 presigned URL로 대체 |
| `ui.lock`, `ui.log` | UI 프로세스 관리(작업 폴더당 1개) | 허용 목록 안 |
| 촬영 원본 폴더 | 사용자 PC 절대 경로, 읽기만(결정 3: 원본 업로드 안 함) | `Path` 그대로. 처리 결과만 ObjectStore |
| `remotion/` 프로젝트, `templates/` | 플러그인 폴더(`PLUGIN_ROOT`) | 변경 없음 |
| 임시 파일·CLI 인자 파일 | 사용자 데이터 아님 | `ctx.here().scratch` 경유 |
| 프로세스 잠금(`INDEX_LOCK`, `HISTORY_LOCK`, `_RELABEL_LOCK`, `library.lock`) | 동작 그대로 | 모듈에 남김. 서버판은 S5에서 행 잠금 |

**"0"의 기계적 정의(마지막 단계에서 `tests/test_no_direct_paths.py`로 강제)** — 허용 목록 밖 `scripts/**/*.py`에서:
1. `work_root(`, `workspace_dir(`, `category_dir(`, `existing_category_dir(`, `move_to_trash(` 호출 0
   (지금 `count_work_root` 정규식은 `refstore.existing_category_dir(`를 **세지 못한다** → 이름 목록에 추가).
2. AST 문자열 상수(독스트링 제외) 중 작업 폴더 최상위 이름 `refs`, `drafts`, `mine`, `missions`, `reviews`, `reports`,
   `workspace`, `profile.md`, `yt-coach.json`, `library.json`으로 시작하거나 `ref_`/`-conti`로 경로를 조립하는 상수 0 (`keys.py`만 허용).
3. `ctx.root`·`.here().root` 속성 접근 0.
`count_work_root`는 출력에 `"allowed"`(허용 목록 모듈)와 `"outside"`(그 밖 합계)를 나누어 내고, 테스트는 `outside == 0`을 단언한다.

## 6. 이전 순서 (16단계)

공통 안전장치(1단계에서 만든다): `tests/helpers/tree_snapshot.py` — 임시 작업 폴더의 `{상대 경로: sha256}`와 CLI 반환 JSON을
기록. 각 이전 단계는 **먼저 바뀌기 전 코드로 특성(characterization) 테스트를 추가·통과시킨 뒤** 모듈을 바꾸고 같은 테스트를 다시 통과시킨다.
픽스처는 실제 모양(LF·CRLF·BOM·이모지·`## 내 메모`·한글 파일 이름, travel 카테고리 축소본)으로 `tests/fixtures/workfolder/`에 둔다.

| # | 단계 | 건드리는 모듈 | 증명(테스트) | 위험 |
|---|---|---|---|---|
| 1 | `keys.py`(키 함수·`check`·`kind`) + `tree_snapshot` 도우미 + 픽스처 작업 폴더 | 새 파일만 | 키 함수 출력 = 지금 경로의 `relative_to(root).as_posix()`(모듈 함수와 대조), 잘못된 카테고리 → `invalid_category` 동일 메시지 | 없음 |
| 2 | `stores.py`·`filestore.py`·Ctx 확장·`ctx.here()`·`refstore.text_bytes`, `common.work_root`가 `here()` 사용 | ctx, common, refstore(도우미만) | 왕복 바이트 동일(LF/CRLF/BOM md, JSON), `text_bytes` = `write_text` 결과, `list` 순서 = `sorted(glob)`(대소문자 섞은 이름), `Meta.mtime == stat().st_mtime`, 없는 키 → FileNotFoundError, `FileTrash.move` 결과·트리 = `move_to_trash`, `expect_version` 충돌 | Ctx frozen + cached_property, 테스트의 env 전환(ADR-7) |
| 3 | refstore를 키 기반으로: `read(key)`, `update_frontmatter(key)`, `require_category(cat)->prefix`, scaffold/init/find_video/list_categories/update_stage. `read_md`·`update_frontmatter`는 **Path도 받는 과도기 래퍼**(Path → `scratch.key_of`) | refstore | test_refstore·test_scaffold + 스냅샷(init→count→update-stage) | 래퍼가 root 밖 절대 경로를 받는 호출부(테스트)가 있으면 그대로 Path IO로 처리 |
| 4 | 수집 계층 → SharedCache/Scratch: fetch(raw, `.last_fetch`), cuts(cuts json, frames `write_path`, tmp), shots, stt(tmp), timings | 5개 | test_fetch/cuts/shots/stt/timings + raw·cuts·shots JSON 바이트(Windows CRLF) 스냅샷 | `write_text` 줄바꿈(pitfall 1), frames 폴더 정리 순서 |
| 5 | videomd·features·cluster·similarity (영상 md, techniques, typify, mine 진단) + videomd.trash는 `ctx.trash` | 4개 | test_videomd/relabel/features/cluster/similarity + write(재작성·CRLF·메모)·relabel 스냅샷 | `video_md` 재작성 시 nl 판정, relabel 해시 비교 → `expect_version` |
| 6 | motif·draft·polish·report·status | 5개 | test_motif/draft/polish/report/status + `status.overview` JSON 전체 동일(mtime 포함) | status `recent` mtime 정렬, `glob("*-conti.html")` |
| 7 | mission (missions, reviews, profile, mine/drafts 조회, 별표 카드) | 1개 | test_mission + 스냅샷(give/submit/skip/swap/review/profile) | `_missions_between`의 stem 비교 → 키에서 stem 추출 |
| 8 | conti·conti_refs·conti_art (draft/conti md, html·srt `put`, data URI·`asset_url`은 `exists`/`get`, `ref_frame` 검증은 `scratch.key_of`, art `put`) + conti.trash·draft.trash는 `ctx.trash` | 3개(+draft trash) | test_conti/conti_art/conti_refs/md_render + html 바이트(Windows CRLF)·srt CRLF 스냅샷 | 가장 큼(12곳). `_rel`의 "root 밖이면 그대로" 동작 보존, `safe_draft_path`의 resolve 기반 탈출 검사 유지 |
| 9 | conti_history → ObjectStore (버전 JSON, 캐시 스탬프 = `Meta.mtime_ns, size`) | 1개 | test_conti_history + 버전 파일 바이트(indent=1, LF) | 캐시 키를 Path 문자열 → 키로 바꿀 때 무효화 조건 동일 유지 |
| 10 | footage·footage_match·footage_out·captions (`write_path`로 jpg·mp4, `delete_prefix`, 원본 폴더는 밖) | 4개 | test_footage/footage_match/footage_out/captions/remotion_timing | PyAV·Remotion이 쓰는 경로가 최종 경로 그대로인지 |
| 11 | llm·claude_cli·llm_anthropic(`files`=키 목록, `ctx.local_file`로 경로, cwd=`scratch.workdir()`), library·library_mcp(config `objects`, clone·lock `scratch.local_path`, preview tmp) | 5개 | test_llm/claude_cli/library/library_mcp + 프롬프트 문자열 동일(파일 목록이 상대 경로로 표시) | claude CLI Read가 상대 경로로 읽으므로 cwd 유지 필수 |
| 12 | UI API A: api_refs·api_add·api_typify·api_motif·api_library·api_help | 6개 | test_ui_refs/add/typify/motif/library/help + 응답 JSON 동일 | tmp 핸드오프 파일, `md`/`hash` 응답 형식 |
| 13 | UI API B: api_draft·api_conti·api_diagnose·api_mission·api_report·api_footage·jobs, `server.py`(`/files`는 그대로 허용 목록) | 7개 | test_ui_draft/conti/diagnose/mission/report/footage/server/jobs_queue | `api_diagnose` 허용 폴더 검사(resolve) → 키 접두사 검사로 바꿀 때 우회 경로 회귀 테스트 |
| 14 | 휴지통 정리: 남은 `move_to_trash` 호출 제거, `common.move_to_trash` 삭제(본문은 FileTrash에만) | common, 잔여 호출부 | test_common의 trash 테스트를 FileTrash 대상으로 옮김, 스냅샷 동일 | 없음(2단계에서 같은 동작 증명) |
| 15 | 과도기 래퍼 제거 + 내 메모 가드: refstore Path 래퍼·`existing_category_dir` 삭제, `FileDocStore.write`에 "기존 `## 내 메모` 내용(LF 정규화·끝 공백 제거)이 새 바이트에 그대로 있어야 함" 검사(`memo_lost`) | refstore, filestore | 전체 테스트 + 메모 보존 위반 테스트(일부러 메모를 빠뜨린 쓰기 → 실패) | 정상 흐름이 메모를 정규화해 가드에 걸리는 경우: 테스트로 먼저 찾아 정규화 규칙 확정 |
| 16 | 게이트: `count_work_root` 이름 목록 확장·허용 목록 출력, `tests/test_no_direct_paths.py`(5절 1~3) 추가, 상위 설계 §2.1 키 예시·§6 S3 행 갱신 | count_work_root, tests, docs | `outside == 0` | 독스트링·주석 오탐 → AST로 독스트링 제외 |

각 단계 공통 완료 조건: `pytest tests/` 전부 통과, `python -m scripts.count_work_root`의 `total.all`이 단계 전보다 줄거나 같음
(단계 2는 common 정의 이동만), 바뀐 모듈의 특성 테스트가 이전 코드에서도 통과했다는 커밋 순서(테스트 커밋 → 이전 커밋).

## 7. 함정(코드에서 확인한 것)

1. **`write_text` 기본 줄바꿈**: `conti._write_html`, `cuts.py:421`, `fetch.py:263`, `shots.py:134`, `cluster.py:283/440`,
   `timings.py:95`, ui tmp 파일이 `newline` 없이 `write_text` → Windows에서 `\r\n`. `data.encode()`로 바꾸면 바이트가 달라진다.
   → 반드시 `refstore.text_bytes(text)`(os.linesep 변환). 반대로 conti_history(`newline="\n"`), footage(`newline="\n"`),
   srt(CRLF 수동), md 쓰기(`write_bytes`/`write_atomic`)는 변환하면 안 된다.
2. **BOM 처리 차이**: `refstore.update_frontmatter`는 읽을 때 BOM을 버리고 BOM 없이 다시 쓴다. `polish`는 BOM을 되살린다.
   저장소는 바이트만 받으므로 각 모듈의 현재 동작이 그대로 남는다 — 저장소에서 "BOM 보존"을 일괄 적용하지 않는다.
3. **경로 문자열이 데이터 안에 저장됨**: 콘티 fm `draft`·`html`·`cuts[].ref_frame`·`ref_auto.frame`·`art.file`, cuts json
   `scenes[].frame`(`workspace/frames/<vid>/…`), mine `target`, conti_history `state.cuts[].ref_frame/art`. ADR-1로 키 = 이 문자열.
   `conti.art_of`의 `ART_DIR` 접두사·`..` 검사, `status._norm_target`의 `\`→`/`·`./` 정규화는 그대로 둔다.
4. **API JSON의 상대 경로 → `/files/<rel>`**: 프레임·그림·HTML·리포트 URL을 UI가 `"/files/" + rel`로 만든다. `rel`은 키와 같아야 하고
   `FILE_DIRS`(`workspace/frames`, `workspace/conti-art`, `drafts`, `reports`)는 바꾸지 않는다. `asset_url`의 `posixpath.relpath`·
   퍼센트 인코딩도 그대로.
5. **절대 경로가 출력에 나감**: `abs_html`(conti), `abs_path`(report), `status.overview["work_root"]`, `scaffold["root"]`, 오류 메시지의
   `{path}:` 접두사(`parse_md`). → `display_path`·`label`이 지금과 **같은 문자열**(`str(root / key)`, resolve 안 함)을 낸다.
6. **resolve 기반 탈출 검사**: `conti.safe_draft_path`, `conti._check_edit`(ref_frame), `api_diagnose`(허용 폴더), `move_to_trash`가
   `resolve().relative_to(root.resolve())`로 막는다. 키로 바꿀 때 `scratch.key_of`가 같은 resolve 검사를 하고, `..`·심볼릭 링크 우회
   회귀 테스트를 남긴다.
7. **Path가 모듈 경계를 넘음**: `videomd.write(analysis_path)`, `shots.apply(tmp)`, `draft.save(cat, path)`, `similarity.save(…, notes)`,
   `conti.save(draft, conti_json)`, `conti.parse_draft(path)`, `conti_history.*(conti_path: str|Path)`, `footage_match.state_path(conti_path)`,
   `conti.trash_targets(path)`, `status._video(path)`. → 문서 인자는 **키(str)** 로, 핸드오프 JSON 인자는 S3에서는 Path 유지
   (`scratch.tmp_path`/`arg_path`), 서버판에서 dict 인자로 바꾸는 것은 S6. `str|Path` 혼용 인자는 진입 시 `scratch.key_of`로 한 번만 변환.
8. **stem 기반 키**: conti_history·footage_match·conti-art 폴더는 `Path(conti).stem`만 쓴다(drafts 밖 콘티가 없다는 가정).
   `keys.history_dir(conti_key)`도 stem만 써서 같은 폴더를 가리켜야 한다.
9. **glob 의미**: `refs.glob("ref_*")`는 폴더만 고르려고 `is_dir()`를 붙이고, `videos.glob("*.md")`는 Windows에서 대소문자 무시.
   `status`는 `*.md`에서 `-conti`를 직접 거른다. `mission._drafts`는 이름 역순, `videomd.examples`·`mission._diagnosis_gaps`는 mtime 역순.
   → 3.1 계약을 그대로 지키고, 정렬은 호출부에 남긴다.
10. **mtime 의미**: `api_refs` "added", `api_diagnose` 날짜, `api_conti._stale`(산출물이 md보다 오래됐는지), `api_mission` tmp 정리,
    `status.recent`가 `st_mtime`을 쓴다. 원자적 교체로 바뀌어도 mtime은 쓰기 시각이라 같다. `Meta.mtime`은 `st_mtime` 그대로.
11. **캐시**: `conti_history._cache` 키가 `str(path)`이고 스탬프가 `(st_mtime_ns, st_size)`. 키로 바꾸면 root가 다른 두 작업 폴더가
    같은 키를 공유한다 → 캐시 키를 `(store 식별자=label(), key)`로.
12. **측정 누락**: `count_work_root` 정규식은 `refstore.existing_category_dir(`(약 40곳)·`conti._resolve`·`_rel` 계열·`req.app.root`를
    세지 않는다. 지금 "146곳"은 하한이다. 5절의 AST 게이트가 진짜 완료 조건이다.
13. **원자적 쓰기의 임시 이름**: `write_atomic`은 `<name>.tmp` 고정이라 같은 파일을 두 스레드가 동시에 쓰면 서로의 임시 파일을 덮는다.
    지금도 같은 위험이지만 쓰기가 모두 이 경로로 모이므로 2단계에서 pid·tid를 붙인다.
