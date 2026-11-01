---
name: dependency-updater
description: Update libraries/packages in a repo's manifest files (package.json, Gemfile, requirements.txt, etc.). Checks changelogs for breaking changes and resolves them in the codebase. WHEN "update dependencies", "bump packages", "upgrade gems", "update npm packages", "check for outdated dependencies".
---

# Dependency Updater

## 1. Gather scope
Ask the user:
- **Which manifest file(s)?** A repo may have several (package.json, Gemfile, requirements.txt, go.mod...). List the ones you find and let them choose.
- **All packages, or specific ones?**
- **How far to update?**
  - Patch — bug fixes only (3.0.1 → 3.0.9)
  - Minor — backward-compatible features (3.0.1 → 3.2.0)
  - Major — potentially breaking (3.0.1 → 4.1.0)
  - Latest — newest available, breaking changes included

## 2. Propose a plan
List each package with its current → target version. Get confirmation before making changes. For major/latest, flag the higher risk.

## 3. Research breaking changes
For any minor, major, or latest bump, read the package's CHANGELOG / release notes / migration guide between the current and target version. Record any breaking changes for later — don't fix them yet.

## 4. Apply updates
Use the package manager's tooling (bundle update, npm install pkg@version, yarn upgrade...) so lockfiles and transitive dependencies resolve. Do NOT hand-edit version strings without regenerating the lockfile.
- Batch patch/minor updates.
- Do major/latest one package (or small group) at a time.
- If an update forces other packages to change, handle that too.

## 5. Verify
Run the install command and the build/test suite. If anything fails to resolve or compile, fix it before continuing.

## 6. Resolve breaking changes
Go through the recorded breaking changes with the user one by one, and update the codebase to address each. Re-run tests after.
