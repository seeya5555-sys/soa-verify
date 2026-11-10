---
name: soa-verify
description: SVMS SOA 1단계 검증(인보이스 금액 대조)을 사용자가 지정한 선박코드에 대해 실행하고, 라인 체크박스·리젝 리마크를 SVMS에 저장할 때. "SOA 검증", "SOA 1단계", "soa verify" 요청 시.
---

# soa-verify (SOA 1단계 검증)

판정은 이 저장소의 엔진(`soa_verify/`)이 한다. **너(Claude Code)가 PDF를 읽고 직접 판정하지 마라.** 직접 판정하면 결과가 사람마다 달라지고 안전장치가 빠진다.

## 순서
1. 처음이면 `python3 -m soa_verify setup` 을 사용자가 직접 실행하게 안내(아이디/비번은 사용자가 터미널에 입력, 너는 값을 묻거나 출력하지 않는다).
   그다음 `python3 -m soa_verify doctor` 로 환경을 점검한다. `=== ready ===`가 아니면 NG 항목(설치 안내가 같이 나옴)을 사용자에게 그대로 전달하고, 해결되기 전에는 verify/stage2를 실행하지 않는다.
2. 대상 확인: 검증할 **선박코드(4자리)** 를 사용자에게 받는다. 선박 목록을 조회해 대신 고르거나, 말하지 않은 선박을 추가하지 마라. My Vessel 전체·선주 단위 일괄 실행 기능은 없다.
3. 미리보기(기본): `python3 -m soa_verify verify --vessel ABCD,EFGH`
   - 기간: `--from YYYYMM --to YYYYMM` (생략 시 올해 01~12), 부서: `--dept 05`(Technical, 기본) / `04`(Crew)
   - **실행 시간이 길다**(라인마다 판정, 선박 여러 척이면 10~20분). **한 번만 실행**하고, Bash timeout을 최대(600000ms)로 두거나 백그라운드로 돌린 뒤 완료 알림을 기다려라. 끝나기 전에 같은 명령을 다시 실행하지 마라(중복 실행=시간·구독 사용량 낭비).
   - 출력이 길면 `... > soa_result.txt 2>&1` 로 파일에 남기고 그 파일을 읽어 요약.
4. 결과 요약: 선박별 CONFIRM / REJECT? / HUMAN 건수와 사유를 그대로 보여준다. 숫자·사유를 지어내지 마라.
5. **저장은 사용자가 "저장해"라고 명시했을 때만**: 같은 인자에 `--save` 추가. 저장 전 "SVMS에 체크박스·리젝 리마크가 실제로 기입됩니다" 확인.
6. 저장 후 합계(confirm 체크 수 · 리젝 리마크 수)를 보고하고, **SOA 승인(Approval)은 사용자가 SVMS에서 직접** 한다고 안내.

## 실패했을 때
- `required program(s) not found` → 설치 안내대로 설치하게 한 뒤 `doctor` 재실행. 우회(`SOA_VERIFY_SKIP_PREFLIGHT`)를 쓰지 마라.
- `soa-verify does not call …` / `does not write …` → 엔진과 `svms.py` 허용 목록이 어긋난 것. 직접 고치지 말고 최신 버전으로 `git pull` 하게 안내, 그래도 나오면 upstream 이슈로.
- 2차 검증의 "확인필요" → 조회 실패로 아무것도 기입하지 않은 상태. 출력의 사유를 그대로 전달하고 재실행은 사용자 확인 후 1회만.
- `LOGIN FAILED` → 아이디/비밀번호 문제. `setup`을 사용자가 다시 실행하게 안내.

## 금지
- 엔진 파일(`soa_amount.py`, `soa_review.py`, `soa_direct_gate.py`, `llm_router.py`, `soa_stage2.py`)과 `svms.py` 허용 목록 수정 금지 — upstream과 판정이 달라진다. 개선은 upstream 이슈로.
- 승인·전표상신·리젝 제출·메일 발송 코드를 추가하지 마라(이 프로젝트 범위 밖).
- 비밀번호·쿠키·PDF 원문을 대화나 외부로 내보내지 마라.

## 판정 규칙 요약
- 금액: 라인 금액이 첨부 인보이스 본문(텍스트/OCR)에 있고, 경량 모델(Claude Haiku 5.5, 로컬 `claude` CLI)이 반증하지 않으면 CONFIRM. 고액(≥ USD 5,000)은 선박명/IMO 본문 확인 필요.
- 파일명 금액만으로는 통과하지 않음. 첨부 없음·본문 미확인 → REJECT? (금액성 사유만 리젝 리마크 저장, 그 외는 보고만)
- 스캔 판독 불가 → HUMAN.


## 2차 검증 (stage2)
"2차 검증 돌려줘" 요청 시: `python3 -m soa_verify stage2 --vessel <CODE> --from <YYYYMM> --to <YYYYMM> --dept 05` (미리보기).
사용자가 저장을 확인하면 같은 명령에 `--save`. 한 번만 실행하고 끝날 때까지 기다린다. 판정은 엔진 출력만 그대로 전달한다.
