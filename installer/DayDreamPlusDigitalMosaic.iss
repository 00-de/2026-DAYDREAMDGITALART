; DayDream Plus デジタルモザイク　インストーラー設定（Inno Setup 6）
; バージョン番号は自動ビルドのとき /DAppVersion=0.2.15 のように渡される

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "DayDream Plus デジタルモザイク"
#define AppExe  "DayDreamPlusDigitalMosaic.exe"

[Setup]
; AppId はアプリの「背番号」。上書き更新・アンインストールの判定に使うので絶対に変えない
AppId={{8E3A1C52-6B4D-4F7A-9C21-5D0E7B9A4F13}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=DayDream AI株式会社
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
; 管理者権限なしで、ユーザー自身のフォルダー（%LOCALAPPDATA%\Programs）に入れる
; → 自動更新のたびに「このアプリが変更を加えることを許可しますか？」が出ない
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\DayDreamPlusDigitalMosaic
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
OutputDir=..\dist_installer
OutputBaseFilename=DayDreamPlusDigitalMosaic_Setup
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 (1809) 以降
MinVersion=10.0.17763
; 更新時、起動中のアプリを閉じてから入れ替える
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; 更新時に古い部品が残らないよう、部品フォルダーを入れ替える（利用者のデータは別の場所にあるので消えない）
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\DayDreamPlusDigitalMosaic\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; 通常のインストール：最後の画面で「起動する」にチェック
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
; 自動更新（静かなモード）：完了後に自動で起動し直す
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: ShouldRelaunch

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function ShouldRelaunch: Boolean;
begin
  { 自動ビルドの検証では /NORELAUNCH=1 を付けて起動しないようにする }
  Result := WizardSilent and (ExpandConstant('{param:NORELAUNCH|0}') = '0');
end;
