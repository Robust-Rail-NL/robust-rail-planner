[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
$EnvName = "robust-rail-planning"

Set-Location $RepoRoot

function Write-WarningMessage([string]$Message) {
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
}

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "Conda was not found on PATH. Install Miniconda/Miniforge and open a new terminal."
}

Write-Host "Creating/updating Conda environment..."
conda env update --name $EnvName --file (Join-Path $RepoRoot "env.yml") --prune
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create or update Conda environment '$EnvName'."
}

Write-Host "Checking Java and JDK tools (needed by ENHSP)..."
conda run --name $EnvName java -version
if ($LASTEXITCODE -ne 0) {
    Write-WarningMessage "java was not found in the Conda environment; ENHSP will fail."
}
conda run --name $EnvName javac -version
if ($LASTEXITCODE -ne 0) {
    Write-WarningMessage "javac was not found in the Conda environment; ENHSP cannot be built."
}

# Fetch and build the standalone ENHSP planner when it is not already present.
$ToolsDir = Join-Path $RepoRoot "tools\planners"
$EnhspDir = Join-Path $ToolsDir "enhsp"
$EnhspRef = if ($env:ENHSP_REF) { $env:ENHSP_REF } else { "enhsp-20" }
$EnhspRepo = if ($env:ENHSP_REPO) { $env:ENHSP_REPO } else { "https://gitlab.com/enricos83/ENHSP-Public.git" }
$EnhspJar = Join-Path $EnhspDir "enhsp-dist\enhsp.jar"

if (Test-Path $EnhspJar) {
    Write-Host "ENHSP already present at $EnhspJar - skipping download."
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-WarningMessage "git was not found; ENHSP cannot be downloaded."
    } else {
        New-Item -ItemType Directory -Path $ToolsDir -Force | Out-Null

        if (Test-Path $EnhspDir) {
            Write-WarningMessage "A partial ENHSP directory already exists at $EnhspDir. Remove it and rerun setup."
        } else {
            Write-Host "ENHSP not found in tools/ - fetching $EnhspRef..."
            git clone --depth 1 --branch $EnhspRef $EnhspRepo $EnhspDir

            if ($LASTEXITCODE -ne 0) {
                Write-WarningMessage "Failed to clone ENHSP from $EnhspRepo ($EnhspRef)."
            } else {
                Write-Host "Building ENHSP..."
                $OutDir = Join-Path $EnhspDir "out"
                $DistDir = Join-Path $EnhspDir "enhsp-dist"
                New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

                $JavaSources = @(
                    Get-ChildItem (Join-Path $EnhspDir "src\planners\*.java")
                    Get-ChildItem (Join-Path $EnhspDir "src\*.java")
                ) | ForEach-Object { $_.FullName }

                conda run --name $EnvName javac `
                    -encoding utf8 `
                    -d $OutDir `
                    -classpath (Join-Path $EnhspDir "libs\*") `
                    @JavaSources
                if ($LASTEXITCODE -ne 0) {
                    throw "ENHSP Java compilation failed."
                }

                $BuiltJar = Join-Path $EnhspDir "enhsp.jar"
                conda run --name $EnvName jar --create --file $BuiltJar `
                    --manifest (Join-Path $EnhspDir "manifest.mf") `
                    -C $OutDir .
                if ($LASTEXITCODE -ne 0) {
                    throw "Failed to package ENHSP jar."
                }

                New-Item -ItemType Directory -Path $DistDir -Force | Out-Null
                Copy-Item (Join-Path $EnhspDir "libs") $DistDir -Recurse -Force
                Copy-Item $BuiltJar $EnhspJar -Force
                Write-Host "ENHSP built: $EnhspJar"
            }
        }
    }
}

if (Test-Path $EnhspJar) {
    $env:ENHSP_HOME = $EnhspDir
    Write-Host "ENHSP_HOME=$env:ENHSP_HOME"
    Write-Host "Run directly with: java -jar `"$EnhspJar`" -o <domain.pddl> -f <problem.pddl>"
}

Write-Host "Checking which planning engines are registered..."
$EngineCheck = 'from unified_planning.shortcuts import get_environment; engines=sorted(get_environment().factory.engines); print(engines); target=bytes((101,110,104,115,112)).decode(); assert target in engines'
$CondaEnvironments = (conda env list --json | ConvertFrom-Json).envs
$EnvironmentPath = $CondaEnvironments | Where-Object { (Split-Path $_ -Leaf) -eq $EnvName } | Select-Object -First 1
if (-not $EnvironmentPath) {
    throw "Conda environment '$EnvName' was not found after installation."
}
$EnvironmentPython = Join-Path $EnvironmentPath "python.exe"
& $EnvironmentPython -c $EngineCheck
if ($LASTEXITCODE -ne 0) {
    throw "The Unified Planning ENHSP engine is not registered."
}
Write-Host "  enhsp OK"

if (-not (Get-Command julia -ErrorAction SilentlyContinue)) {
    throw "Julia was not found on PATH. Install Julia and rerun setup."
}

Write-Host "Installing Julia dependencies..."
julia --project=$RepoRoot -e 'using Pkg; Pkg.resolve(); Pkg.instantiate(); Pkg.precompile(); Pkg.status()'
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Julia dependencies."
}

Write-Host "Setup complete."
Write-Host "Activate the environment with: conda activate $EnvName"
