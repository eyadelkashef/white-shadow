; White Shadow - Windows setup program (built with Inno Setup: https://jrsoftware.org/isinfo.php)
; Works like the installer of any other launcher: Start Menu + desktop shortcut, appears in Settings > Apps for
; uninstalling, and needs NO administrator rights. It also sets up the one Windows component the app window needs
; (Microsoft WebView2) if the PC doesn't have it yet, so the app runs straight after installing.
#ifndef AppVer
  #define AppVer "1.0.0"
#endif
#define WV2 "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

[Setup]
AppId={{6F1D2B7C-4C0E-4B7A-9D55-3A9E5C1B7A21}
AppName=White Shadow Launcher
AppVersion={#AppVer}
AppPublisher=White Shadow
DefaultDirName={localappdata}\Programs\White Shadow
DefaultGroupName=White Shadow
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=WhiteShadow-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\WhiteShadow.exe
UninstallDisplayName=White Shadow Launcher
LicenseFile=EULA.txt
WizardStyle=modern
WizardImageFile=installer\wizard.bmp
WizardSmallImageFile=installer\wizard_small.bmp
Compression=lzma2
SolidCompression=yes
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\WhiteShadow-windows.exe"; DestDir: "{app}"; DestName: "WhiteShadow.exe"; Flags: ignoreversion
Source: "dist\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist; Check: WebView2Missing

[Icons]
Name: "{autoprograms}\White Shadow"; Filename: "{app}\WhiteShadow.exe"
Name: "{autodesktop}\White Shadow"; Filename: "{app}\WhiteShadow.exe"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Setting up the Windows web component (one-time)..."; Flags: waituntilterminated skipifdoesntexist; Check: WebView2Missing
Filename: "{app}\WhiteShadow.exe"; Description: "Launch White Shadow"; Flags: nowait postinstall skipifsilent

[Code]
function WebView2Has(Root: Integer; const Key: String): Boolean;
var v: String;
begin
  Result := False;
  case Root of
    0: Result := RegQueryStringValue(HKLM32, Key, 'pv', v);
    1: Result := RegQueryStringValue(HKLM64, Key, 'pv', v);
    2: Result := RegQueryStringValue(HKCU, Key, 'pv', v);
  end;
  if Result then Result := (v <> '') and (v <> '0.0.0.0');
end;

function WebView2Missing: Boolean;
var k: String;
begin
  k := 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{#WV2}';
  Result := not (WebView2Has(0, k) or WebView2Has(1, k) or WebView2Has(2, 'Software\Microsoft\EdgeUpdate\Clients\{#WV2}'));
end;
