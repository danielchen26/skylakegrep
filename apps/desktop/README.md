# Skygrep Desktop

Skygrep Desktop is the macOS-first desktop agent surface for skylakegrep. It is
intentionally split into a Rust core, an agent event runtime, and a React/Tauri
overlay UI so the app can grow from one production overlay into multi-window and
native 3D layers without replacing the product architecture.

## Architecture

- `crates/skygrep-core`: Rust search/index runtime. The first port targets the
  current Python SQLite schema and implements lexical filtering, cosine
  retrieval, symbol boost, graph tiebreak, and cascade telemetry.
- `crates/skygrep-protocol`: shared serializable app contract for workflow
  events, search results, suggestions, output previews, and quality states.
- `crates/skygrep-agent`: orchestrates a user intent into workflow events that
  the desktop UI can stream.
- `apps/desktop/src-tauri`: Tauri v2 shell with transparent frameless window,
  always-on-top behavior, tray integration, and global shortcut registration.
- `apps/desktop/src`: React + TypeScript modular overlay UI. Focus and CSS 3D
  modes use the same data model and components.

## Development

```bash
npm install
npm run dev
npm run build
npm test
npm run tauri -- build --debug
```

The debug macOS app bundle is written to:

```text
target/debug/bundle/macos/Skygrep Desktop.app
```

## Design Notes

The default layout keeps Live Search, Output Preview, and the Skygrep Agent
phase bar visible by default. Other panels are modular and persisted in
`localStorage`. The `3D` mode is deliberately CSS-based in this version; a
future Three.js layer should render optional spatial effects only, while the
actual workflow panels remain accessible DOM components.

