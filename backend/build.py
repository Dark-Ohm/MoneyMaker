"""Build script: packages Python sidecar into a standalone binary via PyInstaller.

Usage:
    python build.py [--output-dir DIR]

Output:
    - Linux:   moneymaker-sidecar-x86_64-unknown-linux-gnu
    - Windows: moneymaker-sidecar-x86_64-pc-windows-msvc.exe

The binary is placed in ../src-tauri/binaries/ by default.
"""
from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def get_target_suffix() -> str:
    system = platform.system().lower()
    arch = platform.machine().lower()
    if system == "linux":
        return f"{arch}-unknown-linux-gnu"
    elif system == "windows":
        return f"{arch}-pc-windows-msvc.exe"
    elif system == "darwin":
        return f"{arch}-apple-darwin"
    else:
        raise RuntimeError(f"Unsupported platform: {system}/{arch}")


def build(output_dir: Path | None = None) -> Path:
    repo_root = Path(__file__).resolve().parent.parent
    backend_dir = Path(__file__).resolve().parent
    target_dir = output_dir or (repo_root / "src-tauri" / "binaries")
    target_dir.mkdir(parents=True, exist_ok=True)

    suffix = get_target_suffix()
    binary_name = f"moneymaker-sidecar-{suffix}"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "moneymaker-sidecar",
        "--distpath",
        str(target_dir),
        "--workpath",
        str(backend_dir / "build"),
        "--specpath",
        str(backend_dir / "build"),
        "--noconfirm",
        "--clean",
        "--hidden-import",
        "duckdb",
        "--hidden-import",
        "fastapi",
        "--hidden-import",
        "uvicorn",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols",
        "--hidden-import",
        "uvicorn.protocols.http",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan",
        "--hidden-import",
        "uvicorn.lifespan.on",
        "--hidden-import",
        "cryptography",
        "--hidden-import",
        "cryptography.hazmat.primitives.ciphers.aead",
        "--hidden-import",
        "cryptography.hazmat.primitives.kdf.pbkdf2",
        "--hidden-import",
        "cryptography.hazmat.primitives.hashes",
        "--hidden-import",
        "eth_account",
        "--hidden-import",
        "eth_account.account",
        "--hidden-import",
        "eth_account.messages",
        "--collect-all",
        "cryptography",
        str(backend_dir / "src" / "main.py"),
    ]

    print(f"Building sidecar for {suffix}...")
    subprocess.run(cmd, check=True, cwd=str(backend_dir))

    # Rename the output binary to include the platform suffix
    built = target_dir / "moneymaker-sidecar"
    final = target_dir / binary_name

    if built.exists():
        if final.exists():
            final.unlink()
        built.rename(final)
        print(f"Built: {final}")
        return final
    else:
        raise FileNotFoundError(f"Expected binary not found at {built}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build MoneyMaker Python sidecar")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    build(args.output_dir)
