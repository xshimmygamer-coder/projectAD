; ============================================================
;  Inno Setup — gera o instalador (Setup.exe) do twitch_swap.
;  Pre-requisito: build_exe.bat ja rodado (existe dist\twitch_swap\).
;  Compile abrindo este .iss no Inno Setup, ou:  iscc installer.iss
;  (baixe o Inno Setup: https://jrsoftware.org/isdl.php)
;
;  Instala POR USUARIO em %LOCALAPPDATA%\twitch_swap (sem admin) p/ o app
;  poder gravar settings.json/tokens/proxies ao lado do .exe.
; ============================================================
#define MyApp "MURIADS"
#define MyExe "MURIADS.exe"

[Setup]
AppName={#MyApp}
AppVersion=1.0
DefaultDirName={localappdata}\{#MyApp}
DefaultGroupName={#MyApp}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=MURIADS_setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=assets\icone.ico
UninstallDisplayIcon={app}\{#MyExe}

[Files]
; copia TODA a pasta do build (onedir) pro destino
Source: "dist\MURIADS\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyApp}";              Filename: "{app}\{#MyExe}"
Name: "{userdesktop}\{#MyApp}";        Filename: "{app}\{#MyExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na area de trabalho"; GroupDescription: "Atalhos:"

[Run]
Filename: "{app}\{#MyExe}"; Description: "Abrir o {#MyApp}"; Flags: nowait postinstall skipifsilent
