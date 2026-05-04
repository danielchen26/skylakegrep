# skylakecode

Terminology: this parent-folder shared-context model is called Nexus.
Multi-repository workspace. All code lives in the child repositories listed below.
Do not initialize git or create source files in this root directory.

## Repositories
- `./repo-B/`
- `./repo-C/`
- `./<repo-C>/`
- `./repo-A/`

## Working Guidelines
- Perform all git operations within individual repository directories
- This branch: main
- Changes should be coordinated across repositories when they share interfaces
- Read `FEATURE.md` before feature implementation or planning
- Update `FEATURE.md` when relevant constraints, decisions, or progress are discovered
