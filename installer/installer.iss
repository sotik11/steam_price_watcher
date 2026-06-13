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
{ Inno's per-user uninstall registry key for this AppId (PrivilegesRequired=
  lowest -> HKCU). InstallLocation under it tells us where a previous copy
  lives, which is how we detect "already installed" and switch the wizard
  into maintenance mode. }
const
  UNINSTALL_KEY =
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
    '{457A4FF1-5DC2-4834-93F1-24403A15100D}_is1';

{ Win32 ExitProcess - used to terminate cleanly right after an in-installer
  uninstall (modes 2/3), so we don't fall through into the install flow. }
procedure ExitProcessWin(uExitCode: Cardinal);
  external 'ExitProcess@kernel32.dll stdcall';

var
  PythonInstalled: Boolean;        { usable py -3.13 found at startup? }
  UpdatePythonCheck: TNewCheckBox;
  PythonPage: TWizardPage;
  ExistingInstall: Boolean;        { a previous install was detected }
  ExistingDir: String;             { ...and it lives here }
  ModePage: TInputOptionWizardPage;
  InstallMode: Integer;            { 0 update, 1 clean reinstall, 2 del+keep, 3 del }

{ Returns True when no usable Python 3.13 is present. Probes the py launcher
  pinned to 3.13 - matching what setup_env.bat looks for first. }
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

{ Install bundled Python when none is present, OR when the user opted to
  update an existing one. }
function ShouldInstallPython: Boolean;
begin
  Result := (not PythonInstalled)
            or ((UpdatePythonCheck <> nil) and UpdatePythonCheck.Checked);
end;

{ Copy one user-data file if it exists. }
procedure CopyDataFile(srcDir, dstDir, name: String);
begin
  if FileExists(srcDir + '\' + name) then
    CopyFile(srcDir + '\' + name, dstDir + '\' + name, False);
end;

{ Copy everything matching a mask (used for rotating logs). }
procedure CopyDataMask(srcDir, dstDir, mask: String);
var
  fr: TFindRec;
begin
  if FindFirst(srcDir + '\' + mask, fr) then
  begin
    try
      repeat
        if (fr.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then
          CopyFile(srcDir + '\' + fr.Name, dstDir + '\' + fr.Name, False);
      until not FindNext(fr);
    finally
      FindClose(fr);
    end;
  end;
end;

{ Back up user data (config + state files + logs) to a folder on the Desktop,
  preserving names. Data here is flat, so a single folder mirrors it. }
procedure BackupDataToDesktop(srcDir: String);
var
  dst: String;
begin
  dst := ExpandConstant('{userdesktop}\{#MyAppName}');
  ForceDirectories(dst);
  CopyDataFile(srcDir, dst, 'config.json');
  CopyDataFile(srcDir, dst, 'watchlist.json');
  CopyDataFile(srcDir, dst, 'salelist.json');
  CopyDataFile(srcDir, dst, 'gamelist.json');
  CopyDataFile(srcDir, dst, 'gameblacklist.json');
  CopyDataFile(srcDir, dst, 'purchases.json');
  CopyDataFile(srcDir, dst, 'state.json');
  CopyDataMask(srcDir, dst, '*.log');
  CopyDataMask(srcDir, dst, '*.log.*');
end;

{ Remove the scheduled task created by the app's scheduler tab. }
procedure DropScheduledTask;
var
  rc: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
       '/c schtasks /Delete /TN SteamCardWatch /F',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

{ In-installer uninstall for modes 2 (keep data) and 3 (full). Does the whole
  job itself - task, optional data backup, program tree, shortcut, registry -
  then exits the process so we never enter the install flow. The Inno-generated
  unins000.exe still exists as a fallback from Add/Remove Programs. }
procedure DoUninstall(keepData: Boolean);
begin
  DropScheduledTask;
  if keepData then
    BackupDataToDesktop(ExistingDir);
  DeleteFile(ExpandConstant('{userdesktop}\{#MyAppName}.lnk'));
  DelTree(ExistingDir, True, True, True);
  RegDeleteKeyIncludingSubkeys(HKCU, UNINSTALL_KEY);
  if keepData then
    MsgBox('Програму видалено. Збережені дані скопійовано на Робочий стіл '
           + 'у теку "{#MyAppName}".', mbInformation, MB_OK)
  else
    MsgBox('Програму повністю видалено.', mbInformation, MB_OK);
  ExitProcessWin(0);
end;

procedure InitializeWizard;
var
  info: TNewStaticText;
begin
  PythonInstalled := not PythonNeeded();
  ExistingInstall := RegQueryStringValue(HKCU, UNINSTALL_KEY,
                                         'InstallLocation', ExistingDir)
                     and (ExistingDir <> '');
  InstallMode := 0;

  { Maintenance page - shown only on a repeat run (see ShouldSkipPage). }
  ModePage := CreateInputOptionPage(wpWelcome,
    'Програму вже встановлено', 'Оберіть дію',
    'Знайдено наявну інсталяцію {#MyAppName}. Що зробити?',
    True, False);
  ModePage.Add('Оновити (зберегти дані та задачу планувальника)');
  ModePage.Add('Перевстановити з нуля (видалити дані + задачу, потім встановити)');
  ModePage.Add('Видалити, зберігши дані на Робочому столі');
  ModePage.Add('Повністю видалити');
  ModePage.SelectedValueIndex := 0;

  { Python info/update page. }
  PythonPage := CreateCustomPage(wpSelectDir,
    'Python', 'Середовище виконання програми');
  info := TNewStaticText.Create(PythonPage);
  info.Parent := PythonPage.Surface;
  info.Left := 0;
  info.Top := 0;
  info.Width := PythonPage.SurfaceWidth;
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

  UpdatePythonCheck := TNewCheckBox.Create(PythonPage);
  UpdatePythonCheck.Parent := PythonPage.Surface;
  UpdatePythonCheck.Left := 0;
  UpdatePythonCheck.Top := info.Top + info.Height + ScaleY(8);
  UpdatePythonCheck.Width := PythonPage.SurfaceWidth;
  UpdatePythonCheck.Caption :=
    'Оновити / перевстановити Python версією {#PyVersion} з інсталятора';
  UpdatePythonCheck.Checked := False;
  UpdatePythonCheck.Visible := PythonInstalled;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  { Mode page: only on a repeat run. }
  if (ModePage <> nil) and (PageID = ModePage.ID) then
    Result := not ExistingInstall;
  { Directory page: skip when updating/reinstalling - the folder is known.
    (Uninstall modes exit before reaching it anyway.) }
  if (PageID = wpSelectDir) and ExistingInstall then
    Result := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (ModePage <> nil) and (CurPageID = ModePage.ID) then
  begin
    InstallMode := ModePage.SelectedValueIndex;
    case InstallMode of
      2: DoUninstall(True);    { does not return - ExitProcessWin }
      3: DoUninstall(False);   { does not return }
    end;
    { modes 0 (update) and 1 (clean reinstall) fall through to install }
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { Clean reinstall: wipe data + venv + task BEFORE files are copied, so the
    install proceeds onto a blank slate. Code is overwritten by Inno; the
    venv is rebuilt by setup_env.bat in [Run]. }
  if (CurStep = ssInstall) and ExistingInstall and (InstallMode = 1) then
  begin
    DropScheduledTask;
    DeleteFile(ExistingDir + '\config.json');
    DeleteFile(ExistingDir + '\watchlist.json');
    DeleteFile(ExistingDir + '\salelist.json');
    DeleteFile(ExistingDir + '\gamelist.json');
    DeleteFile(ExistingDir + '\gameblacklist.json');
    DeleteFile(ExistingDir + '\purchases.json');
    DeleteFile(ExistingDir + '\state.json');
    DelTree(ExistingDir + '\.venv', True, True, True);
    DelTree(ExistingDir + '\*.log', False, True, False);
    DelTree(ExistingDir + '\*.log.*', False, True, False);
  end;
end;

{ ---- Fallback path: unins000.exe launched from Add/Remove Programs -------- }

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
    DropScheduledTask;

    if MsgBox('Видалити також збережені дані (config, списки, історія)?'
              + #13#10 + 'Оберіть «Ні», щоб зберегти їх для майбутнього '
              + 'перевстановлення.',
              mbConfirmation, MB_YESNO) = IDYES then
      DelTree(ExpandConstant('{app}'), True, True, True);

    pyUninstall := FindPythonUninstall();
    if pyUninstall <> '' then
    begin
      if MsgBox('Видалити також Python {#PyVersion}?'
                + #13#10 + 'Якщо ним користуються інші програми — залиште.',
                mbConfirmation, MB_YESNO) = IDYES then
        Exec(ExpandConstant('{cmd}'), '/c ' + pyUninstall, '',
             SW_HIDE, ewWaitUntilTerminated, rc);
    end;
  end;
end;
