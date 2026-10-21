; Inno Setup script: builds FileOrganizer-Setup.exe from the PyInstaller output in dist\FileOrganizer
#define MyAppName "File Organizer"
#define MyAppVersion "1.0.0"

[Setup]
AppId={{B7C3F1A2-6D4E-4C58-9A1B-3E5F7D2C8A90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\FileOrganizer
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=installer_out
OutputBaseFilename=FileOrganizer-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayName={#MyAppName}

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\FileOrganizer\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\FileOrganizer.exe"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\FileOrganizer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\FileOrganizer.exe"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
