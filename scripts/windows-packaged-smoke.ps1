$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$installerRoot = Join-Path $repoRoot "windows-installer"
$backend = Get-ChildItem -Path $installerRoot -Recurse -File -Filter "codex-switch-backend.exe" |
  Select-Object -First 1

if (-not $backend) {
  throw "Could not find packaged backend executable under $installerRoot"
}

$staticRoot = Get-ChildItem -Path $installerRoot -Recurse -Directory |
  Where-Object { $_.FullName -like "*\resources\web\dist" } |
  Select-Object -First 1

if (-not $staticRoot) {
  throw "Could not find packaged web/dist static root under $installerRoot"
}

$testRoot = Join-Path $env:RUNNER_TEMP "codex-switch-windows-smoke"
if (Test-Path $testRoot) {
  Remove-Item -LiteralPath $testRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $testRoot | Out-Null

$localAppData = Join-Path $testRoot "AppData\Local"
$roamingAppData = Join-Path $testRoot "AppData\Roaming"
New-Item -ItemType Directory -Path $localAppData, $roamingAppData | Out-Null

$fakeCodexScript = Join-Path $testRoot "fake-codex-app-server.cjs"
Set-Content -Path $fakeCodexScript -Encoding UTF8 -Value @'
const readline = require("node:readline");

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
  terminal: false
});

function respond(id, result = {}) {
  process.stdout.write(`${JSON.stringify({ id, result })}\n`);
}

rl.on("line", (line) => {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return;
  }

  if (!message.id) {
    return;
  }

  if (message.method === "account/login/start") {
    respond(message.id, {
      authUrl: "https://chat.openai.com/auth/windows-smoke",
      userCode: "WIN-123"
    });
    return;
  }

  if (message.method === "account/read") {
    respond(message.id, {
      account: {
        email: "windows-smoke@example.com",
        planType: "plus",
        type: "chatgpt"
      }
    });
    return;
  }

  if (message.method === "account/rateLimits/read") {
    respond(message.id, {
      rateLimits: {
        primary: {
          usedPercent: 12,
          resetsAt: 1800000000,
          windowDurationMins: 300
        }
      }
    });
    return;
  }

  respond(message.id, {});
});

setInterval(() => {}, 1000);
'@

$fakeCodexCmd = Join-Path $testRoot "codex.cmd"
Set-Content -Path $fakeCodexCmd -Encoding ASCII -Value @"
@echo off
node "%~dp0fake-codex-app-server.cjs" %*
"@

$skeletonRoot = Join-Path $testRoot "llm_accounts_profiles\codex\profiles\skeleton-1"
New-Item -ItemType Directory -Path (Join-Path $skeletonRoot "home\.codex") | Out-Null
Set-Content -Path (Join-Path $skeletonRoot "home\.codex\config.toml") -Encoding UTF8 -Value "readonly skeleton"
Set-ItemProperty -Path (Join-Path $skeletonRoot "home\.codex\config.toml") -Name IsReadOnly -Value $true

$port = 18765
$baseUrl = "http://127.0.0.1:$port"
$process = $null

try {
  $psi = [System.Diagnostics.ProcessStartInfo]::new()
  $psi.FileName = $backend.FullName
  $psi.WorkingDirectory = $testRoot
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.ArgumentList.Add("--host")
  $psi.ArgumentList.Add("127.0.0.1")
  $psi.ArgumentList.Add("--port")
  $psi.ArgumentList.Add([string]$port)
  $psi.ArgumentList.Add("--static-root")
  $psi.ArgumentList.Add($staticRoot.FullName)
  $psi.Environment["USERPROFILE"] = $testRoot
  $psi.Environment["HOME"] = $testRoot
  $psi.Environment["LOCALAPPDATA"] = $localAppData
  $psi.Environment["APPDATA"] = $roamingAppData
  $psi.Environment["CODEX_BINARY"] = $fakeCodexCmd

  $process = [System.Diagnostics.Process]::Start($psi)

  $healthy = $false
  for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if ($process.HasExited) {
      $stderr = $process.StandardError.ReadToEnd()
      throw "Packaged backend exited before health check. stderr: $stderr"
    }

    try {
      $health = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/health" -TimeoutSec 2
      if ($health.ok) {
        $healthy = $true
        break
      }
    } catch {
      Start-Sleep -Milliseconds 250
    }
  }

  if (-not $healthy) {
    throw "Packaged backend did not become healthy at $baseUrl"
  }

  $fixResult = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/diagnostics/fix" -Body "{}" -ContentType "application/json" -TimeoutSec 10
  $fixedText = ($fixResult.fixed -join "`n")
  if ($fixedText -notmatch "Removed 1 skeleton profile: skeleton-1\.") {
    throw "Fix Common Issues did not report skeleton cleanup. Fixed: $fixedText"
  }
  if (Test-Path $skeletonRoot) {
    throw "Fix Common Issues did not remove skeleton profile at $skeletonRoot"
  }

  $addResult = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/accounts/add" -Body "{}" -ContentType "application/json" -TimeoutSec 20
  if ($addResult.pending_oauth_flow.verification_uri -ne "https://chat.openai.com/auth/windows-smoke") {
    throw "Add Account did not expose pending OAuth flow from fake Codex app-server."
  }
  if ($addResult.oauth.user_code -ne "WIN-123") {
    throw "Add Account did not return expected OAuth user code."
  }

  $state = Invoke-RestMethod -Method Get -Uri "$baseUrl/api/state" -TimeoutSec 10
  if ($state.pending_oauth_flow.verification_uri -ne "https://chat.openai.com/auth/windows-smoke") {
    throw "State did not retain pending OAuth flow for the sign-in pop-up."
  }

  Write-Host "Windows packaged smoke test passed for $($backend.FullName)"
} finally {
  if ($process -and -not $process.HasExited) {
    & taskkill /PID $process.Id /T /F | Out-Null
  }

  Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains("fake-codex-app-server.cjs") } |
    ForEach-Object {
      & taskkill /PID $_.ProcessId /T /F | Out-Null
    }
}
