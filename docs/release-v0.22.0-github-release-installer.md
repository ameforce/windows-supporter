# v0.22.0 GitHub Release installer 업데이트

## 변경 계약

이 릴리즈부터 Windows Supporter 자동 업데이트는 로컬 Git checkout의 `fetch`, `switch`, `reset`, `build.bat`에 의존하지 않는다. 앱은 GitHub의 `ameforce/windows-supporter` 최신 Release를 조회하고, 다음 두 asset을 함께 검증한다.

- `WindowsSupporter-vX.Y.Z-Setup.exe`
- `WindowsSupporter-vX.Y.Z-Setup.exe.sha256`

installer를 임시 경로에 다운로드한 뒤 SHA-256을 확인하고, 검증이 끝난 installer만 현재 설치 경로에 `/VERYSILENT /CLOSEAPPLICATIONS`로 적용한다. 설치 실패 시 기존 실행 파일 백업을 복원하고 앱을 다시 시작한다.

GitHub Actions workflow는 추가하지 않는다. 빌드, asset 업로드, Release read-back은 로컬 릴리즈 절차와 `gh` CLI 증거가 소유한다.

## 릴리즈 asset 생성

clean tagged worktree에서 다음을 실행한다.

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_installer.ps1 `
  -Version 0.22.0 `
  -OutputDirectory "$env:TEMP\windows-supporter-installer-v0.22.0"
```

스크립트는 PyInstaller artifact-only build를 수행하고 Inno Setup installer 및 SHA-256 sidecar를 만든 뒤, 저장소 안의 `build`, `dist`, `windows-supporter.spec`를 정리한다.

## GitHub Release 게시

tag와 main merge, clean tagged artifact 검증이 끝난 뒤 installer와 sidecar를 같은 Release에 업로드한다.

```powershell
$assetDir = "$env:TEMP\windows-supporter-installer-v0.22.0"
gh release create v0.22.0 `
  "$assetDir\WindowsSupporter-v0.22.0-Setup.exe" `
  "$assetDir\WindowsSupporter-v0.22.0-Setup.exe.sha256" `
  --repo ameforce/windows-supporter `
  --title "Windows Supporter v0.22.0" `
  --notes-file docs/release-v0.22.0-github-release-installer.md
```

게시 후에는 다음으로 `tagName`, `isDraft`, `isPrerelease`, asset 이름, asset URL을 다시 읽고, installer SHA-256과 sidecar가 일치하는지 확인한다.
