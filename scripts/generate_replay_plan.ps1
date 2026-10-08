param(
    [string]$Location,
    [string]$Scenario,
    [ValidateSet("symbolic", "symbolic-rail")]
    [string]$Planner = "symbolic-rail",
    [string]$InputRoot,
    [string]$OutputDirectory,
    [string]$PythonExecutable,
    [string]$JuliaExecutable
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$branch = git -C $repoRoot branch --show-current

function Get-RelativeFilePath($basePath, $targetPath) {
    $base = [System.IO.Path]::GetFullPath($basePath).TrimEnd('\', '/')
    $target = [System.IO.Path]::GetFullPath($targetPath)
    if ($target.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $target.Substring($base.Length).TrimStart('\', '/')
    }
    return $target
}

function Select-File($items, $label, $displayRoot) {
    if (-not $items) {
        throw "No $label files were found."
    }

    Write-Host ""
    Write-Host "${label}:"
    for ($index = 0; $index -lt $items.Count; $index++) {
        $relative = Get-RelativeFilePath $displayRoot $items[$index].FullName
        Write-Host "  $($index + 1). $relative"
    }

    $selection = Read-Host "Select [1]"
    if ([string]::IsNullOrWhiteSpace($selection)) {
        $selection = "1"
    }

    $number = 0
    if (-not [int]::TryParse($selection, [ref]$number) -or
        $number -lt 1 -or $number -gt $items.Count) {
        throw "Invalid $label selection: $selection"
    }
    return $items[$number - 1].FullName
}

function Test-UnifiedPlanning($python) {
    & $python -c "import unified_planning" 2>$null
    return $LASTEXITCODE -eq 0
}

if (-not $InputRoot) {
    $InputRoot = Join-Path $repoRoot "..\..\Robust-Rail-NL\scenario-planning-inputs"
}
$InputRoot = (Resolve-Path $InputRoot).Path

if (-not $Location) {
    $locationFiles = @(
        Get-ChildItem -Path $InputRoot -Filter "location.json" -File -Recurse |
            Sort-Object FullName
    )
    $Location = Select-File $locationFiles "Location" $InputRoot
}
$locationPath = (Resolve-Path $Location).Path
$locationDirectory = Split-Path $locationPath -Parent

if (-not $Scenario) {
    $scenarioFiles = @(
        foreach ($folder in @("fixtures", "scenarios")) {
            $scenarioRoot = Join-Path $locationDirectory $folder
            if (Test-Path $scenarioRoot) {
                Get-ChildItem -Path $scenarioRoot -Filter "scenario*.json" -File -Recurse
            }
        }
    )
    $scenarioFiles = @($scenarioFiles | Sort-Object FullName)
    $scenarioGroups = @(
        $scenarioFiles | Group-Object {
            Split-Path (Get-RelativeFilePath $locationDirectory $_.FullName) -Parent
        } | Sort-Object Name
    )

    if ($scenarioGroups.Count -gt 1) {
        Write-Host ""
        Write-Host "Scenario set:"
        for ($index = 0; $index -lt $scenarioGroups.Count; $index++) {
            Write-Host "  $($index + 1). $($scenarioGroups[$index].Name) ($($scenarioGroups[$index].Count))"
        }
        $selection = Read-Host "Select [1]"
        if ([string]::IsNullOrWhiteSpace($selection)) {
            $selection = "1"
        }
        $number = 0
        if (-not [int]::TryParse($selection, [ref]$number) -or
            $number -lt 1 -or $number -gt $scenarioGroups.Count) {
            throw "Invalid scenario set selection: $selection"
        }
        $scenarioFiles = @($scenarioGroups[$number - 1].Group)
    }
    $Scenario = Select-File $scenarioFiles "Scenario" $locationDirectory
}
$scenarioPath = (Resolve-Path $Scenario).Path

if (-not $OutputDirectory) {
    $safeBranch = $branch -replace '[^a-zA-Z0-9_.-]', '_'
    $scenarioName = [System.IO.Path]::GetFileNameWithoutExtension($scenarioPath)
    $OutputDirectory = Join-Path $env:TEMP "planning-replay\$safeBranch\$scenarioName"
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path $OutputDirectory).Path

if (-not $JuliaExecutable) {
    $installedJulia = @(
        Get-ChildItem "$env:LOCALAPPDATA\Programs\Julia-*\bin\julia.exe" -ErrorAction SilentlyContinue
        Get-ChildItem "$env:USERPROFILE\AppData\Local\Programs\Julia-*\bin\julia.exe" -ErrorAction SilentlyContinue
    ) | Sort-Object FullName -Descending -Unique | Select-Object -First 1
    if ($installedJulia) {
        $JuliaExecutable = $installedJulia.FullName
    } else {
        $JuliaExecutable = "julia"
    }
}

if (-not $PythonExecutable) {
    $workspaceRoot = (Resolve-Path (Join-Path $repoRoot "..\..")).Path
    $venvRoot = Join-Path $env:LOCALAPPDATA "planning-approach-replay\venv"
    $pythonCandidates = @(
        (Join-Path $repoRoot ".venv\Scripts\python.exe"),
        (Join-Path $workspaceRoot "tmp\coupling-track-experiment-artifacts\.experiment-venv\Scripts\python.exe"),
        (Join-Path $venvRoot "Scripts\python.exe")
    ) | Where-Object { Test-Path $_ }

    foreach ($candidate in $pythonCandidates) {
        if (Test-UnifiedPlanning $candidate) {
            $PythonExecutable = $candidate
            break
        }
    }

    if (-not $PythonExecutable) {
        $PythonExecutable = Join-Path $venvRoot "Scripts\python.exe"
        $bootstrapCandidates = @(
            $(if ($env:CONDA_PREFIX) { Join-Path $env:CONDA_PREFIX "python.exe" }),
            (Join-Path $env:USERPROFILE "miniforge3\python.exe"),
            $(Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
        ) | Where-Object { $_ -and (Test-Path $_) }
        $bootstrapPython = $bootstrapCandidates | Select-Object -First 1
        if (-not $bootstrapPython) {
            throw "Python was not found. Install Python or pass -PythonExecutable."
        }

        Write-Host "Creating replay environment at $venvRoot"
        & $bootstrapPython -m venv $venvRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the replay Python environment."
        }

        Write-Host "Installing replay dependencies..."
        & $PythonExecutable -m pip install -r (Join-Path $repoRoot "requirements.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "Could not install the replay Python dependencies."
        }
    }
} elseif (-not (Test-UnifiedPlanning $PythonExecutable)) {
    throw "The selected Python does not provide unified_planning."
}

$domain = Join-Path $OutputDirectory "domain.pddl"
$problem = Join-Path $OutputDirectory "problem.pddl"
$plan = Join-Path $OutputDirectory "plan.plan"
$converter = Join-Path $repoRoot "convert_to_pddl\corridor_no_switch_unlimited_order_servicing_discrete_compiled_matching\convert.py"
$plannerScript = Join-Path $repoRoot "plan\symbolic_planner.jl"
$juliaProject = Join-Path $repoRoot "plan"

& $PythonExecutable $converter `
    --location-file $locationPath `
    --scenario-file $scenarioPath `
    --domain-file $domain `
    --output-file $problem
if ($LASTEXITCODE -ne 0) {
    throw "PDDL conversion failed with exit code $LASTEXITCODE"
}

& $JuliaExecutable "--project=$juliaProject" $plannerScript `
    $domain $problem $plan $Planner
if ($LASTEXITCODE -ne 0) {
    throw "Symbolic planning failed with exit code $LASTEXITCODE"
}

Write-Host ""
Write-Host "Replay files written to $OutputDirectory"
Write-Host "Run:"
Write-Host "& `"$JuliaExecutable`" --project=`"$juliaProject`" `"$repoRoot\plan\replay_plan.jl`" `"$domain`" `"$problem`" `"$plan`""
