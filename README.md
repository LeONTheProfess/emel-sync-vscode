# EMEL Sync VS Code Extension

## Workflow

First launch:

1. Open the target project folder in VS Code.
2. Run `EMEL: Initialize & Sync`.
3. Enter the WMS API URL and token.
4. The extension runs `init`, then immediately `sync`.

Later:

- `EMEL: Sync Classes`
- `EMEL: Full Sync`
- `EMEL: Dry Run`
- `EMEL: Open Configuration`

## Important

Replace `python/emel_sync.py` with your real working Python script.

## Development

```bash
npm install
npm run compile
```

Press `F5` to launch the Extension Development Host.

## Configuration

- `emel.pythonPath`
- `emel.scriptPath`
- `emel.defaultWorkers`
- `emel.showTerminal`
