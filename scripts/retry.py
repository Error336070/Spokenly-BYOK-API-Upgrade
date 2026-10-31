"""Resend a failed clip on its saved route. No automatic route fallback.

  retry.py              retry the newest pending clip in logging.failed_clips_dir
  retry.py CLIP         retry that clip
  retry.py --list       show pending clips (recovered ones are counted, not listed)

A nonempty transcript marks the clip recovered in its .json sidecar. The audio stays until retention deletes it.
"""
import argparse
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from proxy import failed  # noqa: E402
from proxy.config import env, load_config, local_server_url  # noqa: E402


class RetryError(ValueError):
    """A readable reason the retry did not recover the clip."""


def _error_text(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return response.text[:300] or response.reason_phrase


def _record_result(path, error, generation, attempt_id):
    try:
        return failed.record_retry(path, error, started_generation=generation, attempt_id=attempt_id)
    except (OSError, ValueError) as exc:
        # A metadata failure must not discard a successfully recovered transcript.
        print(f"warning: could not update recovery state for {path.name}: {exc}", file=sys.stderr)
        return None


def retry_clip(path, cfg, client):
    """Send the clip on its saved route and return the text. Updates the sidecar's state."""
    metadata = failed.read_sidecar(path)
    if metadata is None:
        raise RetryError(f"{path.name} has no readable .json metadata, so its route is unknown")
    if metadata["route"] not in cfg["routes"]:
        raise RetryError(f"saved route '{metadata['route']}' no longer exists in the config; not retrying on another route")
    try:
        audio = path.read_bytes()
    except OSError as e:
        raise RetryError(f"cannot read {path.name}: {e.strerror or e}") from e
    generation = metadata.get("generation", 0)
    attempt_id = uuid.uuid4().hex
    try:
        response = client.post("/v1/audio/transcriptions",
                               data={"model": metadata["route"], "language": metadata.get("language") or "",
                                     "_dictation_proxy_retry": attempt_id},
                               files={"file": (path.name, audio)})
    except httpx.HTTPError as e:
        _record_result(path, type(e).__name__, generation, attempt_id)
        raise RetryError(f"could not reach the proxy ({type(e).__name__}); is it running? "
                         f"Check with scripts\\proxyctl.ps1 status") from e
    if response.status_code != 200:
        _record_result(path, f"HTTP {response.status_code}", generation, attempt_id)
        raise RetryError(f"route '{metadata['route']}' failed again: HTTP {response.status_code}: "
                         f"{_error_text(response)}")
    try:
        text = response.json()["text"]
        if not isinstance(text, str):
            raise ValueError("transcript is not text")
    except (ValueError, KeyError, TypeError) as e:
        _record_result(path, "unreadable response", generation, attempt_id)
        raise RetryError("the proxy returned an unreadable response") from e
    if not text.strip():
        _record_result(path, "empty transcript", generation, attempt_id)
        raise RetryError(f"route '{metadata['route']}' returned an empty transcript again; the clip stays pending")
    recorded = _record_result(path, None, generation, attempt_id)
    if recorded is not None and recorded.get("status") != "recovered":
        print(f"warning: a newer failure of {path.name} arrived during this retry, so it stays pending",
              file=sys.stderr)
    return text


def pending(directory):
    out = []
    for p in failed.clips(directory):
        metadata = failed.read_sidecar(p)
        if metadata is None or metadata.get("status") != "recovered":
            out.append((p, metadata))
    return out


def list_pending(directory):
    entries = pending(directory)
    recovered = len(failed.clips(directory)) - len(entries)
    for p, metadata in entries:
        saved = datetime.fromtimestamp(p.stat().st_mtime).isoformat(sep=" ", timespec="minutes")
        if metadata is None:
            print(f"  {p.name}  {saved}  metadata missing or unreadable")
        else:
            print(f"  {p.name}  {saved}  route {metadata['route']}  failures {metadata.get('failures', 1)}  "
                  f"retries {metadata.get('retries', 0)}"
                  + (f"  last retry: {metadata['last_retry_error']}" if metadata.get("last_retry_error") else ""))
    print(f"{len(entries)} pending, {recovered} recovered in {directory}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clip", nargs="?", type=Path)
    parser.add_argument("--copy", action="store_true", help="copy the text to the Windows clipboard")
    parser.add_argument("--list", action="store_true", help="list pending failed clips")
    args = parser.parse_args(argv)
    cfg = load_config()
    directory = Path(cfg["logging"]["failed_clips_dir"])
    if args.list:
        list_pending(directory)
        return 0
    path = args.clip or next((p for p, metadata in pending(directory) if metadata is not None), None)
    if path is None:
        print(f"no pending failed clips with readable metadata in {directory} (see --list)", file=sys.stderr)
        return 1
    try:
        with httpx.Client(base_url=local_server_url(cfg["server"]["host"], cfg["server"]["port"]), timeout=None,
                          headers={"Authorization": f"Bearer {env('PROXY_API_KEY')}"}) as client:
            text = retry_clip(path, cfg, client)
    except RetryError as e:
        print(f"retry failed for {path.name}: {e}", file=sys.stderr)
        return 1
    print(text)
    if args.copy and sys.platform == "win32":
        subprocess.run(["clip.exe"], input=text.encode("utf-16le"), check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
