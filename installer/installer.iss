; ============================================================================
;  Steam Card Price Watch - Inno Setup installer
;
;  Build with:  installer\build_installer.bat   (it downloads the bundled
;  Python installer into installer\python\ and invokes ISCC.exe on this file).
;
;  Design decisions:
;    * Per-user install (PrivilegesRequired=lowest) - NO UAC, no admin. The
;      app and Python both land under the user profile, so the program folder
;      is always writable (it stores config.json / *.json / logs next to the
;      code) and there's no Program-Files-vs-VirtualStore mess.
;    * Files are a WHITELIST: only source/resources are packed. .venv, .git,
;      .idea, preview, __pycache__, *.log and user data never enter the
;      installer because they're simply not listed here.
;    * User data is NOT shipped and NOT touched. config.json is created by the
;      app from config.example.json on first run; the *.json state files are
;      created as the app runs. On UPDATE, re-running the installer overwrites
;      code only - data lives outside the file manifest, so it survives.
;    * Update mode is automatic: same AppId -> Inno installs over the existing
;      directory, refreshing code, then setup_env.bat tops up dependencies.
;    * Uninstall removes the scheduled task, the venv and caches, and offers
;      to remove the bundled Python.
; ============================================================================

#define MyAppName "Steam Card Price Watch"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "sotik"
#define PyVersion "3.13.7"
#define PyInstaller "python-" + PyVersion + "-amd64.exe"

[Setup]
AppId={{457A4FF1-5DC2-4834-93F1-24403A15100D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user install: no admin, no UAC prompt.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\SteamCardWatch
DisableProgramGroupPage=yes
; Let the user pick the folder, but default to a writable per-user location.
DisableDirPage=no
OutputDir=dist
OutputBaseFilename=SteamCardWatch-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\icon.ico
UninstallDisplayName={#MyAppName}

[Languages]
Name: "uk"; MessagesFile: "compiler:Languages\Ukrainian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; --- application code (whitelist - nothing else gets in) ------------------
Source: "..\alerts.py";               DestDir: "{app}"; Flags: ignoreversion
Source: "..\browser_cookies.py";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\cookie_extract_helper.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\gui.pyw";                 DestDir: "{app}"; Flags: ignoreversion
Source: "..\i18n.py";                 DestDir: "{app}"; Flags: ignoreversion
Source: "..\regions.py";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\scheduler.py";            DestDir: "{app}"; Flags: ignoreversion
Source: "..\steam.py";                DestDir: "{app}"; Flags: ignoreversion
Source: "..\steam_login.py";          DestDir: "{app}"; Flags: ignoreversion
Source: "..\telegram.py";             DestDir: "{app}"; Flags: ignoreversion
Source: "..\watch.py";                DestDir: "{app}"; Flags: ignoreversion
Source: "..\themes.py";               DestDir: "{app}"; Flags: ignoreversion
; --- launchers + env bootstrap --------------------------------------------
Source: "..\gui.bat";                 DestDir: "{app}"; Flags: ignoreversion
Source: "..\gui_debug.bat";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\setup_env.bat";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\run.vbs";                 DestDir: "{app}"; Flags: ignoreversion
; --- resources -------------------------------------------------------------
Source: "..\requirements.txt";        DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.example.json";     DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.ico";                DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\*";  DestDir: "{app}\assets"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\lang\*";    DestDir: "{app}\lang";   Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\themes\*";  DestDir: "{app}\themes"; Flags: ignoreversion recursesubdirs createallsubdirs
; --- bundled Python installer (extracted only if we're going to run it) ----
Source: "python\{#PyInstaller}"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: ShouldInstallPython

[Icons]
; Shortcut points straight at pythonw.exe in the venv (no console flash).
; The venv doesn't exist until the [Run] step below finishes, but a .lnk
; only stores the path - it resolves fine once the venv is built.
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
    Parameters: """{app}\gui.pyw"""; WorkingDir: "{app}"; \
    IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
; 1) Install bundled Python silently, per-user, when it's missing OR when
;    the user ticked "update Python" on the custom page.
Filename: "{tmp}\{#PyInstaller}"; \
    Parameters: "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1"; \
    StatusMsg: "Installing Python {#PyVersion}..."; \
    Check: ShouldInstallPython

; 2) Build/refresh the venv and install dependencies.
Filename: "{app}\setup_env.bat"; WorkingDir: "{app}"; \
    StatusMsg: "Setting up environment and dependencies..."; \
    Flags: runhidden waituntilterminated

[UninstallDelete]
; venv, caches and logs are created at runtime - not in the manifest - so
; remove them explicitly. User data (config.json, *.json) is left for the
; CurUninstallStepChanged handler to confirm-delete.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files;          Name: "{app}\*.log"
Type: files;          Name: "{app}\*.log.*"

[Code]
var
  PythonInstalled: Boolean;   { True if a usable py -3.13 was found at startup }
  UpdatePythonCheck: TNewCheckBox;

{ Returns True when no usable Python 3.13 is present. We probe the py launcher
  pinned to 3.13 - matching what setup_env.bat looks for first, so the two
  stay in sync. }
function PythonNeeded: Boolean;
var
  rc: Integer;
begin
  if Exec(ExpandConstant('{cmd}'), '/c py -3.13 --version', '',
          SW_HIDE, ewWaitUntilTerminated, rc) and (rc = 0) then
    Result := False
  else
    Result := True;
end;

{ Decision used by [Files]/[Run]: install the bundled Python when none is
  present, OR when the user opted to update an existing one. }
function ShouldInstallPython: Boolean;
begin
  Result := (not PythonInstalled)
            or ((UpdatePythonCheck <> nil) and UpdatePythonCheck.Checked);
end;

{ Custom wizard page: explain the Python dependency, and - only when Python
  is already on the system - offer to reinstall it with the bundled version. }
procedure InitializeWizard;
var
  page: TWizardPage;
  info: TNewStaticText;
begin
  PythonInstalled := not PythonNeeded();

  page := CreateCustomPage(wpSelectDir,
    'Python', 'Середовище виконання програми');

  info := TNewStaticText.Create(page);
  info.Parent := page.Surface;
  info.Left := 0;
  info.Top := 0;
  info.Width := page.SurfaceWidth;
  info.AutoSize := False;
  info.Height := ScaleY(72);
  info.WordWrap := True;
  if PythonInstalled then
    info.Caption :=
      'Програма працює на Python 3.13. У системі вже знайдено відповідну '
      + 'версію — повторне встановлення не потрібне.'
  else
    info.Caption :=
      'Програма працює на Python 3.13. У системі його не знайдено, тому '
      + 'Python {#PyVersion} буде встановлено автоматично під час інсталяції '
      + '(тихо, без додаткових вікон).';

  UpdatePythonCheck := TNewCheckBox.Create(page);
  UpdatePythonCheck.Parent := page.Surface;
  UpdatePythonCheck.Left := 0;
  UpdatePythonCheck.Top := info.Top + info.Height + ScaleY(8);
  UpdatePythonCheck.Width := page.SurfaceWidth;
  UpdatePythonCheck.Caption :=
    'Оновити / перевстановити Python версією {#PyVersion} з інсталятора';
  UpdatePythonCheck.Checked := False;
  { Pointless when Python is absent (it gets installed regardless), so the
    checkbox only appears when there's an existing install to update. }
  UpdatePythonCheck.Visible := PythonInstalled;
end;

{ Find the per-user Python uninstall command in the registry, if our bundled
  Python is still registered. Returns '' when not found. }
function FindPythonUninstall(): String;
var
  rootKey: String;
  names: TArrayOfString;
  i: Integer;
  disp, cmd: String;
begin
  Result := '';
  rootKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall';
  if not RegGetSubkeyNames(HKCU, rootKey, names) then
    Exit;
  for i := 0 to GetArrayLength(names) - 1 do
  begin
    if RegQueryStringValue(HKCU, rootKey + '\' + names[i], 'DisplayName', disp) then
    begin
      if Pos('Python 3.13', disp) > 0 then
      begin
        if RegQueryStringValue(HKCU, rootKey + '\' + names[i],
                               'QuietUninstallString', cmd) then
        begin
          Result := cmd;
          Exit;
        end;
      end;
    end;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  rc: Integer;
  pyUninstall: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { 1) Drop the scheduled task if it's still there. }
    Exec(ExpandConstant('{cmd}'),
         '/c schtasks /Delete /TN SteamCardWatch /F',
         '', SW_HIDE, ewWaitUntilTerminated, rc);

    { 2) Offer to delete the user data sitting next to the program. }
    if MsgBox('Delete saved data too (config, watch/sale/game lists, history)?'
              + #13#10 + 'Choose No to keep it for a future reinstall.',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      DelTree(ExpandConstant('{app}'), True, True, True);
    end;

    { 3) Offer to remove the bundled Python (only if we can find it). }
    pyUninstall := FindPythonUninstall();
    if pyUninstall <> '' then
    begin
      if MsgBox('Also remove Python ' + '{#PyVersion}' + '?'
                + #13#10 + 'If other programs use it, keep it.',
                mbConfirmation, MB_YESNO) = IDYES then
      begin
        Exec(ExpandConstant('{cmd}'), '/c ' + pyUninstall, '',
             SW_HIDE, ewWaitUntilTerminated, rc);
      end;
    end;
  end;
end;
