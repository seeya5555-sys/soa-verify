# soa-verify — SVMS SOA 1단계 검증

SVMS의 **SOA(Statement of Account) 라인 금액을 첨부 인보이스와 대조**해서, 라인별로 confirm 체크 또는 리젝 체크+리마크를 달아주는 도구입니다. 각자 PC의 Claude Code에서 실행합니다.

- 대상: 로그인한 사용자의 **My Vessel** 선박 전체 — 선주(OW_COMP_ID)별 또는 선박별 선택
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
```

## 사용
```bash
python3 -m soa_verify owners                          # 선주별 선박 목록
python3 -m soa_verify verify --owner 037              # 미리보기(DRY, 기입 없음)
python3 -m soa_verify verify --vessel ABCD --from 202601 --to 202606
python3 -m soa_verify verify --owner 037 --save       # SVMS에 체크박스·리젝 리마크 저장
```
Claude Code에서는 저장소를 연 뒤 "SOA 1단계 검증 돌려줘"라고 하면 `.claude/skills/soa-verify` 스킬이 위 명령을 실행합니다.

## 판정 규칙(요약)
| 라인 | 규칙 |
|---|---|
| Asset | confirm |
| AOR | 연동 AOR 승인액 일치 → 첨부 파일명 금액 → 본문 판독, 모두 실패 시 리젝 후보 |
| Technical OPEX | 첨부 없음=리젝 후보 · 감독출장비 소액+N2F=confirm · 본문(텍스트/OCR) 금액 + Haiku 판독 · 다중 인보이스 합산 · 판독 불가=사람 |
| Crew(04) | 선주 037 OPEX=confirm(upstream 규칙) · 그 외 선주=사람 검토 · 선주 001 Crew=제외 |

- 고액(USD 5,000 이상)은 선박명/IMO가 본문에 있어야 confirm.
- 리젝 리마크는 금액 불일치 사유일 때만 저장(`Cost no match: …`), 그 외 사유는 보고만.
- 관리사가 재상신한 리젝 라인(S/M 체크+리마크, 금액 사유)은 금액을 다시 검증합니다.

## 주의
- 모든 저장은 본인 SVMS 계정 권한으로 이뤄집니다. 저장 전 미리보기로 결과를 확인하세요.
- Claude CLI 호출은 본인 구독 사용량을 소모합니다(라인당 1회 내외).
- 엔진 파일은 upstream 동기화 대상입니다. 수정 제안은 이슈로 남겨주세요.

License: MIT
