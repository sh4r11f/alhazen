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
; When step 2 fails the app cannot start, and the install says so three ways:
; a message with what uv reported, exit code 20 (PythonStepFailed below; Setup's
; own codes stop at 8), and no shortcut left pointing at a program that is not there.
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

; The exit code of an install whose files were copied but whose Python step
; (step 2) failed. Inno Setup's own exit codes are 0 to 8.
#define PythonStepFailed 20

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
; Windows' RedirectionGuard stays off. Inno Setup switches it on by default
; from version 6.7. A process under the guard refuses to go through a folder
; link (a junction) that someone who is not an administrator made. The guard
; also covers the programs Setup starts, whatever Inno Setup's documentation
; says: seen on Windows 11 (build 26300) and on Windows Server 2025. uv keeps
; every Python it manages behind exactly such a link, made by the person installing
; (cpython-3.12-... points to cpython-3.12.14-...). So with the guard on,
; step 2 failed for everyone who is not an administrator:
;   "The path cannot be traversed because it contains an untrusted mount
;    point. (os error 448)"
; The guard is there for an installer that has more rights than the person
; who made the link. This one runs as that person and writes only to their
; own folders, so it protects nothing here.
; (An older compiler does not know the directive, and has no guard to switch off.)
#if Ver >= EncodeVer(6,7,0)
RedirectionGuard=no
#endif

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
  { Whether step 2 was reached at all. False when the install stopped before
    it: Setup then reports that itself, with its own exit code. }
  PythonStepRan: Boolean;

function SetupSucceeded(): Boolean;
begin
  Result := PythonReady;
end;

{ What the exit code of a finished install is. Setup counts an install as
  done once the files are copied, so without this a silent install whose
  Python step failed would end with 0, like one that worked, and whoever
  started it would never be told. Setup calls this only when its own code
  would have been 0. }
function GetCustomSetupExitCode(): Integer;
begin
  Result := 0;
  if PythonStepRan and not PythonReady then
    Result := {#PythonStepFailed};
end;

{ The end of install.log: what uv itself reported, which is the cause. The
  message shows it, because a guess ("check the connection") was wrong the
  first time this step failed on a real computer. Empty when there is no log. }
function LogTail(): String;
var
  Lines: TArrayOfString;
  Index, First: Integer;
begin
  Result := '';
  if not LoadStringsFromFile(ExpandConstant('{app}\install.log'), Lines) then
    exit;
  { uv ends with its error and the causes under it: the last lines. }
  First := GetArrayLength(Lines) - 6;
  if First < 0 then
    First := 0;
  for Index := First to GetArrayLength(Lines) - 1 do
    if Trim(Lines[Index]) <> '' then
      Result := Result + Trim(Lines[Index]) + #13#10;
  { A message box has room for a few lines. Keep the end: the last cause
    uv names is the root one. }
  if Length(Result) > 900 then
    Result := '...' + Copy(Result, Length(Result) - 899, 900);
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
  version before it.

  A failure leaves no "installed.ok" and is said loudly: a message with what
  uv reported, a non-zero exit code (GetCustomSetupExitCode), and the
  shortcuts taken away again, since they would point at a program that does
  not exist. Running the installer again puts them back. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  Environment, Python, WheelPath, Step, Report: String;
begin
  if CurStep <> ssPostInstall then
    exit;
  PythonStepRan := True;
  Environment := ExpandConstant('{app}\app');
  Python := Environment + '\Scripts\python.exe';
  WheelPath := ExpandConstant('{app}\wheels\{#Wheel}');
  DeleteFile(ExpandConstant('{app}\install.log'));
  DeleteFile(ExpandConstant('{app}\installed.ok'));
  { The two halves in turn, so the message can name the one that failed. }
  Step := 'getting its Python';
  PythonReady := RunUv('Downloading Python {#AppPython} (about 30 MB)...',
    'venv --clear --managed-python --python {#AppPython} "' + Environment + '"');
  if PythonReady then
  begin
    Step := 'installing its packages';
    PythonReady := RunUv('Downloading and installing Alhazen''s packages (about 100 MB)...',
      'pip install --python "' + Python + '" "' + WheelPath + '"');
  end;
  if PythonReady then
  begin
    SaveStringToFile(ExpandConstant('{app}\installed.ok'), '{#AppVersion}', False);
    exit;
  end;
  { The shortcuts were made before this step; without the app they are dead. }
  DeleteFile(ExpandConstant('{autoprograms}\Alhazen.lnk'));
  DeleteFile(ExpandConstant('{autodesktop}\Alhazen.lnk'));
  Report := LogTail();
  if Report = '' then
    Report := '(nothing was written to the log)' + #13#10;
  { Suppressible, not MsgBox: a plain MsgBox is shown even to a silent
    install with /SUPPRESSMSGBOXES, which then waits for a click nobody is
    there to make. The text still goes to Setup's log (/LOG). }
  SuppressibleMsgBox('Alhazen''s files were copied, but ' + Step + ' did not finish, so Alhazen cannot start yet.'
    + #13#10#13#10 + 'What was reported:' + #13#10 + Report
    + #13#10 + 'This step needs an internet connection. Once the cause is dealt with, run this installer again.'
    + #13#10#13#10 + 'The full log: ' + ExpandConstant('{app}\install.log'), mbError, MB_OK, IDOK);
end;

{ An uninstall removes the environment the running app is using. }
function InitializeUninstall(): Boolean;
begin
  Result := True;
  if AppIsRunning() then
  begin
    { Suppressible for the same reason as above: a silent uninstall must
      end (refused), not wait for a click. }
    SuppressibleMsgBox('Alhazen is running. Close its window, then uninstall again.', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;
