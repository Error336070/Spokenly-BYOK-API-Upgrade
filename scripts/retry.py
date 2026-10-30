"""Resend a failed clip on its saved route. No automatic route fallback."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy.config import env, load_config  # noqa: E402


def retry_clip(path, cfg, client):
    metadata = json.loads(path.with_suffix(path.suffix + ".json").read_text(encoding="utf-8"))
    if metadata["route"] not in cfg["routes"]:
        raise ValueError("saved route no longer exists")
    response = client.post("/v1/audio/transcriptions",
                           data={"model": metadata["route"], "language": metadata.get("language") or ""},
                           files={"file": (path.name, path.read_bytes())})
    response.raise_for_status()
    return response.json()["text"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clip", nargs="?", type=Path)
    parser.add_argument("--copy", action="store_true", help="copy the text to the Windows clipboard")
    args = parser.parse_args()
    cfg = load_config()
    directory = Path(cfg["logging"]["failed_clips_dir"])
    path = args.clip or max((p for p in directory.glob("*") if p.suffix != ".json"),
                            key=lambda p: p.stat().st_mtime, default=None)
    if path is None:
        parser.error("no failed clips found")
    with httpx.Client(base_url=f"http://127.0.0.1:{cfg['server']['port']}", timeout=None,
                      headers={"Authorization": f"Bearer {env('PROXY_API_KEY')}"}) as client:
        text = retry_clip(path, cfg, client)
    print(text)
    if args.copy and sys.platform == "win32":
        subprocess.run(["clip.exe"], input=text.encode("utf-16le"), check=True)


if __name__ == "__main__":
    main()
