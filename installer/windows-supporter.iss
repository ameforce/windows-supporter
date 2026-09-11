#ifndef AppVersion
#define AppVersion "0.0.0"
#endif

#ifndef SourceExe
#define SourceExe "..\dist\windows-supporter.exe"
#endif

#define AppName "Windows Supporter"
#define AppExeName "windows-supporter.exe"

[Setup]
AppId={{A9C8B7B5-4FA7-4B55-9D9E-3A0C8BB71D32}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=ePapyrus
DefaultDirName={localappdata}\Programs\Windows Supporter
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=.
OutputBaseFilename=WindowsSupporter-v{#AppVersion}-Setup
SetupIconFile=..\src\utils\windows_supporter.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; The app uses a hidden Tk root and frozen multiprocessing workers. A normal
; Restart Manager close only sends a graceful close request, which leaves
; worker processes holding the executable open. Force-close is limited to the
; executable listed below and the updater also performs an exact-path preflight.
CloseApplications=force
RestartApplications=no

[Files]
; Do not defer replacement to reboot. The updater verifies the installed hash
; and version immediately; a locked file must fail the install instead.
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 바로 가기 만들기"; GroupDescription: "추가 아이콘:"

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Windows Supporter"; ValueData: """{app}\{#AppExeName}"""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{#AppName} 실행"; Flags: nowait postinstall skipifsilent
