# MoneyMaker Terminal

Cross-platform trading terminal with AI agents (crypto, stocks, Polymarket).

## Architecture

```
Tauri v2 (Rust)  ←→  Python Sidecar (FastAPI + DuckDB)
   ↓                       ↓
React UI (WebSocket)    Tick feed, validation, logs
```

## Quick Start

### 1. Build Python Sidecar

```bash
chmod +x build_sidecar.sh
./build_sidecar.sh
```

This creates `src-tauri/binaries/moneymaker-sidecar-{platform}` via PyInstaller.

### 2. Install Frontend Dependencies

```bash
cd frontend && npm install
```

### 3. Run in Development

```bash
cd src-tauri && cargo tauri dev
```

### 4. Build Native Installer

```bash
cd src-tauri && cargo tauri build
```

## Project Structure

```
MoneyMaker/
├── frontend/         # React + Vite + Tailwind
├── src-tauri/        # Rust Tauri shell + sidecar binary
├── backend/          # Python FastAPI + DuckDB
├── build_sidecar.sh  # PyInstaller build script
└── README.md
```

## Tech Stack

- **UI**: Tauri v2, React 18, Vite, Tailwind CSS
- **Backend**: Python 3.11+, FastAPI, uvicorn, WebSocket
- **Database**: DuckDB (embedded, columnar)
- **Charts**: TradingView Lightweight Charts (planned)
- **AI**: OpenBB, CCXT, Polymarket APIs (planned)
