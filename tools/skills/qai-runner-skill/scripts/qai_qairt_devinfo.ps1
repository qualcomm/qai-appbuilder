# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
param(
    [string]$SdkRoot,
    [switch]$Json,
    [switch]$VerboseValidator
)

$ErrorActionPreference = "Stop"

function Find-QairtSdkRoot {
    if ($SdkRoot) {
        return $SdkRoot
    }

    if ($env:QAIRT_SDK_ROOT) {
        return $env:QAIRT_SDK_ROOT
    }

    $sdkBase = "C:\Qualcomm\AIStack\QAIRT"
    if (Test-Path $sdkBase) {
        $latest = Get-ChildItem $sdkBase -Directory |
            Where-Object { $_.Name -match "^\d+\.\d+\.\d+\.\d+$" } |
            Sort-Object Name -Descending |
            Select-Object -First 1

        if ($latest) {
            return $latest.FullName
        }
    }

    throw "QAIRT_SDK_ROOT is not set. Run: . 'C:\Qualcomm\AIStack\QAIRT\<version>\bin\envsetup.ps1', or pass -SdkRoot explicitly."
}

function Get-RegistryCpuName {
    $keyPath = "HKLM:\HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    try {
        $cpu = Get-ItemProperty -LiteralPath $keyPath -Name ProcessorNameString -ErrorAction Stop
        return [string]$cpu.ProcessorNameString
    } catch {
        return $null
    }
}

function Find-QairtDriverInfo {
    $driverRoot = "C:\WINDOWS\System32\DriverStore\FileRepository"
    if (-not (Test-Path $driverRoot)) {
        return [pscustomobject]@{
            DriverDirectory = $null
            DriverInf       = $null
            DriverFamily    = $null
            DriverDigits    = $null
            DeviceLines     = @()
        }
    }

    $driverDirs = Get-ChildItem $driverRoot -Directory -Filter "qcnspmcdm*.inf_*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending

    foreach ($dir in $driverDirs) {
        $inf = Get-ChildItem $dir.FullName -Filter "*.inf" -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $inf) {
            continue
        }

        $infText = Get-Content $inf.FullName -ErrorAction SilentlyContinue
        $familyDigits = $null
        foreach ($candidate in @($dir.Name, $inf.Name, ($infText -join "`n"))) {
            if ($candidate -match "(?i)(?:qcnspmcdm|cdsp|sc)(\d{3,5})") {
                $familyDigits = $Matches[1]
                break
            }
        }
        $deviceLines = @($infText | Where-Object { $_ -match "DeviceDesc|Snapdragon|Qualcomm.*Hexagon|ACPI\\VEN_QCOM" })

        return [pscustomobject]@{
            DriverDirectory = $dir.FullName
            DriverInf       = $inf.FullName
            DriverFamily    = if ($familyDigits) { "SC$familyDigits" } else { $null }
            DriverDigits    = $familyDigits
            DeviceLines     = $deviceLines
        }
    }

    [pscustomobject]@{
        DriverDirectory = $null
        DriverInf       = $null
        DriverFamily    = $null
        DriverDigits    = $null
        DeviceLines     = @()
    }
}

function Get-DeviceTokens {
    param([string[]]$Text)

    $tokens = New-Object System.Collections.Generic.List[string]
    foreach ($item in $Text) {
        if (-not $item) {
            continue
        }

        foreach ($match in [regex]::Matches($item.ToUpperInvariant(), "[A-Z]{1,4}\d{3,5}[A-Z0-9]*")) {
            $tokens.Add($match.Value)
        }
    }

    $tokens | Select-Object -Unique
}

function Resolve-QairtSocModel {
    param(
        [Parameter(Mandatory = $true)]
        [array]$SocTable,

        [string]$CpuName,
        [string]$DriverFamily,
        [string]$DriverDigits,
        [string[]]$DriverDeviceLines
    )

    $evidence = @($CpuName, $DriverFamily) + $DriverDeviceLines
    $tokens = Get-DeviceTokens -Text $evidence
    $candidates = @()

    foreach ($model in $SocTable) {
        $score = 0
        $reasons = New-Object System.Collections.Generic.List[string]

        foreach ($token in $tokens) {
            if ($model.Name -eq $token) {
                $score += 100
                $reasons.Add("exact token $token")
            } elseif ($model.Name.StartsWith($token) -or $token.StartsWith($model.Name)) {
                $score += 60
                $reasons.Add("prefix token $token")
            }
        }

        if ($DriverFamily -and $model.Name -eq $DriverFamily) {
            $score += 90
            $reasons.Add("driver family $DriverFamily")
        } elseif ($DriverFamily -and ($model.Name.StartsWith($DriverFamily) -or $DriverFamily.StartsWith($model.Name))) {
            $score += 70
            $reasons.Add("driver family prefix $DriverFamily")
        }

        if ($DriverDigits -and $model.Name -match [regex]::Escape($DriverDigits)) {
            $score += 35
            $reasons.Add("driver digits $DriverDigits")
        }

        if ($score -gt 0) {
            $candidates += [pscustomobject]@{
                SocModel = $model.SocModel
                SocId    = $model.SocId
                Score    = $score
                Reasons  = ($reasons -join "; ")
            }
        }
    }

    $best = $candidates | Sort-Object Score, SocId -Descending | Select-Object -First 1
    if ($best) {
        return [pscustomobject]@{
            SocModel   = $best.SocModel
            SocId      = $best.SocId
            Confidence = if ($best.Score -ge 90) { "high" } elseif ($best.Score -ge 60) { "medium" } else { "low" }
            Source     = $best.Reasons
            Candidates = $candidates | Sort-Object Score, SocId -Descending
        }
    }

    [pscustomobject]@{
        SocModel   = $null
        SocId      = $null
        Confidence = "none"
        Source     = "No QAIRT SoC enum matched discovered CPU or driver evidence"
        Candidates = @()
    }
}

function Get-SocIdAndDspArch {
    param(
        [string]$CpuName,
        [string]$DriverFamily,
        [string[]]$DriverDeviceLines,
        [string]$QnnTypesPath
    )

    $socTable = @()
    Get-Content $QnnTypesPath | ForEach-Object {
        if ($_ -match "^\s*(QNN_SOC_MODEL_([A-Z0-9_]+))\s*=\s*(\d+)\s*,") {
            $socTable += [pscustomobject]@{
                SocModel = $Matches[1]
                Name     = $Matches[2]
                SocId    = [int]$Matches[3]
            }
        }
    }

    $driverDigits = $null
    if ($DriverFamily -match "SC(\d+)") {
        $driverDigits = $Matches[1]
    }

    $soc = Resolve-QairtSocModel `
        -SocTable $socTable `
        -CpuName $CpuName `
        -DriverFamily $DriverFamily `
        -DriverDigits $driverDigits `
        -DriverDeviceLines $DriverDeviceLines

    $dspArch = $null
    $coreVersion = $null

    return [pscustomobject]@{
        SocModel   = $soc.SocModel
        SocId      = $soc.SocId
        Confidence = $soc.Confidence
        Source     = $soc.Source
        Candidates = $soc.Candidates
        DspArch    = $dspArch
        CoreVersion = $coreVersion
    }
}

function Find-QairtTool {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,

        [Parameter(Mandatory = $true)]
        [string]$ToolName
    )

    foreach ($target in @("x86_64-windows-msvc", "arm64x-windows-msvc", "aarch64-windows-msvc")) {
        foreach ($name in @("$ToolName.exe", $ToolName)) {
            $tool = Join-Path $Root "bin\$target\$name"
            if (Test-Path $tool) {
                return [pscustomobject]@{
                    Path   = $tool
                    Target = $target
                    BinDir = (Join-Path $Root "bin\$target")
                    LibDir = (Join-Path $Root "lib\$target")
                }
            }
        }
    }

    throw "$ToolName was not found under QAIRT SDK root: $Root"
}

$root = Find-QairtSdkRoot
$qnnTypes = Join-Path $root "include\QNN\QnnTypes.h"
if (-not (Test-Path $qnnTypes)) {
    throw "QnnTypes.h not found: $qnnTypes"
}

$cpuName = Get-RegistryCpuName
$driver = Find-QairtDriverInfo
$socInfo = Get-SocIdAndDspArch -CpuName $cpuName -DriverFamily $driver.DriverFamily -DriverDeviceLines $driver.DeviceLines -QnnTypesPath $qnnTypes
$validator = Find-QairtTool -Root $root -ToolName "qnn-platform-validator"

$oldPath = $env:PATH
if (Test-Path $validator.LibDir) {
    $env:PATH = "$($validator.BinDir);$($validator.LibDir);$env:PATH"
} else {
    $env:PATH = "$($validator.BinDir);$env:PATH"
}

try {
    $validatorArgs = @("--backend", "dsp", "--coreVersion", "--libVersion")
    if ($VerboseValidator) {
        $validatorArgs += "--debug"
    }

    $oldEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $validatorOutput = & $validator.Path @validatorArgs 2>&1
    $validatorExitCode = $LASTEXITCODE
    $ErrorActionPreference = $oldEAP
} catch {
    $validatorOutput = $_.Exception.Message
    $validatorExitCode = 1
} finally {
    $env:PATH = $oldPath
}

if (-not $socInfo.DspArch) {
    $coreLine = $validatorOutput | Where-Object { $_ -match "Core Version of the backend DSP:" } | Select-Object -First 1
    if ($coreLine -and $coreLine -match "Core Version of the backend DSP:\s*(.+)$") {
        $coreVersion = $Matches[1].Trim()
        if ($coreVersion -match "V(\d+)") {
            $socInfo.DspArch = ("v{0}" -f $Matches[1]).ToLowerInvariant()
        }
    }
}

$result = [pscustomobject]@{
    CpuName              = $cpuName
    QairtSdkRoot         = $root
    QairtToolTarget      = $validator.Target
    SocModel             = $socInfo.SocModel
    SocId                = $socInfo.SocId
    SocConfidence        = $socInfo.Confidence
    SocSource            = $socInfo.Source
    DspArch              = $socInfo.DspArch
    DriverFamily         = $driver.DriverFamily
    DriverInf            = $driver.DriverInf
    ValidatorSucceeded   = ($validatorExitCode -eq 0)
    ValidatorOutput      = $validatorOutput
    SocCandidates        = $socInfo.Candidates
}

if ($Json) {
    $result | ConvertTo-Json -Depth 5
} else {
    $result | Format-List CpuName, QairtSdkRoot, QairtToolTarget, SocModel, SocId, SocConfidence, SocSource, DspArch, DriverFamily, DriverInf, ValidatorSucceeded
}
