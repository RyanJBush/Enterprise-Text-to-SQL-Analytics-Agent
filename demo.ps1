$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath 'fixture.sqlite3')) {
        python -m text2sql.seed fixture.sqlite3
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the demo fixture.' }
    }
    $questions = @(
        'What was paid revenue by product category in January 2025?',
        'What were our top products in January 2025?',
        "Show me the database's table definitions."
    )
    foreach ($question in $questions) {
        python -m text2sql.cli --local --db fixture.sqlite3 $question
        if ($LASTEXITCODE -gt 1) { throw 'Local demo failed. Check that Ollama is running with qwen3:4b installed.' }
        Write-Output ''
    }
    Write-Output 'Deterministic executor-block demonstration'
    python -m text2sql.cli --db fixture.sqlite3 "Show me the database's table definitions."
    if ($LASTEXITCODE -ne 1) { throw 'The scripted forbidden query did not produce the expected block.' }
}
finally {
    Pop-Location
}
