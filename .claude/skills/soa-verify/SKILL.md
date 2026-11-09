---
name: soa-verify
description: SVMS SOA 1단계 검증(인보이스 금액 대조)을 내 My Vessel 선박에 대해 실행하고, 라인 체크박스·리젝 리마크를 SVMS에 저장할 때. "SOA 검증", "SOA 1단계", "soa verify" 요청 시.
---

# soa-verify (SOA 1단계 검증)

판정은 이 저장소의 엔진(`soa_verify/`)이 한다. **너(Claude Code)가 PDF를 읽고 직접 판정하지 마라.** 직접 판정하면 결과가 사람마다 달라지고 안전장치가 빠진다.

## 순서
1. 처음이면 `python3 -m soa_verify setup` 을 사용자가 직접 실행하게 안내(아이디/비번은 사용자가 터미널에 입력, 너는 값을 묻거나 출력하지 않는다).
2. 대상 확인: `python3 -m soa_verify owners` → 선주코드·선박 목록을 보여주고 어떤 선주/선박을 검증할지 확인.
3. 미리보기(기본): `python3 -m soa_verify verify --owner <코드>` 또는 `--vessel ABCD,EFGH` 또는 `--all`
   - 기간: `--from YYYYMM --to YYYYMM` (기본 당해 01~12), 부서: `--dept 05`(Technical, 기본) / `04`(Crew)
4. 결과 요약: 선박별 CONFIRM / REJECT? / HUMAN 건수와 사유를 그대로 보여준다. 숫자·사유를 지어내지 마라.
5. **저장은 사용자가 "저장해"라고 명시했을 때만**: 같은 인자에 `--save` 추가. 저장 전 "SVMS에 체크박스·리젝 리마크가 실제로 기입됩니다" 확인.
6. 저장 후 합계(confirm 체크 수 · 리젝 리마크 수)를 보고하고, **SOA 승인(Approval)은 사용자가 SVMS에서 직접** 한다고 안내.

## 금지
- 엔진 파일(`soa_amount.py`, `soa_review.py`, `soa_direct_gate.py`, `llm_router.py`) 수정 금지 — upstream과 판정이 달라진다. 개선은 upstream 이슈로.
- 승인·전표상신·리젝 제출·메일 발송 코드를 추가하지 마라(이 프로젝트 범위 밖).
- 비밀번호·쿠키·PDF 원문을 대화나 외부로 내보내지 마라.

## 판정 규칙 요약
- 금액: 라인 금액이 첨부 인보이스 본문(텍스트/OCR)에 있고, 경량 모델(Claude Haiku 5.5, 로컬 `claude` CLI)이 반증하지 않으면 CONFIRM. 고액(≥ USD 5,000)은 선박명/IMO 본문 확인 필요.
- 파일명 금액만으로는 통과하지 않음. 첨부 없음·본문 미확인 → REJECT? (금액성 사유만 리젝 리마크 저장, 그 외는 보고만)
- 스캔 판독 불가 → HUMAN.
