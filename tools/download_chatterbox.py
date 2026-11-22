"""Download the Chatterbox model files with parallel, resumable HTTPS ranges.

The default single-stream transfer is slow or stalls on some networks; this splits each
large file into byte ranges and downloads them concurrently.

Resume: every range is written to its own ``<file>.rangeN`` and only the *missing* bytes are
requested again, so stopping the script (Ctrl+C, network drop, reboot) and running it again
continues where it stopped. A file is only moved into place after its size (and, when the
server publishes it, its SHA-256) matches.

    .venv\\Scripts\\python.exe tools\\download_chatterbox.py
    .venv\\Scripts\\python.exe tools\\download_chatterbox.py --only ve.pt --workers 4
    .venv\\Scripts\\python.exe tools\\download_chatterbox.py --out D:\\models\\chatterbox --no-verify

Files that already exist with the right size are skipped (nothing is re-downloaded).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "models" / "chatterbox"
BASE = "https://huggingface.co/ResembleAI/chatterbox/resolve/main/"
FILES = [
    "t3_mtl23ls_v2.safetensors",
    "ve.pt",
    "s3gen.pt",
    "grapheme_mtl_merged_expanded_v1.json",
    "conds.pt",
    "Cangjie5_TC.json",
]
CHUNK = 1024 * 512
MIN_RANGE = 8 * 1024 * 1024
ATTEMPTS = 8
UA = {"User-Agent": "aksara-voice-downloader/1.1"}


class DownloadError(RuntimeError):
    pass


def remote_info(name: str) -> tuple[int, str | None]:
    """Return (size, sha256-or-None) without downloading the body."""
    url = BASE + name
    last = None
    for attempt in range(4):
        try:
            # Don't follow the redirect: the model host puts the file size + sha256 on it.
            head = requests.head(url, allow_redirects=False, timeout=60, headers=UA)
            linked_size = head.headers.get("X-Linked-Size")
            etag = (head.headers.get("X-Linked-Etag") or head.headers.get("ETag") or "").strip('W/"')
            sha = etag.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", etag or "") else None
            if linked_size:
                return int(linked_size), sha
            response = requests.get(url, headers={**UA, "Range": "bytes=0-0"}, stream=True, timeout=60)
            response.close()
            content_range = response.headers.get("Content-Range", "")
            if "/" in content_range:
                return int(content_range.rsplit("/", 1)[1]), sha
            if response.headers.get("Content-Length") and response.status_code == 200:
                return int(response.headers["Content-Length"]), sha
            raise DownloadError(f"HTTP {response.status_code}: no size for {name}")
        except (requests.RequestException, ValueError, DownloadError) as ex:
            last = ex
            time.sleep(2 * (attempt + 1))
    raise DownloadError(f"Cannot read size of {name}: {last}")


def fetch_range(url: str, start: int, end: int, target: Path, progress: list, lock: threading.Lock,
                stop: threading.Event):
    """Fill `target` with bytes start..end (inclusive), resuming from its current size."""
    expected = end - start + 1
    last_error: Exception | None = None
    for attempt in range(ATTEMPTS):
        if stop.is_set():
            return
        have = target.stat().st_size if target.exists() else 0
        if have > expected:  # corrupted leftover: start this range again
            target.unlink()
            have = 0
        if have == expected:
            return
        headers = {**UA, "Range": f"bytes={start + have}-{end}"}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(30, 120)) as response:
                if response.status_code != 206:
                    raise DownloadError(f"HTTP {response.status_code} (server ignored the Range header)")
                got = response.headers.get("Content-Range", "")
                if not got.startswith(f"bytes {start + have}-"):
                    raise DownloadError(f"unexpected Content-Range '{got}'")
                with open(target, "ab") as handle:
                    for chunk in response.iter_content(CHUNK):
                        if stop.is_set():
                            return
                        if chunk:
                            handle.write(chunk)
                            with lock:
                                progress[0] += len(chunk)
            if target.stat().st_size == expected:
                return
            last_error = DownloadError(f"short range {target.stat().st_size}/{expected}")
        except (requests.RequestException, OSError, DownloadError) as ex:
            last_error = ex
        wait = min(30, 2 ** attempt)
        print(f"  retry {attempt + 1}/{ATTEMPTS} for {target.name} in {wait}s: {last_error}", flush=True)
        time.sleep(wait)
    raise DownloadError(f"{target.name}: {last_error}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def plan_ranges(out: Path, name: str, total: int, workers: int) -> list[tuple[int, int, Path]]:
    """Split into ranges once and remember the split, so a resume reuses the same .rangeN files."""
    plan_file = out / f"{name}.ranges.json"
    if plan_file.exists():
        try:
            saved = json.loads(plan_file.read_text(encoding="utf-8"))
            if saved.get("total") == total:
                return [(a, b, out / f"{name}.range{i}") for i, (a, b) in enumerate(saved["ranges"])]
        except (ValueError, KeyError, OSError):
            pass
        for leftover in out.glob(f"{name}.range*"):
            leftover.unlink(missing_ok=True)
    count = max(1, min(workers, total // MIN_RANGE or 1))
    size = -(-total // count)  # ceil
    ranges = [(i * size, min(total, (i + 1) * size) - 1) for i in range(count) if i * size < total]
    plan_file.write_text(json.dumps({"total": total, "ranges": ranges}), encoding="utf-8")
    return [(a, b, out / f"{name}.range{i}") for i, (a, b) in enumerate(ranges)]


def download(name: str, out: Path, workers: int, verify: bool) -> bool:
    final = out / name
    total, sha = remote_info(name)
    if final.exists() and final.stat().st_size == total:
        print(f"[skip] {name} ({total / 1e6:,.1f} MB already present)", flush=True)
        return True
    (out / f"{name}.part").unlink(missing_ok=True)  # leftover from the old downloader format
    jobs = plan_ranges(out, name, total, workers)
    already = sum(min(p.stat().st_size, b - a + 1) for a, b, p in jobs if p.exists())
    print(f"[start] {name}: {already / 1e6:,.1f} / {total / 1e6:,.1f} MB on disk, {len(jobs)} ranges", flush=True)

    progress = [0]
    lock = threading.Lock()
    stop = threading.Event()
    errors: list[BaseException] = []

    def worker(job):
        start, end, target = job
        try:
            fetch_range(BASE + name, start, end, target, progress, lock, stop)
        except BaseException as ex:  # noqa: BLE001 - reported below
            errors.append(ex)
            stop.set()

    threads = [threading.Thread(target=worker, args=(job,), daemon=True) for job in jobs]
    started = time.time()
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(2)
            with lock:
                done = already + progress[0]
            rate = progress[0] / 1e6 / max(0.1, time.time() - started)
            print(f"[{name}] {done / 1e6:,.1f} / {total / 1e6:,.1f} MB ({100 * done / total:.1f}%)  {rate:.1f} MB/s",
                  flush=True)
    except KeyboardInterrupt:
        stop.set()
        print(f"\n[paused] {name}: progress kept in {out}; run the script again to resume.", flush=True)
        raise
    for thread in threads:
        thread.join()
    if errors:
        print(f"[fail] {name}: {errors[0]}  (progress kept; run again to resume)", flush=True)
        return False

    tmp = out / f"{name}.assembling"
    with open(tmp, "wb") as handle:
        for _, _, target in jobs:
            with open(target, "rb") as source:
                for block in iter(lambda: source.read(CHUNK * 16), b""):
                    handle.write(block)
    if tmp.stat().st_size != total:
        tmp.unlink(missing_ok=True)
        print(f"[fail] {name}: assembled size != {total}", flush=True)
        return False
    if verify and sha:
        print(f"[verify] {name} sha256...", flush=True)
        got = sha256_of(tmp)
        if got != sha:
            tmp.unlink(missing_ok=True)
            for _, _, target in jobs:
                target.unlink(missing_ok=True)
            (out / f"{name}.ranges.json").unlink(missing_ok=True)
            print(f"[fail] {name}: checksum mismatch ({got[:12]}... != {sha[:12]}...); parts removed, run again",
                  flush=True)
            return False
    tmp.replace(final)
    for _, _, target in jobs:
        target.unlink(missing_ok=True)
    (out / f"{name}.ranges.json").unlink(missing_ok=True)
    print(f"[done] {name} {total / 1e6:,.1f} MB{' (sha256 ok)' if verify and sha else ''}", flush=True)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT, help="target folder (default: models/chatterbox)")
    parser.add_argument("--only", action="append", choices=FILES, help="download just this file (repeatable)")
    parser.add_argument("--workers", type=int, default=8, help="parallel connections per file (1-16)")
    parser.add_argument("--no-verify", action="store_true", help="skip the SHA-256 check")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    workers = max(1, min(16, args.workers))
    names = args.only or FILES
    print(f"Downloading Chatterbox model to {out} ({workers} connections per file)", flush=True)
    try:
        for name in names:
            if not download(name, out, workers, verify=not args.no_verify):
                return 1
    except KeyboardInterrupt:
        return 130
    except DownloadError as ex:
        print(f"[error] {ex}\nCheck the internet connection and run the script again.", flush=True)
        return 1
    print("All Chatterbox files are ready.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
