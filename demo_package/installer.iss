; Inno Setup script — Windows installer for the Repair Shop demo package.
; Compile:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" demo_package\installer.iss
; Input:    demo_package\dist\A1RepairDemo\*   (PyInstaller onedir output)

#define AppName "Repair Shop Demo"
#define AppVersion "1.0"
#define AppPublisher "Manoj Badapanda"
#define ExeName "A1RepairDemo.exe"
#define SrcDir "dist\A1RepairDemo"

[Setup]
AppId={{1A530B1F-FC5C-4625-9A9D-685DF563D56F}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://a1computersolutions.in
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=output
OutputBaseFilename=RepairShopDemo-Setup
Compression=lzma2
SolidCompression=yes
UninstallDisplayName={#AppName}
SetupIconFile={#SrcDir}\..\..\a1demo.ico
WizardStyle=modern
; The demo writes its own data under %LOCALAPPDATA%\A1RepairDemo (SQLite +
; static + media). No admin rights needed; nothing touches the registry DB.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut (with icon)"; GroupDescription: "Shortcuts:"
[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"; Comment: "Launch the Repair Shop demo (local server)"
Name: "{group}\{#AppName} Guide"; Filename: "{app}\{#ExeName}"; Parameters: "--guide"; Comment: "Open the bilingual tutorial"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon; IconFilename: "{app}\{#ExeName}"; Comment: "Launch the Repair Shop demo"

[Run]
; One-time first-run setup (DB migrate + static + demo seed) at install time,
; so launching from the desktop shortcut is instant.
Filename: "{app}\{#ExeName}"; Parameters: "--setup"; Flags: runhidden waituntilterminated; StatusMsg: "Preparing demo workspace (database + sample data)…"
Filename: "{app}\{#ExeName}"; Description: "Launch {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\A1RepairDemo"

[Code]
function InitializeUninstall(): Boolean;
begin
  Result := True;
end;
