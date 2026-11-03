# Delete the frontend modules nothing imports any more.
#
# Every screen now reads the live API, so the scripted world that stood in for
# it before the backend existed is dead code: the mock client and its fixtures,
# the scenario stepper that drove them, the guided tour that narrated them, the
# two portals that CitizenApp and FieldApp replaced, and two files already
# reduced to re-export stubs.
#
# Kept deliberately: `api/httpClient.ts` (the real client), `components/map/
# LiveMap.tsx` (MapView's replacement), and everything under `components/ui`.
#
# Run from the repo root:   powershell -ExecutionPolicy Bypass -File frontend\indradhanu\scripts\remove-dead-frontend.ps1
# Then run `npm run build`, which is `tsc -b && vite build`.
#
# WHY THIS SCRIPT CHECKS ITSELF
#
# `tsconfig.app.json` has `"include": ["src"]`, so `tsc -b` type-checks every
# file under src whether or not anything reaches it. "Nothing renders it" is
# therefore not the same as "it is safe to delete": a file no screen imports can
# still be importing a file on this list, and deleting that one breaks the
# build from a direction nobody was looking at.
#
# That is exactly what happened to `src/api/types.ts`. It was dead when this
# list was written, and then `lib/tokens.ts` and `components/common/
# indicators.tsx` started importing it. Both are themselves unreachable from
# `App.tsx`, so the three of them form a closed orphan island: correct to
# delete, but only all together. The reference scan below is what turns that
# from something you have to remember into something the script refuses to get
# wrong.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot   # .../frontend/indradhanu
$src  = Join-Path $root 'src'

$targets = @(
  'src/api/client.ts',
  'src/api/types.ts',
  'src/api/mock',
  'src/hooks/useApi.ts',
  'src/scenario',
  'src/components/tour',
  'src/components/map/MapView.tsx',
  'src/routes/citizen/CitizenPortal.tsx',
  'src/routes/field/FieldPortal.tsx',
  'src/routes/admin/LiveOps.tsx',
  'src/routes/demo/MapCanvas.tsx',
  # The orphan island described above. `indicators.tsx` is imported by nothing;
  # it imports `lib/tokens.ts`, which is imported by nothing else; both import
  # `api/types.ts`. Remove the island or keep it, never half of it.
  'src/components/common/indicators.tsx',
  'src/lib/tokens.ts'
)

# Absolute paths of everything being deleted, so a reference *between* two
# targets does not count as a reason to keep either.
$doomed = $targets | ForEach-Object { (Join-Path $root $_) }

function Get-Specifier([string]$target) {
  # 'src/api/types.ts' -> '@/api/types';  'src/api/mock' -> '@/api/mock'
  $s = $target -replace '^src/', '@/'
  return ($s -replace '\.(ts|tsx)$', '')
}

$survivors = Get-ChildItem -Path $src -Recurse -File -Include *.ts,*.tsx |
  Where-Object { $f = $_.FullName; -not ($doomed | Where-Object { $f -eq $_ -or $f.StartsWith($_ + [IO.Path]::DirectorySeparatorChar) }) }

$blocked = @()
foreach ($t in $targets) {
  $spec = Get-Specifier $t
  # Match an import, not a mention. `StatusStrip.tsx` carries a comment saying
  # `@/hooks/useApi` was removed in an earlier cleanup — a plain substring scan
  # reads that as a live reference and refuses to delete anything at all.
  # Anchored to `from "..."` or `import("...")`, with an optional subpath so a
  # deleted directory is still caught by an import reaching inside it.
  $pattern = '(?:from|import\s*\()\s*["'']' + [regex]::Escape($spec) + '(?:/[^"'']*)?["'']'
  $hits = $survivors | Select-String -Pattern $pattern
  if ($hits) {
    $blocked += $t
    Write-Host "REFUSED  $t -- still imported as '$spec' by:" -ForegroundColor Yellow
    $hits | ForEach-Object {
      Write-Host ("           " + $_.Path.Substring($root.Length + 1) + ":" + $_.LineNumber)
    }
  }
}

if ($blocked.Count -gt 0) {
  Write-Host ''
  Write-Host "Nothing deleted. $($blocked.Count) target(s) are still referenced." -ForegroundColor Red
  Write-Host 'Either remove those imports, or drop the target from $targets in this script.'
  exit 1
}

foreach ($t in $targets) {
  $path = Join-Path $root $t
  if (Test-Path $path) {
    Remove-Item -Recurse -Force $path
    Write-Host "removed  $t"
  } else {
    Write-Host "missing  $t (already gone)"
  }
}

Write-Host ''
Write-Host 'Now run: npm run build'
