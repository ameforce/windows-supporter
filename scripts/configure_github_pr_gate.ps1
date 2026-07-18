[CmdletBinding()]
param(
    [ValidateSet("Plan", "Apply", "Verify", "Disable", "Restore", "DeleteLane")]
    [string]$Mode = "Plan",
    [string]$Repository = "ameforce/windows-supporter",
    [string]$DesiredPath = ".github/pr-gate/ruleset.json",
    [string]$ActiveConfigPath = ".github/pr-gate/active-release.json",
    [string]$StateDirectory = (Join-Path $env:TEMP "windows-supporter-pr-gate"),
    [string]$RestorePath,
    [string]$LaneRef
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-GhJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $resolvedArguments = @($Arguments[0], "-H", "X-GitHub-Api-Version: 2026-03-10") + @($Arguments | Select-Object -Skip 1)
    $output = & gh @resolvedArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "gh $($Arguments -join ' ') failed: $($output -join [Environment]::NewLine)"
    }
    $text = $output -join [Environment]::NewLine
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    return $text | ConvertFrom-Json
}

function Get-CanonicalRulesetJson {
    param([Parameter(Mandatory = $true)]$Ruleset)

    $canonical = [ordered]@{
        name = $Ruleset.name
        target = $Ruleset.target
        enforcement = $Ruleset.enforcement
        bypass_actors = @($Ruleset.bypass_actors)
        conditions = $Ruleset.conditions
        rules = @($Ruleset.rules)
    }
    return $canonical | ConvertTo-Json -Depth 100 -Compress
}

function Get-TextSha256 {
    param([AllowNull()][string]$Text)

    $bytes = [Text.Encoding]::UTF8.GetBytes($(if ($null -eq $Text) { "<absent>" } else { $Text }))
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $algorithm.ComputeHash($bytes)
    } finally {
        $algorithm.Dispose()
    }
    return (($hash | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Save-JsonSnapshot {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    [IO.Directory]::CreateDirectory($StateDirectory) | Out-Null
    $timestamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $path = Join-Path $StateDirectory "$timestamp-$Label.json"
    [IO.File]::WriteAllText(
        $path,
        ($Value | ConvertTo-Json -Depth 100),
        [Text.UTF8Encoding]::new($false)
    )
    return $path
}

function Get-RepositoryRulesets {
    $value = Invoke-GhJson -Arguments @("api", "repos/$Repository/rulesets", "--paginate")
    return @($value)
}

function Get-StableRuleset {
    param([Parameter(Mandatory = $true)][string]$Name)

    $matches = @(Get-RepositoryRulesets | Where-Object { $_.name -eq $Name })
    if ($matches.Count -gt 1) {
        throw "Duplicate rulesets found for stable name '$Name'. Refusing to mutate GitHub state."
    }
    if ($matches.Count -eq 0) {
        return $null
    }
    return Invoke-GhJson -Arguments @("api", "repos/$Repository/rulesets/$($matches[0].id)")
}

function Assert-EffectiveRules {
    param([Parameter(Mandatory = $true)][string[]]$BranchNames)

    foreach ($branchName in $BranchNames) {
        $encodedBranch = [Uri]::EscapeDataString($branchName)
        $rules = @(Invoke-GhJson -Arguments @("api", "repos/$Repository/rules/branches/$encodedBranch"))
        $types = @($rules | ForEach-Object { $_.type })
        foreach ($requiredType in @("pull_request", "required_status_checks", "non_fast_forward", "deletion")) {
            if ($requiredType -notin $types) {
                throw "Effective rules for '$branchName' do not include '$requiredType'."
            }
        }
        $statusRule = $rules | Where-Object { $_.type -eq "required_status_checks" } | Select-Object -First 1
        $contexts = @($statusRule.parameters.required_status_checks | ForEach-Object { $_.context })
        foreach ($context in @("pr-policy-gate", "pr-quality-gate")) {
            if ($context -notin $contexts) {
                throw "Effective rules for '$branchName' do not require '$context'."
            }
        }
        if (-not $statusRule.parameters.strict_required_status_checks_policy) {
            throw "Effective rules for '$branchName' do not use strict required status checks."
        }
        if (-not $statusRule.parameters.do_not_enforce_on_create) {
            throw "Effective rules for '$branchName' do not preserve initial lane creation."
        }
    }
}

function Get-RemoteBranchRef {
    param([Parameter(Mandatory = $true)][string]$BranchName)

    $encodedBranch = [Uri]::EscapeDataString($BranchName)
    return Invoke-GhJson -Arguments @("api", "repos/$Repository/git/ref/heads/$encodedBranch")
}

function Assert-LaneIntegrated {
    param(
        [Parameter(Mandatory = $true)][string]$BranchName,
        [Parameter(Mandatory = $true)][string]$BranchSha
    )

    $encodedBranch = [Uri]::EscapeDataString($BranchName)
    foreach ($target in @("main", "develop")) {
        $comparison = Invoke-GhJson -Arguments @(
            "api", "repos/$Repository/compare/$encodedBranch...$target"
        )
        if ($comparison.status -notin @("ahead", "identical")) {
            throw "Remote '$BranchName' is not an ancestor of '$target'."
        }
        if ($comparison.merge_base_commit.sha -ne $BranchSha) {
            throw "Remote '$target' does not contain the exact '$BranchName' tip $BranchSha."
        }
    }
}

function Assert-RemoteBranchMissing {
    param([Parameter(Mandatory = $true)][string]$BranchName)

    $encodedBranch = [Uri]::EscapeDataString($BranchName)
    $matches = @(Invoke-GhJson -Arguments @(
        "api", "repos/$Repository/git/matching-refs/heads/$encodedBranch"
    ))
    if ($matches | Where-Object { $_.ref -eq "refs/heads/$BranchName" }) {
        throw "Remote branch '$BranchName' still exists after deletion."
    }
}

function Assert-FreezeEffective {
    param([Parameter(Mandatory = $true)][string]$BranchName)

    $encodedBranch = [Uri]::EscapeDataString($BranchName)
    $rules = @(Invoke-GhJson -Arguments @("api", "repos/$Repository/rules/branches/$encodedBranch"))
    $types = @($rules | ForEach-Object { $_.type })
    foreach ($requiredType in @("creation", "update")) {
        if ($requiredType -notin $types) {
            throw "Delete freeze for '$BranchName' does not include '$requiredType'."
        }
    }
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "gh CLI was not found."
}

$desiredFullPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\$DesiredPath"))
if (-not (Test-Path -LiteralPath $desiredFullPath -PathType Leaf)) {
    throw "Desired ruleset JSON was not found: $desiredFullPath"
}
$desired = Get-Content -LiteralPath $desiredFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
$existing = Get-StableRuleset -Name $desired.name
$desiredCanonical = Get-CanonicalRulesetJson -Ruleset $desired
$existingCanonical = if ($null -eq $existing) { $null } else { Get-CanonicalRulesetJson -Ruleset $existing }

if ($Mode -eq "Plan") {
    [ordered]@{
        repository = $Repository
        stable_name = $desired.name
        action = if ($null -eq $existing) { "create" } elseif ($desiredCanonical -eq $existingCanonical) { "none" } else { "update" }
        current_digest = Get-TextSha256 -Text $existingCanonical
        desired_digest = Get-TextSha256 -Text $desiredCanonical
        current = $existingCanonical
        desired = $desiredCanonical
    } | ConvertTo-Json -Depth 100
    exit 0
}

if ($Mode -eq "Verify") {
    if ($null -eq $existing) {
        throw "Ruleset '$($desired.name)' does not exist."
    }
    if ($desiredCanonical -ne $existingCanonical) {
        throw "Remote ruleset differs from the checked-in desired state."
    }
    Assert-EffectiveRules -BranchNames @("hotfix/v9.9.9", "release/v9.9.9")
    Write-Output "Verified desired and effective rules for $Repository."
    exit 0
}

if ($Mode -eq "Disable") {
    if ($null -eq $existing) {
        Write-Output "Ruleset '$($desired.name)' is already absent."
        exit 0
    }
    $snapshotPath = Save-JsonSnapshot -Value $existing -Label "before-disable"
    $disabled = $existing | Select-Object name, target, enforcement, bypass_actors, conditions, rules
    $disabled.enforcement = "disabled"
    $disabledPath = Save-JsonSnapshot -Value $disabled -Label "disabled-request"
    Invoke-GhJson -Arguments @(
        "api", "repos/$Repository/rulesets/$($existing.id)", "--method", "PUT", "--input", $disabledPath
    ) | Out-Null
    Write-Output "Disabled ruleset '$($desired.name)'. Restore snapshot: $snapshotPath"
    exit 0
}

if ($Mode -eq "Restore") {
    if ([string]::IsNullOrWhiteSpace($RestorePath) -or -not (Test-Path -LiteralPath $RestorePath -PathType Leaf)) {
        throw "-RestorePath must point to an exported ruleset JSON file."
    }
    $restore = Get-Content -LiteralPath $RestorePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $restoreRequest = $restore | Select-Object name, target, enforcement, bypass_actors, conditions, rules
    $restoreRequestPath = Save-JsonSnapshot -Value $restoreRequest -Label "restore-request"
    $current = Get-StableRuleset -Name $restoreRequest.name
    if ($null -eq $current) {
        Invoke-GhJson -Arguments @(
            "api", "repos/$Repository/rulesets", "--method", "POST", "--input", $restoreRequestPath
        ) | Out-Null
    } else {
        Invoke-GhJson -Arguments @(
            "api", "repos/$Repository/rulesets/$($current.id)", "--method", "PUT", "--input", $restoreRequestPath
        ) | Out-Null
    }
    Write-Output "Restored ruleset '$($restoreRequest.name)' from $RestorePath."
    exit 0
}

if ($Mode -eq "DeleteLane") {
    if ($LaneRef -cnotmatch '^(hotfix|release)/v\d+\.\d+\.\d+$') {
        throw "-LaneRef must be an exact versioned hotfix/vX.Y.Z or release/vX.Y.Z branch."
    }
    if ($null -eq $existing -or $desiredCanonical -ne $existingCanonical) {
        throw "DeleteLane requires the checked-in canonical ruleset to be active before mutation."
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git was not found."
    }
    $originUrl = (& git remote get-url origin 2>&1) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to resolve the origin remote: $originUrl"
    }
    $originUrl = $originUrl.Trim()
    $allowedOriginUrls = @(
        "https://github.com/$Repository",
        "https://github.com/$Repository.git",
        "git@github.com:$Repository",
        "git@github.com:$Repository.git"
    )
    if ($originUrl -notin $allowedOriginUrls) {
        throw "origin '$originUrl' does not match repository '$Repository'."
    }

    $activeConfigFullPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\$ActiveConfigPath"))
    if (-not (Test-Path -LiteralPath $activeConfigFullPath -PathType Leaf)) {
        throw "Active release config was not found: $activeConfigFullPath"
    }
    $activeConfig = Get-Content -LiteralPath $activeConfigFullPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $remoteRepository = Invoke-GhJson -Arguments @("api", "repos/$Repository")
    if (
        [string]$remoteRepository.id -ne [string]$activeConfig.repository_id -or
        [string]$remoteRepository.full_name -cne [string]$activeConfig.repository_full_name
    ) {
        throw "Remote repository numeric identity does not match active-release.json."
    }

    $remoteRef = Get-RemoteBranchRef -BranchName $LaneRef
    if ([string]$remoteRef.ref -cne "refs/heads/$LaneRef") {
        throw "GitHub returned a different ref for '$LaneRef'."
    }
    $laneSha = [string]$remoteRef.object.sha
    if ($laneSha -notmatch '^[0-9a-f]{40}$') {
        throw "Remote lane '$LaneRef' does not resolve to a commit SHA."
    }
    Assert-LaneIntegrated -BranchName $LaneRef -BranchSha $laneSha

    $preMutation = Get-StableRuleset -Name $desired.name
    if ($null -eq $preMutation -or (Get-CanonicalRulesetJson -Ruleset $preMutation) -ne $existingCanonical) {
        throw "Ruleset state changed before lane deletion. Refusing stale mutation."
    }

    $snapshotPath = Save-JsonSnapshot -Value $existing -Label "before-delete-lane"
    $temporary = (($existing | Select-Object name, target, enforcement, bypass_actors, conditions, rules) |
        ConvertTo-Json -Depth 100 | ConvertFrom-Json)
    $exactRef = "refs/heads/$LaneRef"
    $temporary.conditions.ref_name.exclude = @(
        @($temporary.conditions.ref_name.exclude) + $exactRef | Select-Object -Unique
    )
    $temporaryCanonical = Get-CanonicalRulesetJson -Ruleset $temporary
    $temporaryPath = Save-JsonSnapshot -Value $temporary -Label "exclude-delete-lane"
    $freezeName = "windows-supporter-delete-freeze-$($laneSha.Substring(0, 12))"
    if ($null -ne (Get-StableRuleset -Name $freezeName)) {
        throw "A stale delete freeze ruleset already exists: $freezeName"
    }
    $freeze = [ordered]@{
        name = $freezeName
        target = "branch"
        enforcement = "active"
        bypass_actors = @()
        conditions = [ordered]@{
            ref_name = [ordered]@{
                include = @($exactRef)
                exclude = @()
            }
        }
        rules = @(
            [ordered]@{ type = "creation" },
            [ordered]@{ type = "update" }
        )
    }
    $freezeCanonical = Get-CanonicalRulesetJson -Ruleset $freeze
    $freezePath = Save-JsonSnapshot -Value $freeze -Label "delete-lane-freeze"
    $freezeId = $null
    $operationFailure = $null
    $restoreFailure = $null
    $freezeCleanupFailure = $null

    try {
        $freezeCreateFailure = $null
        try {
            $createdFreeze = Invoke-GhJson -Arguments @(
                "api", "repos/$Repository/rulesets", "--method", "POST", "--input", $freezePath
            )
            $freezeId = $createdFreeze.id
        } catch {
            $freezeCreateFailure = $_
        }
        $freezeRemote = Get-StableRuleset -Name $freezeName
        if ($null -eq $freezeRemote -or (Get-CanonicalRulesetJson -Ruleset $freezeRemote) -ne $freezeCanonical) {
            if ($null -ne $freezeCreateFailure) {
                throw "Delete freeze creation failed and was not adopted: $freezeCreateFailure"
            }
            throw "GitHub did not persist the exact-ref delete freeze."
        }
        $freezeId = $freezeRemote.id
        Assert-FreezeEffective -BranchName $LaneRef

        $frozenRef = Get-RemoteBranchRef -BranchName $LaneRef
        if ([string]$frozenRef.ref -cne $exactRef) {
            throw "GitHub returned a different ref after delete freeze activation."
        }
        $laneSha = [string]$frozenRef.object.sha
        if ($laneSha -cnotmatch '^[0-9a-f]{40}$') {
            throw "Frozen lane '$LaneRef' does not resolve to a commit SHA."
        }
        Assert-LaneIntegrated -BranchName $LaneRef -BranchSha $laneSha

        $beforeExclusion = Get-StableRuleset -Name $desired.name
        if ($null -eq $beforeExclusion -or (Get-CanonicalRulesetJson -Ruleset $beforeExclusion) -ne $desiredCanonical) {
            throw "Canonical ruleset changed after delete freeze activation."
        }
        $temporaryPutFailure = $null
        try {
            Invoke-GhJson -Arguments @(
                "api", "repos/$Repository/rulesets/$($existing.id)", "--method", "PUT", "--input", $temporaryPath
            ) | Out-Null
        } catch {
            $temporaryPutFailure = $_
        }
        $temporaryRemote = Get-StableRuleset -Name $desired.name
        if ($null -eq $temporaryRemote -or (Get-CanonicalRulesetJson -Ruleset $temporaryRemote) -ne $temporaryCanonical) {
            if ($null -ne $temporaryPutFailure) {
                throw "Temporary exact-ref exclusion failed and was not adopted: $temporaryPutFailure"
            }
            throw "GitHub did not persist the exact-ref temporary exclusion."
        }

        $deleteOutput = (& git push "--force-with-lease=refs/heads/${LaneRef}:$laneSha" origin ":refs/heads/$LaneRef" 2>&1) -join [Environment]::NewLine
        if ($LASTEXITCODE -ne 0) {
            try {
                Assert-RemoteBranchMissing -BranchName $LaneRef
            } catch {
                throw "Atomic exact-SHA lane deletion failed and the branch remains. Output: $deleteOutput"
            }
        }
        Assert-RemoteBranchMissing -BranchName $LaneRef
    } catch {
        $operationFailure = $_
    }

    try {
        $beforeRestore = Get-StableRuleset -Name $desired.name
        if ($null -eq $beforeRestore) {
            throw "Ruleset disappeared before canonical restore."
        }
        $beforeRestoreCanonical = Get-CanonicalRulesetJson -Ruleset $beforeRestore
        if ($beforeRestoreCanonical -eq $temporaryCanonical) {
            $restorePutFailure = $null
            try {
                Invoke-GhJson -Arguments @(
                    "api", "repos/$Repository/rulesets/$($existing.id)", "--method", "PUT", "--input", $desiredFullPath
                ) | Out-Null
            } catch {
                $restorePutFailure = $_
            }
            $restored = Get-StableRuleset -Name $desired.name
            if ($null -eq $restored -or (Get-CanonicalRulesetJson -Ruleset $restored) -ne $desiredCanonical) {
                if ($null -ne $restorePutFailure) {
                    throw "Canonical restore failed and was not adopted: $restorePutFailure"
                }
                throw "GitHub did not restore the canonical ruleset after lane deletion."
            }
        } elseif ($beforeRestoreCanonical -eq $desiredCanonical) {
            $restored = $beforeRestore
        } else {
            throw "Ruleset state changed outside DeleteLane before canonical restore."
        }
        Assert-EffectiveRules -BranchNames @("hotfix/v9.9.9", "release/v9.9.9")
    } catch {
        $restoreFailure = $_
    }

    if ($null -ne $restoreFailure) {
        throw "Canonical ruleset restore failed after DeleteLane; exact-ref freeze was retained. Snapshot: $snapshotPath. Freeze: $freezeName. Cause: $restoreFailure"
    }

    try {
        $freezeForCleanup = Get-StableRuleset -Name $freezeName
        if ($null -ne $freezeForCleanup) {
            $freezeId = $freezeForCleanup.id
            $freezeDeleteFailure = $null
            try {
                Invoke-GhJson -Arguments @(
                    "api", "repos/$Repository/rulesets/$freezeId", "--method", "DELETE"
                ) | Out-Null
            } catch {
                $freezeDeleteFailure = $_
            }
            $remainingFreeze = Get-StableRuleset -Name $freezeName
            if ($null -ne $remainingFreeze) {
                if ($null -ne $freezeDeleteFailure) {
                    throw "Delete freeze removal failed and was not adopted: $freezeDeleteFailure"
                }
                throw "GitHub retained the delete freeze ruleset."
            }
        }
    } catch {
        $freezeCleanupFailure = $_
    }

    if ($null -ne $freezeCleanupFailure) {
        throw "DeleteLane restored canonical protection but could not remove the exact-ref freeze. Snapshot: $snapshotPath. Freeze: $freezeName. Cause: $freezeCleanupFailure"
    }
    if ($null -ne $operationFailure) {
        throw "DeleteLane failed; canonical ruleset was restored and the freeze was removed. Snapshot: $snapshotPath. Cause: $operationFailure"
    }

    Write-Output "Deleted frozen integrated remote lane '$LaneRef' at $laneSha, restored canonical protection, and removed freeze '$freezeName'. Snapshot: $snapshotPath"
    exit 0
}

$allBefore = Get-RepositoryRulesets
$allBeforePath = Save-JsonSnapshot -Value $allBefore -Label "all-rulesets-before-apply"
$createdId = $null
$previousPath = $null
try {
    $preMutation = Get-StableRuleset -Name $desired.name
    $preMutationCanonical = if ($null -eq $preMutation) { $null } else { Get-CanonicalRulesetJson -Ruleset $preMutation }
    if ($preMutationCanonical -ne $existingCanonical) {
        throw "Ruleset state changed after planning. Refusing stale apply."
    }
    if ($null -eq $existing) {
        try {
            $applied = Invoke-GhJson -Arguments @(
                "api", "repos/$Repository/rulesets", "--method", "POST", "--input", $desiredFullPath
            )
        } catch {
            $adopted = Get-StableRuleset -Name $desired.name
            if ($null -eq $adopted -or (Get-CanonicalRulesetJson -Ruleset $adopted) -ne $desiredCanonical) {
                throw
            }
            $applied = $adopted
        }
        $createdId = $applied.id
    } elseif ($desiredCanonical -ne $existingCanonical) {
        $previousPath = Save-JsonSnapshot -Value $existing -Label "previous-ruleset"
        try {
            Invoke-GhJson -Arguments @(
                "api", "repos/$Repository/rulesets/$($existing.id)", "--method", "PUT", "--input", $desiredFullPath
            ) | Out-Null
        } catch {
            $adopted = Get-StableRuleset -Name $desired.name
            if ($null -eq $adopted -or (Get-CanonicalRulesetJson -Ruleset $adopted) -ne $desiredCanonical) {
                throw
            }
        }
    }

    $appliedRuleset = Get-StableRuleset -Name $desired.name
    if ($null -eq $appliedRuleset -or (Get-CanonicalRulesetJson -Ruleset $appliedRuleset) -ne $desiredCanonical) {
        throw "GitHub did not persist the canonical desired ruleset."
    }
    Assert-EffectiveRules -BranchNames @("hotfix/v9.9.9", "release/v9.9.9")
    Write-Output "Applied and verified '$($desired.name)'. Pre-apply snapshot: $allBeforePath"
} catch {
    $applyFailure = $_
    if ($null -ne $createdId) {
        & gh api "repos/$Repository/rulesets/$createdId" -H "X-GitHub-Api-Version: 2026-03-10" --method DELETE 2>&1 | Out-Null
    } elseif ($null -ne $previousPath) {
        $previous = Get-Content -LiteralPath $previousPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $restoreRequest = $previous | Select-Object name, target, enforcement, bypass_actors, conditions, rules
        $rollbackPath = Save-JsonSnapshot -Value $restoreRequest -Label "automatic-rollback"
        & gh api "repos/$Repository/rulesets/$($existing.id)" -H "X-GitHub-Api-Version: 2026-03-10" --method PUT --input $rollbackPath 2>&1 | Out-Null
    }
    throw "Ruleset apply failed and rollback was attempted. Snapshot: $allBeforePath. Cause: $applyFailure"
}
