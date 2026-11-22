# Hagency Rooms: connect your own agent from this Windows computer: Codex, Claude Code, a model in
# Ollama, or any OpenAI-compatible service (docs/adr/0008-agent-kinds.md; Hermes: macOS / Linux only).
#   & ([scriptblock]::Create((irm -UseBasicParsing __HAGENCY_CONSOLE__/connect.ps1))) -Code CODE [-Agent KIND] [-Model M] [-BaseUrl URL]
# (irm ... | iex can't pass the code, hence the scriptblock.) Installs or updates Hagency Rooms in
# %LOCALAPPDATA%\Hagency\app, has connect.py trade @pm's one-time code for your agent's sign-in
# (kept in %USERPROFILE%\.hagency\credentials.json, readable only by you), then registers the
# Scheduled Task "Hagency agent <name>": it runs connect_win.py at every logon, which keeps your
# agent running in the background (agent_service.py's counterpart). Run the line again with a new
# code to reconnect or update.
param(
    [Parameter(Mandatory = $true)][string]$Code,
    [ValidateSet('codex', 'claude', 'ollama', 'remote')][string]$Agent,   # none: the kind @pm set up
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}\z')][string]$Model,   # agent_kinds.MODEL_RE
    [ValidatePattern('^https?://[^\s"\\]+\z')][string]$BaseUrl,                # connect.py checks it fully
    [switch]$NewKey
)
Set-StrictMode -Version 3
$ErrorActionPreference = 'Stop'

$Console = if ($env:HAGENCY_CONSOLE) { $env:HAGENCY_CONSOLE } else { '__HAGENCY_CONSOLE__' }
$Repo = if ($env:HAGENCY_REPO) { $env:HAGENCY_REPO } else { 'https://github.com/csheargm/hagency-rooms' }
$App = if ($env:HAGENCY_APP) { $env:HAGENCY_APP } else { Join-Path $env:LOCALAPPDATA 'Hagency\app' }
$HagencyHome = if ($env:HAGENCY_DIR) { $env:HAGENCY_DIR } else { Join-Path $env:USERPROFILE '.hagency' }
$HagencyHome = [IO.Path]::GetFullPath($HagencyHome)   # one absolute folder for this script and both Python entry points
# The keeper a task or shortcut runs; the root one is what tasks made before the launchers moved into
# bin\ name (docs/adr/0012-launchers-in-bin.md). TODO(remove after 2027-02-01): the root entry.
$Keepers = @((Join-Path $App 'bin\connect_win.py'), (Join-Path $App 'connect_win.py'))
$CodePattern = '^[A-Za-z0-9-]{4,32}$'          # what pairing.py codes look like; nothing else reaches python

function Say([string]$Text) { Write-Host $Text }

function Find-Python {
    # The newest Python 3.11+ (the py launcher first), as a full path; '' when there is none.
    $ErrorActionPreference = 'Continue'          # a missing version writes to stderr: not an error here
    $tries = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in '3.13', '3.12', '3.11') { $tries += , @('py', "-$v") }
    }
    $tries += , @('python')
    $tries += , @('python3')
    foreach ($try in $tries) {
        if (-not (Get-Command $try[0] -ErrorAction SilentlyContinue)) { continue }
        $rest = @($try | Select-Object -Skip 1)
        $found = & $try[0] @rest -c 'import sys; sys.version_info >= (3, 11) and print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { return ([string]@($found)[0]).Trim() }
    }
    return ''
}

function Invoke-Checked([string]$What, [scriptblock]$Run) {
    $ErrorActionPreference = 'Continue'          # a native command's stderr is shown; its exit code decides
    & $Run | Out-Host                            # shown, never part of a function's return value
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

function Test-Ours([string]$Text) {
    # Does this command line (a task's, a shortcut's) run this installation's keeper for this folder?
    $has = { param($part) $Text.IndexOf($part, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
    $keeper = @($Keepers | Where-Object { & $has ('"{0}"' -f $_) })
    [bool]($Text -and $keeper.Count -and (& $has ('--home "{0}"' -f $HagencyHome)))
}

function Get-OurTasks {
    # Only tasks that run this installation's keeper for this folder, whatever their name.
    @(Get-ScheduledTask -TaskName 'Hagency agent *' -ErrorAction SilentlyContinue | Where-Object {
        @($_.Actions | Where-Object { Test-Ours $_.Arguments }).Count -gt 0 })
}

function Remove-OurShortcuts([string]$Startup) {
    $shell = New-Object -ComObject WScript.Shell
    Get-ChildItem $Startup -Filter 'Hagency agent *.lnk' -ErrorAction SilentlyContinue |
        Where-Object { Test-Ours $shell.CreateShortcut($_.FullName).Arguments } | Remove-Item -ErrorAction SilentlyContinue
}

function Stop-OldAgent {
    # Before updating: stop the running agent (connect_win.py's job ends the agent adapter with it), so its
    # files and Python aren't in use. agent.pid is the fallback for one started from Startup.
    $ErrorActionPreference = 'Continue'          # nothing to stop is not an error
    $state.Stopped = @(Get-OurTasks)            # started again if this run fails before replacing them
    $state.Stopped | Stop-ScheduledTask -ErrorAction SilentlyContinue
    $shell = New-Object -ComObject WScript.Shell
    $state.Relaunch = @(Get-ChildItem ([Environment]::GetFolderPath('Startup')) -Filter 'Hagency agent *.lnk' -ErrorAction SilentlyContinue |
        ForEach-Object { $shell.CreateShortcut($_.FullName) } | Where-Object { Test-Ours $_.Arguments })
    $pidFile = Join-Path $HagencyHome 'agent.pid'
    if (-not (Test-Path $pidFile)) { return }
    $ours = @($Keepers) + @((Join-Path $App 'bin\bridge.py'), (Join-Path $App 'bridge.py'))
    foreach ($line in Get-Content $pidFile) {
        $id = 0
        if (-not [int]::TryParse($line.Trim(), [ref]$id)) { continue }
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
        # Only a Python running our own keeper or agent adapter: a stale number may belong to something else by now.
        if ($proc -and $proc.Name -like 'python*.exe' -and $proc.CommandLine -and
            @($ours | Where-Object { $proc.CommandLine.IndexOf($_, [StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0) {
            taskkill.exe /PID $id /T /F | Out-Null    # with the CLI it runs; already gone is fine
        }
    }
}

function Install-App {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($env:HAGENCY_APP) { Say "Using $App"; return }
    if ((Test-Path (Join-Path $App '.git')) -and $git) {
        Say "Updating $App"
        Invoke-Checked 'git pull' { git -C $App pull -q --ff-only }
        return
    }
    if (-not (Test-Path $App) -and $git) {
        Say "Installing Hagency Rooms in $App"
        New-Item -ItemType Directory -Force -Path (Split-Path $App) | Out-Null
        Invoke-Checked 'git clone' { git clone -q --depth 1 $Repo $App }
        return
    }
    Say "Downloading Hagency Rooms into $App (no git here)"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('hagency-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp 'app.zip'
        Invoke-WebRequest -UseBasicParsing -Uri "$Repo/archive/refs/heads/master.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp 'x')
        $src = Get-ChildItem (Join-Path $tmp 'x') -Directory | Select-Object -First 1
        if (-not $src) { throw 'the download was empty' }
        New-Item -ItemType Directory -Force -Path $App | Out-Null
        Copy-Item -Path (Join-Path $src.FullName '*') -Destination $App -Recurse -Force
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

function Move-OldKeepers {
    # Right after the update: a task or shortcut Stop-OldAgent stopped may still run the root keeper,
    # which the update just removed (docs/adr/0012-launchers-in-bin.md). Point it at bin\ before
    # anything can fail, so the catch below starts a keeper that exists. TODO(remove after 2027-02-01).
    $old = '"{0}"' -f (Join-Path $App 'connect_win.py')
    $new = '"{0}"' -f (Join-Path $App 'bin\connect_win.py')
    foreach ($task in $state.Stopped) {
        if (-not @($task.Actions | Where-Object { $_.Arguments -and $_.Arguments.Contains($old) }).Count) { continue }
        $actions = @($task.Actions | ForEach-Object {
            New-ScheduledTaskAction -Execute $_.Execute -Argument ([string]$_.Arguments).Replace($old, $new) -WorkingDirectory $_.WorkingDirectory })
        Set-ScheduledTask -TaskName $task.TaskName -Action $actions | Out-Null
    }
    foreach ($link in $state.Relaunch) {
        if ($link.Arguments.Contains($old)) { $link.Arguments = $link.Arguments.Replace($old, $new); $link.Save() }
    }
}

function Install-Venv([string]$Python) {
    $venvPy = Join-Path $App '.venv\Scripts\python.exe'
    if (Test-Path $venvPy) {
        $ok = & $venvPy -c 'import sys; print(sys.version_info >= (3, 11))'
        if ("$ok".Trim() -ne 'True') { Invoke-Checked 'rebuilding the venv' { & $Python -m venv --clear (Join-Path $App '.venv') } }
    } else {
        Invoke-Checked 'creating the venv' { & $Python -m venv (Join-Path $App '.venv') }
    }
    Invoke-Checked 'installing requirements' {
        & $venvPy -m pip install -q --disable-pip-version-check -r (Join-Path $App 'requirements.txt')
    }
    return $venvPy
}

function Test-Backend([string]$Name) {
    # The agent starts the CLI without a shell: a real program (.exe), or npm's .cmd wrapper, whose
    # own program it then starts directly (cli_path.py). A .ps1 wrapper alone isn't enough.
    [bool](Get-Command "$Name.exe", "$Name.cmd" -CommandType Application -ErrorAction SilentlyContinue)
}

function Test-Backends {
    # Before the code is used up: the CLI the chosen kind runs (ollama and remote run through Codex),
    # or with no -Agent at least one. connect.py then picks the kind (@pm's, or asks) and checks the rest.
    $hint = 'Claude Code (irm https://claude.ai/install.ps1 | iex) or Codex (npm i -g @openai/codex)'
    if ($Agent) {
        $cli = if ($Agent -eq 'claude') { 'claude' } else { 'codex' }
        if (-not (Test-Backend $cli)) { throw "$Agent needs $cli, which isn't on this PC's PATH; install $hint first" }
        return
    }
    if (@('codex', 'claude' | Where-Object { Test-Backend $_ }).Count -eq 0) {
        $wrapper = @('codex', 'claude' | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue })
        $why = if ($wrapper.Count) { "found only PowerShell wrappers ($($wrapper -join ', ')), which the agent can't start; " } else { '' }
        throw "$($why)install $hint, then open a new PowerShell and run this line again"
    }
}

function Get-KindArgs {
    # Only what was given: connect.py asks for the rest at this window (an API key never echoed).
    $kindArgs = @()
    if ($Agent) { $kindArgs += '--agent', $Agent }
    if ($Model) { $kindArgs += '--model', $Model }
    if ($BaseUrl) { $kindArgs += '--base-url', $BaseUrl }
    if ($NewKey) { $kindArgs += '--new-key' }
    return , $kindArgs
}

function Protect-Home {
    # Only you (not other accounts on this PC) can read the folder that holds your agent's sign-in:
    # the folder's whole access list is replaced first, then what's inside inherits only that.
    New-Item -ItemType Directory -Force -Path $HagencyHome | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        [Security.Principal.WindowsIdentity]::GetCurrent().User, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    $dir = Get-Item -Force $HagencyHome          # not Set-Acl: it also tries the audit list, which needs admin
    if ($dir.PSObject.Methods['SetAccessControl']) { $dir.SetAccessControl($acl) }       # Windows PowerShell 5.1
    else { [IO.FileSystemAclExtensions]::SetAccessControl($dir, $acl) }                 # PowerShell 7
    if (Get-ChildItem -Force $HagencyHome) {
        Invoke-Checked 'protecting the sign-in folder' { icacls (Join-Path $HagencyHome '*') /reset /t /q | Out-Null }
    }
}

function Register-Agent([string]$VenvPy) {
    $pyw = Join-Path $App '.venv\Scripts\pythonw.exe'
    $runner = Join-Path $App 'bin\connect_win.py'
    $runArgs = '"{0}" --home "{1}"' -f $runner, $HagencyHome
    $name = ([string](& $VenvPy $runner --home $HagencyHome --task-name)).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $name) { throw 'could not read the saved sign-in' }
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $startup = [Environment]::GetFolderPath('Startup')
    # An earlier connection (maybe under another name) goes first: one keeper per computer.
    Get-OurTasks | Where-Object { $_.TaskName -ne $name } | Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue
    Remove-OurShortcuts $startup
    try {
        $action = New-ScheduledTaskAction -Execute $pyw -Argument $runArgs -WorkingDirectory $App
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -RestartCount 255 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
            -Principal $principal -Force | Out-Null
        Start-ScheduledTask -TaskName $name
        Say "Your agent runs in the background now, and again each time you sign in (task: $name)."
    } catch {
        # Some PCs don't let people make tasks: start it from the Startup folder instead (a half-made
        # task is removed; connect_win.py's lock would stop a second keeper anyway).
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $startup "$name.lnk"))
        $shortcut.TargetPath = $pyw
        $shortcut.Arguments = $runArgs
        $shortcut.WorkingDirectory = $App
        $shortcut.Save()
        Start-Process -FilePath $pyw -ArgumentList $runArgs -WorkingDirectory $App -WindowStyle Hidden
        Say "Your agent runs in the background now, and again each time you sign in (Startup: $name)."
    }
    Say "Its log is in $(Join-Path $HagencyHome 'logs'). You can close this window."
}

$savedEncoding = $env:PYTHONIOENCODING
$state = @{ Stopped = @(); Relaunch = @() }    # what Stop-OldAgent stopped; a hashtable, not script scope (the caller's)
try {
    $env:PYTHONIOENCODING = 'utf-8'              # Python's piped output on a cp1252 PC: no crash on its check marks
    if ($Code -notmatch $CodePattern) { throw 'that does not look like a code from @pm (like ABCDE-FGHJK)' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $python = Find-Python
    if (-not $python) {
        throw 'needs Python 3.11 or newer: winget install -e --id Python.Python.3.12, then open a new PowerShell'
    }
    Test-Backends                                # before the code is used up
    $kindArgs = Get-KindArgs
    Stop-OldAgent
    Install-App
    Move-OldKeepers
    $venvPy = Install-Venv $python
    Protect-Home
    Invoke-Checked 'connecting' {
        & $venvPy (Join-Path $App 'bin\connect.py') --server $Console --home $HagencyHome $Code @kindArgs --pair-only
    }
    Register-Agent $venvPy
    $state.Stopped = @()
    $state.Relaunch = @()
} catch {
    # Not `exit`: this runs inside the person's own PowerShell window, which must stay open.
    Write-Host "x $($_.Exception.Message)" -ForegroundColor Red
    $back = 0                                    # a failed rerun (expired code, no network): the old agent comes back
    foreach ($task in $state.Stopped) {
        try { Start-ScheduledTask -TaskName $task.TaskName -ErrorAction Stop; $back++ } catch { }   # gone: counted below
    }
    foreach ($link in $state.Relaunch) {
        try {
            Start-Process -FilePath $link.TargetPath -ArgumentList $link.Arguments -WorkingDirectory $link.WorkingDirectory `
                -WindowStyle Hidden -ErrorAction Stop
            $back++
        } catch { }                              # its files are gone: counted below
    }
    if ($state.Stopped.Count + $state.Relaunch.Count) {
        Write-Host $(if ($back) { 'Your agent was started again as it was.' } else { 'Your agent is stopped: run the line again with a new code.' })
    }
} finally {
    $env:PYTHONIOENCODING = $savedEncoding       # this window's own setting, as it was
}
