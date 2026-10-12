; The Alhazen app's Windows installer (Inno Setup 6).
;
; Built by packaging/windows/build.py, which passes three definitions:
;   AppVersion  the alhazen version, e.g. 2.15.0
;   Stage       the folder to copy to the computer (tools, wheel, icon, licences)
;   Wheel       the alhazen wheel's file name inside Stage\wheels
;
; What an install does:
;   1. copies Stage to %LOCALAPPDATA%\Alhazen. A per-user folder: no
;      administrator rights are asked for, and the paths under it stay short
;      enough for a Windows without long paths.
;   2. has the bundled uv download a Python and create the app's environment
;      in <folder>\app, then install the alhazen wheel into it. This step
;      needs an internet connection; its output goes to <folder>\install.log.
;   3. adds the Start-menu shortcut, which runs <folder>\app\Scripts\alhazen-app.exe.
;
; The app's layout (<folder>\app beside <folder>\tools) is what
; alhazen.hub.environments.find_tools looks for; keep the two in step.
;
; Not removed by an uninstall: the person's workspace (their experiments, data
; and settings under %USERPROFILE%\.alhazen) and uv's shared download cache.

#ifndef AppVersion
  #error AppVersion is not defined. Build with packaging/windows/build.py.
#endif
#ifndef Stage
  #error Stage is not defined. Build with packaging/windows/build.py.
#endif
#ifndef Wheel
  #error Wheel is not defined. Build with packaging/windows/build.py.
#endif

; The Python the app itself runs on. Experiments get the Python their own
; lock files ask for; this one only runs the dashboard.
#define AppPython "3.12"

[Setup]
; Identifies the app to Windows across versions: never change it, or an
; upgrade would install beside the old version instead of over it.
AppId={{597AD17D-7C55-4184-847B-9E905C398D83}
AppName=Alhazen
AppVersion={#AppVersion}
AppVerName=Alhazen {#AppVersion}
AppPublisher=The Alhazen project
AppPublisherURL=https://github.com/sh4r11f/alhazen
DefaultDirName={localappdata}\Alhazen
; One fixed place (still overridable with /DIR= for tests): the app's own
; paths and the docs can then name it.
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=AlhazenSetup-{#AppVersion}
SetupIconFile={#Stage}\alhazen.ico
UninstallDisplayIcon={app}\alhazen.ico
UninstallDisplayName=Alhazen
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The running app is detected in [Code] (PrepareToInstall), with a message
; that says what to do; Windows' own "close applications" page cannot see it,
; because the program that is running is created by step 2, not copied here.
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Alhazen"; Filename: "{app}\app\Scripts\alhazen-app.exe"; IconFilename: "{app}\alhazen.ico"; WorkingDir: "{app}"; Comment: "Run experiments with Alhazen"
Name: "{autodesktop}\Alhazen"; Filename: "{app}\app\Scripts\alhazen-app.exe"; IconFilename: "{app}\alhazen.ico"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Offered on the last page, and only when step 2 succeeded.
Filename: "{app}\app\Scripts\alhazen-app.exe"; Description: "Open Alhazen"; Flags: nowait postinstall skipifsilent; Check: SetupSucceeded

[UninstallDelete]
; Created after the copy (step 2), so the uninstaller does not know them.
Type: filesandordirs; Name: "{app}\app"
Type: files; Name: "{app}\install.log"
Type: files; Name: "{app}\installed.ok"
Type: dirifempty; Name: "{app}"

[Code]
var
  { Whether step 2 (the app's Python environment) finished. }
  PythonReady: Boolean;

function SetupSucceeded(): Boolean;
begin
  Result := PythonReady;
end;

{ True while the app is running. Its server process is <folder>\app\Scripts\python.exe,
  and Windows refuses to open a program that is running for writing. }
function AppIsRunning(): Boolean;
var
  Path: String;
  Stream: TFileStream;
begin
  Result := False;
  Path := ExpandConstant('{app}\app\Scripts\python.exe');
  if not FileExists(Path) then
    exit;
  try
    Stream := TFileStream.Create(Path, fmOpenReadWrite or fmShareExclusive);
    Stream.Free;
  except
    Result := True;
  end;
end;

{ Before anything is copied: an upgrade replaces the environment the running
  app is using, so it must be closed first. A non-empty result stops the
  install with that message (also in a silent install, as a failure). }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if AppIsRunning() then
    Result := 'Alhazen is running. Close its window, wait for any session or setup to finish, then run this installer again.';
end;

{ Run the bundled uv with Arguments, its output appended to install.log.
  Through cmd.exe only for the redirection; True when uv exited with 0. }
function RunUv(const Title, Arguments: String): Boolean;
var
  Code: Integer;
  Command: String;
begin
  WizardForm.StatusLabel.Caption := Title;
  Command := '/C ""' + ExpandConstant('{app}\tools\uv.exe') + '" ' + Arguments
    + ' >> "' + ExpandConstant('{app}\install.log') + '" 2>&1"';
  Result := Exec(ExpandConstant('{cmd}'), Command, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code)
    and (Code = 0);
end;

{ Step 2, after the files are in place. The environment is made from
  scratch each time (--clear), so an upgrade never inherits packages from the
  version before it. A failure is said in a message box and leaves no
  "installed.ok": the app cannot start, and the message says why and what to do. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  Environment, Python, WheelPath: String;
begin
  if CurStep <> ssPostInstall then
    exit;
  Environment := ExpandConstant('{app}\app');
  Python := Environment + '\Scripts\python.exe';
  WheelPath := ExpandConstant('{app}\wheels\{#Wheel}');
  DeleteFile(ExpandConstant('{app}\install.log'));
  DeleteFile(ExpandConstant('{app}\installed.ok'));
  PythonReady :=
    RunUv('Downloading Python {#AppPython} (about 30 MB)...',
      'venv --clear --managed-python --python {#AppPython} "' + Environment + '"')
    and RunUv('Downloading and installing Alhazen''s packages (about 100 MB)...',
      'pip install --python "' + Python + '" "' + WheelPath + '"');
  if PythonReady then
    SaveStringToFile(ExpandConstant('{app}\installed.ok'), '{#AppVersion}', False)
  else
    MsgBox('Alhazen''s files were copied, but setting up its Python did not finish, so Alhazen cannot start yet.'
      + #13#10#13#10 + 'This step downloads from the internet. Check the connection and run this installer again.'
      + #13#10#13#10 + 'Details: ' + ExpandConstant('{app}\install.log'), mbError, MB_OK);
end;

{ An uninstall removes the environment the running app is using. }
function InitializeUninstall(): Boolean;
begin
  Result := True;
  if AppIsRunning() then
  begin
    MsgBox('Alhazen is running. Close its window, then uninstall again.', mbError, MB_OK);
    Result := False;
  end;
end;
