; ============================================================================
;  Steam Price Watcher - Inno Setup installer
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

#define MyAppName "Steam Price Watcher"
#define MyAppVersion "0.1.2.1"
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
DefaultDirName={localappdata}\Programs\SteamPriceWatcher
DisableProgramGroupPage=yes
; Let the user pick the folder, but default to a writable per-user location.
DisableDirPage=no
OutputDir=dist
OutputBaseFilename={#MyAppName} {#MyAppVersion}
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
Source: "..\version.py";              DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.*.txt";         DestDir: "{app}"; Flags: ignoreversion
; --- launchers + env bootstrap --------------------------------------------
Source: "..\gui.bat";                 DestDir: "{app}"; Flags: ignoreversion
Source: "..\gui_debug.bat";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\setup_env.bat";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\run.vbs";                 DestDir: "{app}"; Flags: ignoreversion
; --- resources -------------------------------------------------------------
Source: "..\requirements.txt";          DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements-optional.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config.example.json";     DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.ico";                DestDir: "{app}"; Flags: ignoreversion
; Prebuilt wheels for deps with no PyPI wheel on this Python (rookiepy).
; setup_env.bat installs from here offline; missing/mismatch just skips.
Source: "wheels\*";  DestDir: "{app}\wheels"; Flags: ignoreversion recursesubdirs createallsubdirs
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

; 2) Build/refresh the venv and install dependencies. Run via cmd /c -
;    Inno's CreateProcess can't launch a .bat directly, which is why the
;    venv was never built before. setup_env.bat writes setup_env.log next
;    to itself for diagnosis.
Filename: "{cmd}"; Parameters: "/c ""{app}\setup_env.bat"""; \
    WorkingDir: "{app}"; \
    StatusMsg: "Setting up environment and dependencies..."; \
    Flags: runhidden waituntilterminated

; 3) Optional launch from the final page (checkbox).
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\gui.pyw"""; \
    WorkingDir: "{app}"; Description: "Запустити {#MyAppName}"; \
    Flags: postinstall nowait skipifsilent

; 4) Optional delete of the imported backup .zip — only shown when we
;    actually unpacked one (WasZipUsed). Path comes from {code:GetZipPath}.
Filename: "{cmd}"; Parameters: "/c del ""{code:GetZipPath}"""; \
    Description: "Видалити архів даних після імпорту"; \
    Flags: postinstall skipifsilent runhidden; Check: WasZipUsed

[UninstallDelete]
; venv, caches and logs are created at runtime - not in the manifest - so
; remove them explicitly. User data (config.json, *.json) is left for the
; CurUninstallStepChanged handler to confirm-delete.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files;          Name: "{app}\*.log"
Type: files;          Name: "{app}\*.log.*"
Type: files;          Name: "{app}\setup_env.log"

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
  RemovePythonCheck: TNewCheckBox; { on the maintenance page, for delete modes }
  PythonRemovable: Boolean;        { our per-user Python is registered & removable }
  ImportPage: TWizardPage;         { restore-user-data page (fresh / clean reinstall) }
  ImportDataCheck: TNewCheckBox;
  ZipPathEdit: TNewEdit;           { chosen backup .zip, '' if none }
  UsedZipImport: Boolean;          { set in post-install if we unpacked a zip }

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

{ Copy the user-data files (config + state lists, no logs) src -> dst. }
procedure ImportDataFiles(srcDir, dstDir: String);
begin
  CopyDataFile(srcDir, dstDir, 'config.json');
  CopyDataFile(srcDir, dstDir, 'watchlist.json');
  CopyDataFile(srcDir, dstDir, 'salelist.json');
  CopyDataFile(srcDir, dstDir, 'gamelist.json');
  CopyDataFile(srcDir, dstDir, 'gameblacklist.json');
  CopyDataFile(srcDir, dstDir, 'purchases.json');
  CopyDataFile(srcDir, dstDir, 'state.json');
end;

{ Restore user data into the installed app when the import checkbox is on.
  Source priority (both OVERWRITE whatever's already at the install path -
  ticking the box is an explicit "bring my data in" on any mode, update
  included):
    1. a .zip the user explicitly picked — unpack it (-Force overwrites);
    2. else Desktop\<AppName> — the folder the delete-with-keep mode makes
       (or one dropped there manually) — copied in (CopyFile overwrites).
  Runs at post-install (files are in place, before the app launches). }
procedure ImportUserData(appDir: String);
var
  deskDir, zip: String;
  rc: Integer;
begin
  if (ImportDataCheck = nil) or (not ImportDataCheck.Checked) then
    Exit;
  zip := '';
  if ZipPathEdit <> nil then
    zip := Trim(ZipPathEdit.Text);
  if (zip <> '') and FileExists(zip) then
  begin
    Exec('powershell.exe',
         '-NoProfile -Command "Expand-Archive -Path ''' + zip
         + ''' -DestinationPath ''' + appDir + ''' -Force"',
         '', SW_HIDE, ewWaitUntilTerminated, rc);
    UsedZipImport := True;
    Exit;
  end;
  deskDir := ExpandConstant('{userdesktop}\{#MyAppName}');
  if FileExists(deskDir + '\config.json') then
    ImportDataFiles(deskDir, appDir);
end;

{ For the final-page "delete archive" task: shown only if we unpacked a
  zip, and supplies its path to the del command. }
function WasZipUsed: Boolean;
begin
  Result := UsedZipImport;
end;

function GetZipPath(Param: String): String;
begin
  if ZipPathEdit <> nil then
    Result := Trim(ZipPathEdit.Text)
  else
    Result := '';
end;

{ Remove the scheduled task created by the app's scheduler tab. Also drops
  the legacy "SteamCardWatch" task (the pre-rename name): an orphaned old
  task keeps firing watch.py and, during a clean reinstall, could run it in
  the window where config.json is wiped-but-not-yet-imported. /F so a
  missing task isn't an error. }
procedure DropScheduledTask;
var
  rc: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
       '/c schtasks /Delete /TN SteamPriceWatcher /F',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec(ExpandConstant('{cmd}'),
       '/c schtasks /Delete /TN SteamCardWatch /F',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
end;

{ Kill any running instance of the app BEFORE deleting/overwriting files.
  The GUI runs as <dir>\.venv\Scripts\pythonw.exe and holds a lock on the
  venv, so DelTree/Inno can't remove or replace it while it's alive - that's
  why an uninstall-while-running left .venv/config/log behind and the window
  still up. We target only pythonw.exe whose executable path is inside the
  install dir, so other Python apps are untouched. The short Sleep lets
  Windows release the file handles before we proceed. }
procedure KillRunningApp(appDir: String);
var
  rc: Integer;
  ps: String;
begin
  ps := 'Get-CimInstance Win32_Process | Where-Object { '
      + '$_.Name -eq ''pythonw.exe'' -and $_.ExecutablePath -and '
      + '$_.ExecutablePath.StartsWith(''' + appDir + ''') } | '
      + 'ForEach-Object { Stop-Process -Id $_.ProcessId -Force }';
  Exec('powershell.exe',
       '-NoProfile -WindowStyle Hidden -Command "' + ps + '"',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(800);
end;

{ Find the per-user Python uninstall command in the registry, if our bundled
  Python is still registered. Returns '' when not found. Declared here (above
  DoUninstall and InitializeWizard) since both use it. }
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

{ In-installer uninstall for modes 2 (keep data) and 3 (full). Does the whole
  job itself - task, optional data backup, program tree, shortcut, registry -
  then exits the process so we never enter the install flow. The Inno-generated
  unins000.exe still exists as a fallback from Add/Remove Programs. }
procedure DoUninstall(keepData: Boolean);
var
  rc: Integer;
  pyCmd: String;
begin
  KillRunningApp(ExistingDir);   { release file locks before deleting }
  DropScheduledTask;
  if keepData then
    BackupDataToDesktop(ExistingDir);
  DeleteFile(ExpandConstant('{userdesktop}\{#MyAppName}.lnk'));
  DelTree(ExistingDir, True, True, True);
  RegDeleteKeyIncludingSubkeys(HKCU, UNINSTALL_KEY);
  { Optional: remove the bundled Python too, if the user ticked the box and
    we can find its uninstall command. Done last - the app dir is already
    gone, and the venv inside it pointed at this Python. }
  if (RemovePythonCheck <> nil) and RemovePythonCheck.Checked then
  begin
    pyCmd := FindPythonUninstall();
    if pyCmd <> '' then
    begin
      Exec(ExpandConstant('{cmd}'), '/c ' + pyCmd, '',
           SW_HIDE, ewWaitUntilTerminated, rc);
      { The per-user Python uninstaller leaves leftovers behind: the
        Python313 tree (third-party site-packages it didn't create) AND
        the separate Launcher folder (py.exe/pyw.exe — a distinct ARP
        entry the core uninstaller doesn't touch). Sweep the whole
        per-user Programs\Python tree. We only get here when our bundled
        per-user Python was found in HKCU, so this is our install. }
      DelTree(ExpandConstant('{localappdata}\Programs\Python'),
              True, True, True);
    end;
  end;
  if keepData then
    MsgBox('Програму видалено. Збережені дані скопійовано на Робочий стіл '
           + 'у теку "{#MyAppName}".', mbInformation, MB_OK)
  else
    MsgBox('Програму повністю видалено.', mbInformation, MB_OK);
  ExitProcessWin(0);
end;

{ Show "also remove Python" only for the two delete modes (index 2/3) and
  only when our Python is actually removable. Fires on every click in the
  mode list, so it tracks the selection live. }
procedure ModeSelectionChanged(Sender: TObject);
begin
  if (RemovePythonCheck <> nil) and (ModePage <> nil) then
    RemovePythonCheck.Visible :=
      PythonRemovable and (ModePage.SelectedValueIndex >= 2);
end;

{ "Choose .zip" button on the import page. }
procedure BrowseZipClick(Sender: TObject);
var
  fn: String;
begin
  fn := '';
  if GetOpenFileName('Оберіть архів із даними', fn,
                     ExpandConstant('{userdesktop}'),
                     'ZIP-архіви (*.zip)|*.zip|Усі файли (*.*)|*.*', 'zip') then
    ZipPathEdit.Text := fn;
end;

procedure InitializeWizard;
var
  info: TNewStaticText;
  importInfo: TNewStaticText;
  browseBtn: TNewButton;
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

  { Extra checkbox under the radios, for the two delete modes: optionally
    also uninstall the bundled Python. Shrink the radio list to make room.
    Shown only when our Python is actually registered (nothing to remove
    otherwise). At update/reinstall the box is simply ignored. }
  ModePage.CheckListBox.Height := ScaleY(96);
  RemovePythonCheck := TNewCheckBox.Create(ModePage);
  RemovePythonCheck.Parent := ModePage.Surface;
  RemovePythonCheck.Left := ModePage.CheckListBox.Left;
  RemovePythonCheck.Top := ModePage.CheckListBox.Top
                           + ModePage.CheckListBox.Height + ScaleY(12);
  RemovePythonCheck.Width := ModePage.SurfaceWidth;
  RemovePythonCheck.Caption :=
    'При видаленні: також видалити Python {#PyVersion}';
  RemovePythonCheck.Checked := False;
  { Hidden until a delete mode (index 2/3) is picked; ModeSelectionChanged
    toggles it live. Only meaningful when our Python is registered. }
  PythonRemovable := (FindPythonUninstall() <> '');
  RemovePythonCheck.Visible := False;
  ModePage.CheckListBox.OnClickCheck := @ModeSelectionChanged;

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

  { Import-user-data page. Shown on a fresh install and on a clean
    reinstall (see ShouldSkipPage); pointless on an update (data stays). }
  ImportPage := CreateCustomPage(PythonPage.ID,
    'Дані', 'Імпорт користувацьких даних');

  ImportDataCheck := TNewCheckBox.Create(ImportPage);
  ImportDataCheck.Parent := ImportPage.Surface;
  ImportDataCheck.Left := 0;
  ImportDataCheck.Top := 0;
  ImportDataCheck.Width := ImportPage.SurfaceWidth;
  ImportDataCheck.Caption :=
    'Імпортувати користувацькі дані (config, списки, історія)';
  ImportDataCheck.Checked := False;

  importInfo := TNewStaticText.Create(ImportPage);
  importInfo.Parent := ImportPage.Surface;
  importInfo.Left := 0;
  importInfo.Top := ImportDataCheck.Top + ImportDataCheck.Height + ScaleY(10);
  importInfo.Width := ImportPage.SurfaceWidth;
  importInfo.AutoSize := False;
  importInfo.Height := ScaleY(64);
  importInfo.WordWrap := True;
  importInfo.Caption :=
    'Якщо позначено — дані буде відновлено (з ПЕРЕЗАПИСОМ наявних): '
    + 'спершу з вказаного .zip-архіву, інакше з теки "{#MyAppName}" на '
    + 'Робочому столі (її створює видалення «зі збереженням даних»). '
    + 'Працює і при оновленні.';

  ZipPathEdit := TNewEdit.Create(ImportPage);
  ZipPathEdit.Parent := ImportPage.Surface;
  ZipPathEdit.Left := 0;
  ZipPathEdit.Top := importInfo.Top + importInfo.Height + ScaleY(6);
  ZipPathEdit.Width := ImportPage.SurfaceWidth - ScaleX(110);
  ZipPathEdit.ReadOnly := True;

  browseBtn := TNewButton.Create(ImportPage);
  browseBtn.Parent := ImportPage.Surface;
  browseBtn.Left := ZipPathEdit.Left + ZipPathEdit.Width + ScaleX(10);
  browseBtn.Top := ZipPathEdit.Top - ScaleY(1);
  browseBtn.Width := ScaleX(100);
  browseBtn.Caption := 'Обрати .zip…';
  browseBtn.OnClick := @BrowseZipClick;
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
  { Import page is shown on every install path that proceeds to copying
    files — fresh, update (mode 0) and clean reinstall (mode 1). The two
    delete modes never reach it (they ExitProcess on the mode page). So:
    never skip it here. When the checkbox is left off it's a no-op anyway. }
end;

{ Add the data-import choice to the "Ready to install" summary, next to
  the Desktop-shortcut task, so the user sees it's going to happen. }
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo,
  MemoTypeInfo, MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := '';
  if MemoDirInfo <> '' then
    Result := Result + MemoDirInfo + NewLine + NewLine;
  if MemoTasksInfo <> '' then
    Result := Result + MemoTasksInfo;
  if (ImportDataCheck <> nil) and ImportDataCheck.Checked then
  begin
    if (ZipPathEdit <> nil) and (Trim(ZipPathEdit.Text) <> '') then
      Result := Result + NewLine + Space
                + 'Імпортувати користувацькі дані (з архіву)'
    else
      Result := Result + NewLine + Space
                + 'Імпортувати користувацькі дані';
  end;
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
  if (CurStep = ssInstall) and ExistingInstall then
    { Kill a running instance first - otherwise the locked venv blocks
      both Inno's file copy and setup_env's venv rebuild (update mode),
      and the wipe below (clean reinstall). }
    KillRunningApp(ExistingDir);

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

  { Restore user data after files are in place (fresh install or clean
    reinstall — the import checkbox only exists there). No-op if the box
    was left unchecked. }
  if CurStep = ssPostInstall then
    ImportUserData(ExpandConstant('{app}'));
end;

{ ---- Fallback path: unins000.exe launched from Add/Remove Programs -------- }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  rc: Integer;
  pyUninstall: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    KillRunningApp(ExpandConstant('{app}'));   { free locks first }
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
