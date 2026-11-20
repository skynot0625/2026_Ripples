# 파워플랜트 원본 자료 (구현 전 · runtime geometry 없음)

- 출처: 공개 Notion 페이지 "파워플랜트 공간 소개 페이지" https://functional-principle-ecb.notion.site/811f6c11049d40dfbdcb3fe6e9542a3d
- 받은 날: 2026-10-14 (페이지의 첨부 블록에서 Notion 공개 다운로드 경로로 받음)

| 파일 | 원래 이름 | 크기 | sha256 |
| --- | --- | --- | --- |
| `source/230224_SNU_Powerplant.skp` | 230224_SNU_Powerplant.skp | 96,888,315 B (SketchUp 2023, 23.1.341) | 228bf1e6df129d28b64f9d495df0eb07e5250e71e5e1d9976ce2a0e69896302b |
| `source/plan_68.pdf` | (도면)관악 68동[제1파워플랜트].pdf | 374,920 B (2쪽: 지상1층 2022-12-16, 지상2층 2023-02-13) | 55e51e86761470cbfab2d6e28878b234ca3161ec7d79838bf2cb8c464ef2fba9 |

- PDF: 시설관리용 평면(벡터, 실명·면적 텍스트 있음, 격자 7,500). 1층 120호 "문화공작소" 351.85㎡(문화예술원), 113호 복도 982.65㎡ 등. 높이·천장 정보 없음.
- SKP: 독자 바이너리(내부 zip의 `model.dat` 약 263MB). 무료·오픈소스 CLI 변환기를 확보하지 못함. Cinema 4D c4dpy 경로는 라이선스가 없어 폐기(재시도하지 않음). 억지로 파싱하거나 추정하지 않는다.
- 상태: runtime geometry(`geometry.json`) 없음, `--venue powerplant` 선택지 없음. 다음 단계(사용자 지시 후): PDF에서 검증 가능한 평면까지만, SKP 전용 3D 구조·천장·높이는 미확인으로 표시.
