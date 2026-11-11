# soa-verify — SVMS SOA 1단계 검증

SVMS의 **SOA(Statement of Account) 라인 금액을 첨부 인보이스와 대조**해서, 라인별로 confirm 체크 또는 리젝 체크+리마크를 달아주는 도구입니다. 각자 PC의 Claude Code에서 실행합니다.

- 대상: **`--vessel`로 직접 지정한 선박코드**만 (여러 척은 쉼표로). My Vessel 목록에서 자동으로 고르는 기능은 없습니다.
- 판정: 코드(텍스트/OCR 금액 대조) + Claude **Haiku 5.5**(본인 `claude` CLI 로그인 사용)
- 범위: **체크박스·리마크 저장까지만.** SOA 승인, 전표상신, 리젝 제출, 메일 발송 기능은 없습니다(코드 자체가 없음).
- 판정 엔진은 운영 중인 upstream과 같은 코드입니다(`UPSTREAM` 파일에 동기화 커밋 기록, 내보내기 때 구문(AST) 동일성 검사).

## 설치
필요: Python 3.10+, `poppler`(pdftotext/pdftoppm), `tesseract`, Claude Code CLI(`claude`, 본인 계정 로그인)

```bash
# macOS
brew install poppler tesseract
# Windows: poppler·tesseract 설치 후 PATH 등록 (WSL 사용 권장)

git clone https://github.com/seeya5555-sys/soa-verify && cd soa-verify
python3 -m unittest discover -s tests      # selftest (네트워크·AI 호출 없음)
python3 -m soa_verify setup                # SVMS 아이디/비번 → ~/.soa-verify/config.json (권한 600, 업로드 안 함)
python3 -m soa_verify doctor               # 환경 점검: Python·poppler·tesseract·claude·SVMS 로그인 (기입 없음)
```
- `doctor`가 `=== ready ===`를 내면 준비 끝입니다. poppler·tesseract·claude 중 하나라도 없으면 `verify`/`stage2`는 **실행 자체를 멈춥니다**(인보이스 본문을 못 읽은 채로 정상 라인이 리젝 후보로 나오는 것을 막기 위함).
- 기간(`--from/--to`)을 생략하면 **올해 1월~12월**입니다.

## 사용
```bash
python3 -m soa_verify verify --vessel ABCD,EFGH        # 미리보기(DRY, 기입 없음)
python3 -m soa_verify verify --vessel ABCD --from 202601 --to 202606
python3 -m soa_verify verify --vessel ABCD --save      # SVMS에 체크박스·리젝 리마크 저장
```
Claude Code에서는 저장소를 연 뒤 "SOA 1단계 검증 돌려줘"라고 하면 `.claude/skills/soa-verify` 스킬이 위 명령을 실행합니다.

## 2차 검증 (1단계 이후)
1단계로 금액 대조를 끝낸 뒤 실행합니다. 현재 체크 항목:
- **Operational AOR**: Tech(05)/Crew(04) SOA 라인의 AOR Req No를 비용청구 AOR 원장에서 조회해 Dept가 Operational이면 리젝 체크 + 리마크
  `Operational AOR: <AOR No> (Dept: Operational) - not chargeable to Technical SOA. Pls move to Operational SOA.`
- **외화 환율 과청구**: Local Currency가 USD가 아닌 라인. 기준 USD = Local Amount × 기준환율(SVMS 환율표 교차, 없으면 하나은행 당일 매매기준율 교차). 청구 USD가 기준의 120% 이상이면 리젝 체크 + `FX over-charged: ...` 리마크(낮은 건 무관)
- **관리비 SMA 초과**: 비용코드 080101 라인의 SOA 합계가 계약(일 단가 × 해당 월 일수 또는 월 고정액)보다 $1 넘게 높으면 리젝 + `Mgmt fee over SMA: ...`. 계약표는 저장소에 내장(`soa_verify/soa_mgmt_fee.json`, 선박명 기준 184척) — 받으면 바로 사용. 다른 표를 쓰려면 `SOA_MGMT_FEE_DB` 경로 지정. 표에 없는 선박은 건너뜀(경고)
- **NRC 인보이스**: 첨부 PDF 본문에 National Response Corporation / OPA 90 Service Invoice가 있으면 리젝 + 리마크 `NRC invoice`. 1단계가 남긴 본문 텍스트 캐시를 재사용(PDF 재추출 없음)
- **중복 인보이스**: Debit 라인의 인보이스 번호(점·대시·슬래시 등 기호 무시) + 통화 + 금액이 같은 선박의 먼저 만들어진 다른 SOA(Draft 제외, ±24개월)에 있으면 리젝 + `Duplicate invoice: <번호> = <원본 SOA> s<라인>`. 원본은 건드리지 않음. **비용청구(Fund Request)는 비교하지 않음** — Fund Request는 송금 서류이고 SOA는 그 비용의 기록이라, SOA에 등록된 적 없는 인보이스는 중복이 아님. 관리비(080101)·통신비(0601*) 제외(`SOA_DUP_EXCLUDE_EXP`로 추가). 같은 선박 안에서만
- **AOR을 OPEX로 상신**: CAT-II가 OPEX인데 첨부 본문에 AOR 표식("Agreed Overrun Expenses")과 같은 선박의 AOR 번호가 있고, 그 번호가 SVMS AOR 원장에 실제로 있으면 리젝 + `AOR filed as OPEX: AOR doc <AOR No> attached - pls change Cat-II to AOR and enter AOR Req No`. 문구만 있고 원장에 없는 번호면 걸지 않음
- **AOR 인보이스 번호**: AOR 건(CAT-II=AOR 또는 위 항목에 걸린 라인)인데 SOA Invoice No가 AOR 커버페이지의 Ref 번호이거나 공란이면 리젝 + `AOR invoice no: SOA <번호> = AOR cover page Ref / vendor invoice <업체 번호 | no. required>`. 커버 Ref는 AOR 표식이 있는 페이지에서만 읽고, 업체 인보이스 번호는 커버페이지 밖에서 찾음(코드 우선, 못 찾으면 Haiku 보조 — 본문 실재·커버 Ref·AOR 번호 아님을 재확인). `SOA_STAGE2_AORDOC=shadow`면 판정만 출력하고 기입 안 함, `0`이면 끔
- 1단계가 단 리젝 리마크(`Cost no match …`)가 있으면 덮어쓰지 않고 2단계 사유를 ` | `로 덧붙임. 관리사·사람이 쓴 리마크는 보존
- 리젝 사유가 여러 개면 리마크에 전부 ` | `로 이어서 남김(NRC → 중복 → AOR을 OPEX로 → AOR 인보이스 번호 → Operational AOR → 관리비 → 환율 순, 240자 넘으면 숫자 유지 축약)
- AOR·환율 조회 실패가 1건이라도 있으면 아무것도 기입하지 않고 "확인필요"로만 보고합니다. 이미 리젝된 라인·관리사 재상신 라인의 리마크는 덮어쓰지 않습니다.
```bash
python3 -m soa_verify stage2 --vessel ABCD --from 202601 --to 202612 --dept 05          # 미리보기
python3 -m soa_verify stage2 --vessel ABCD --from 202601 --to 202612 --dept 05 --save   # 리젝 체크 + 리마크 저장
```

## 리마크정리
```bash
python3 -m soa_verify cleanup --vessel ABCD --from 202601 --to 202612          # 미리보기
python3 -m soa_verify cleanup --vessel ABCD --from 202601 --to 202612 --save   # confirm 라인에 남은 리젝 리마크 삭제
```

## 내 웹사이트에 붙이기 (선택)
Flask 사이트가 있으면 아래 2줄로 같은 입력 양식(선박코드·시작·끝·부서·검증 실기입·리마크정리)을 붙일 수 있습니다.
```python
from soa_verify.web import create_blueprint
app.register_blueprint(create_blueprint(), url_prefix="/soa-verify")   # 반드시 내 사이트 로그인 뒤에 둘 것
```
- SVMS 아이디/비밀번호는 페이지에서 처음 한 번 입력하면 서버 PC에 저장(`~/.soa-verify/config.json`, 권한 600)
- 판정 모델은 Haiku 5.5 고정(선택 없음). 서버 PC에 `claude` CLI가 로그인돼 있어야 합니다.
- 이 블루프린트 자체에는 로그인 기능이 없습니다. 내 사이트 인증 아래에 마운트하세요.
- 저장소 폴더가 내 사이트의 import 경로에 있어야 합니다. 다른 위치에 받았다면 앱 시작 부분에 `sys.path.insert(0, "/받은/경로/soa-verify")` 한 줄을 넣으세요. 실행 로그·잠금 파일은 그 폴더의 `_cache/`에 생기므로 쓰기 권한이 필요합니다.
- 웹 서버를 돌리는 계정의 PATH에 poppler·tesseract·claude가 있어야 합니다(서비스로 띄우면 PATH가 터미널과 다를 수 있음 — 그 계정으로 `python3 -m soa_verify doctor` 확인).
- 실행은 한 번에 하나만 됩니다(파일 잠금 — 워커가 여러 개여도 동일). Windows 잠금 경로는 실제 Windows에서 시험하지 못했습니다(WSL 권장).

## 리젝 리마크 형식
`Cost no match - Amount: SOA USD 164.41 / Invoice 109.71 | Invoice No.: SOA INV-123 - not found on invoice`
- 금액: 인보이스에 실제로 적힌 다른 금액이 있을 때만 표시, 없으면 `not confirmed on invoice`
- 인보이스 번호: `OK` / `not found on invoice` / `blank in SOA` / `not verified (unreadable scan)`
- 리젝 판정 기준은 금액(인보이스 번호는 리마크 정보로만 사용)

## 판정 규칙(요약)
| 라인 | 규칙 |
|---|---|
| Asset | confirm |
| AOR | 연동 AOR 승인액 일치 → 첨부 파일명 금액 → 본문 판독, 모두 실패 시 리젝 후보 |
| Technical OPEX | 첨부 없음=리젝 후보 · 감독출장비 소액+N2F=confirm · 본문(텍스트/OCR) 금액 + Haiku 판독 · 다중 인보이스 합산 · 판독 불가=사람 |
| Crew(04) | 선주 037=Technical과 동일(첨부 본문 금액 검증) · 그 외 선주=사람 검토 · 선주 001 Crew=제외 |

- 고액(USD 5,000 이상)은 선박명/IMO가 본문에 있어야 confirm.
- 리젝 리마크는 금액 불일치 사유일 때만 저장(`Cost no match: …`), 그 외 사유는 보고만.
- 관리사가 재상신한 리젝 라인(S/M 체크+리마크, 금액 사유)은 금액을 다시 검증합니다.

## 주의
- 모든 저장은 본인 SVMS 계정 권한으로 이뤄집니다. 저장 전 미리보기로 결과를 확인하세요.
- Claude CLI 호출은 본인 구독 사용량을 소모합니다(라인당 1회 내외).
- 엔진 파일은 upstream 동기화 대상입니다. 수정 제안은 이슈로 남겨주세요.

License: MIT
