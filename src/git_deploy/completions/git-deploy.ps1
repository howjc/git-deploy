# PowerShell 5.1/7 completion for git-deploy. All discovery is local and read-only.
$gitDeployCompleter = {
    param($wordToComplete, $commandAst, $cursorPosition)

    $flags = @('--dry-run', '--remote-plan', '--recover', '--skip-build', '--full',
        '--yes', '--config', '--workspace', '--verbose', '--create-root',
        '--no-create-root', '--force', '--probe-ftp-hybrid', '--version', '--help')
    $actions = @('build', 'doctor', 'init', 'bootstrap', 'completion')
    $shells = @('bash', 'zsh', 'powershell', 'pwsh')
    $positionals = @()
    $selectors = @()
    $selector = $null
    foreach ($element in @($commandAst.CommandElements | Select-Object -Skip 1)) {
        if ($element.Extent.EndOffset -ge $cursorPosition) { break }
        # Never evaluate expressions typed at the prompt during completion.
        if ($element -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) {
            return
        }
        $value = $element.Value
        if ($selector) {
            $selectors += @($selector, $value)
            $selector = $null
        } elseif ($value -in @('--config', '--workspace')) {
            $selector = $value
        } elseif ($value.StartsWith('--config=') -or $value.StartsWith('--workspace=')) {
            $selectors += $value
        } elseif (-not $value.StartsWith('-')) {
            $positionals += $value
        }
    }
    if ($selector) { return } # Let PowerShell complete filenames.
    if ($wordToComplete.StartsWith('-')) {
        $candidates = $flags
    } elseif ($positionals.Count -gt 0 -and $positionals[0] -eq 'completion') {
        if ($positionals.Count -eq 1) {
            $candidates = $shells + @('targets', 'install')
        } elseif ($positionals.Count -eq 2 -and $positionals[1] -eq 'install') {
            $candidates = $shells
        } else { return }
    } else {
        $candidates = @(& git-deploy @selectors completion targets 2>$null)
        if ($positionals.Count -eq 0) { $candidates = $actions + $candidates }
    }
    foreach ($candidate in $candidates) {
        # Only validated literal CLI words may be inserted into the command line.
        if ($candidate -match '^[A-Za-z0-9-][A-Za-z0-9._-]*$' -and
            $candidate.StartsWith($wordToComplete, [System.StringComparison]::OrdinalIgnoreCase)) {
            [System.Management.Automation.CompletionResult]::new(
                $candidate, $candidate, 'ParameterValue', $candidate)
        }
    }
}
$gitDeployRegistration = @{
    CommandName = @('git-deploy', 'git-deploy.exe')
    ScriptBlock = $gitDeployCompleter
}
if ((Get-Command Register-ArgumentCompleter).Parameters.ContainsKey('Native')) {
    $gitDeployRegistration.Native = $true
}
Register-ArgumentCompleter @gitDeployRegistration
Remove-Variable gitDeployCompleter, gitDeployRegistration
