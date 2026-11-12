"""Generate local test-host files and optional .reg instructions; never edit registry."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys

from host import HOST_NAME, prototype_dir

BROWSERS = {"chrome": r"Software\Google\Chrome", "edge": r"Software\Microsoft\Edge"}


def prepare(root: Path, python: Path, extension_ids: dict[str, str], *, inbox: str = "prototype") -> list[Path]:
    if inbox not in ("prototype", "production"):
        raise ValueError("Choose prototype or production inbox.")
    if not extension_ids or set(extension_ids) - BROWSERS.keys():
        raise ValueError("Specify Chrome and/or Edge extension IDs.")
    if not all(isinstance(value, str) and re.fullmatch(r"[a-p]{32}", value) for value in extension_ids.values()):
        raise ValueError("Each extension ID must contain exactly 32 lowercase letters a-p.")
    python = python.resolve(strict=True)
    root = root.resolve()
    if python.name.lower() not in {"python.exe", "python3.exe"}:
        raise ValueError("Use python.exe, not pythonw.exe: Native Messaging requires stdin/stdout.")
    if any(char in str(root) + str(python) for char in '\r\n"'):
        raise ValueError("Unsupported characters in local installation paths.")
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("host.py"), root / "host.py")
    shutil.copyfile(Path(__file__).resolve().parents[2] / "indexer" / "capture_format.py", root / "capture_format.py")
    (root / "destination.json").write_text(json.dumps({"inbox": inbox}), encoding="utf-8")
    # UTF-8 batch after an ASCII codepage switch; quote paths and escape literal %.
    launcher = '@echo off\r\n@chcp 65001 >nul 2>nul\r\n"' + str(python).replace("%", "%%") + '" -I -u "%~dp0host.py" %*\r\n'
    (root / "launch_host.cmd").write_bytes(launcher.encode("utf-8"))
    manifest = {
        "name": HOST_NAME,
        "description": "Isolated Planned Native Messaging transport test",
        "path": str(root / "launch_host.cmd"),
        "type": "stdio",
        "allowed_origins": sorted({f"chrome-extension://{value}/" for value in extension_ids.values()}),
    }
    manifest_path = root / "host.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    generated = [root / "host.py", root / "capture_format.py", root / "destination.json", root / "launch_host.cmd", manifest_path]
    escaped_path = str(manifest_path).replace("\\", "\\\\").replace('"', '\\"')
    for browser in extension_ids:
        key = "HKEY_CURRENT_USER\\" + BROWSERS[browser] + "\\NativeMessagingHosts\\" + HOST_NAME
        register = root / f"register-{browser}.reg"
        unregister = root / f"unregister-{browser}.reg"
        register.write_text('Windows Registry Editor Version 5.00\n\n[' + key + ']\n@="' + escaped_path + '"\n', encoding="utf-16")
        unregister.write_text('Windows Registry Editor Version 5.00\n\n[-' + key + ']\n', encoding="utf-16")
        generated.extend([register, unregister])
    return generated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome-id", help="ID shown on chrome://extensions")
    parser.add_argument("--edge-id", help="ID shown on edge://extensions")
    parser.add_argument("--inbox", choices=("prototype", "production"), default="prototype",
                        help="Explicit destination; defaults to the isolated Stage 3A inbox")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This prototype registration procedure targets Windows only.")
    ids = {key: value for key, value in {"chrome": args.chrome_id, "edge": args.edge_id}.items() if value}
    try:
        paths = prepare(prototype_dir(), Path(sys.executable), ids, inbox=args.inbox)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print("Generated native host files; NO registry keys were changed.")
    print("Capture destination:", args.inbox)
    print("Python:", sys.executable)
    for path in paths:
        print(path)
    print("Review the register-*.reg files and import only after explicit approval.")


if __name__ == "__main__":
    main()
