#!/usr/bin/env python3
"""
Upload a local folder of photos/videos to S3 byte-for-byte (no re-encoding,
no compression), then write a manifest.json that the web gallery reads.

Usage:
    python upload.py /path/to/media
    python upload.py /path/to/media --prefix italy-2026/ --workers 4
    python upload.py --manifest-only          # rebuild manifest from what is in S3
    python upload.py /path/to/media --dry-run

Credentials/config come from .env (see .env.example).
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from tqdm import tqdm

try:
    from PIL import Image, ImageOps

    try:  # optional: lets Pillow read iPhone HEIC photos for thumbnails
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp", ".mts", ".m2ts", ".wmv"}
THUMB_PREFIX = "_thumbs/"
PREVIEW_PREFIX = "_previews/"
THUMB_SIZE = 800
PREVIEW_MAX = 1920  # longest side of the browser-playable video preview

# Extra MIME types that `mimetypes` may not know on every platform.
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/mp2t", ".mts")
mimetypes.add_type("video/mp2t", ".m2ts")

# Large chunks + parallel parts = fast uploads of multi-GB videos.
TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=64 * 1024 * 1024,
    multipart_chunksize=64 * 1024 * 1024,
    max_concurrency=8,
    use_threads=True,
)


_ffmpeg_cache: dict = {}
_preview_slots = threading.Semaphore(2)  # at most 2 ffmpeg encodes at once


def find_ffmpeg() -> str | None:
    """System ffmpeg, else the static binary shipped by the imageio-ffmpeg pip package."""
    if "exe" not in _ffmpeg_cache:
        exe = shutil.which("ffmpeg")
        if not exe:
            try:
                import imageio_ffmpeg

                exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                exe = None
        _ffmpeg_cache["exe"] = exe
    return _ffmpeg_cache["exe"]


def _ffmpeg_has_filter(name: str) -> bool:
    key = f"filter:{name}"
    if key not in _ffmpeg_cache:
        out = subprocess.run([find_ffmpeg(), "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        _ffmpeg_cache[key] = f" {name} " in out
    return _ffmpeg_cache[key]


def _is_hdr(path: Path) -> bool:
    # `ffmpeg -i` prints stream info (incl. transfer function) to stderr.
    info = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    return "arib-std-b67" in info or "smpte2084" in info


def _video_filter(path: Path, max_side: int) -> str:
    scale = (
        f"scale=w='if(gt(iw,ih),min({max_side},iw),-2)':h='if(gt(iw,ih),-2,min({max_side},ih))'"
    )
    # iPhone HDR (HLG/Dolby Vision) looks washed-out if just converted to SDR; tone-map it first.
    if _is_hdr(path) and _ffmpeg_has_filter("zscale") and _ffmpeg_has_filter("tonemap"):
        return (
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
            f"zscale=t=bt709:m=bt709:r=tv,format=yuv420p,{scale}"
        )
    return f"{scale},format=yuv420p"


def make_video_poster(path: Path, out: Path) -> bool:
    for ss in ("1", "0"):  # frame at 1s, or the first frame for very short clips
        cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", ss, "-i", str(path),
               "-frames:v", "1", "-vf", _video_filter(path, THUMB_SIZE), "-q:v", "3", str(out)]
        subprocess.run(cmd, capture_output=True)
        if out.exists() and out.stat().st_size > 0:
            return True
    return False


def make_video_preview(path: Path, out: Path) -> None:
    """H.264/AAC MP4, max 1080p, moov atom first (faststart) so it streams instantly in any browser."""
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
           "-map", "0:v:0", "-map", "0:a:0?", "-vf", _video_filter(path, PREVIEW_MAX),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-profile:v", "high",
           "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "ffmpeg failed")


def media_kind(path: Path | str) -> str | None:
    ext = os.path.splitext(str(path))[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def content_disposition(filename: str) -> str:
    """Make a plain <a href> download the file instead of opening it.

    <img>/<video> tags ignore this header, so inline display is unaffected.
    """
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "").replace("\\", "")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def public_url(bucket: str, region: str, key: str) -> str:
    encoded = "/".join(quote(part, safe="") for part in key.split("/"))
    if "." in bucket:  # dotted bucket names break TLS on virtual-hosted URLs
        return f"https://s3.{region}.amazonaws.com/{bucket}/{encoded}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{encoded}"


def load_config() -> dict:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    cfg = {
        "access_key": os.getenv("AWS_ACCESS_KEY_ID", "").strip(),
        "secret_key": os.getenv("AWS_SECRET_ACCESS_KEY", "").strip(),
        "bucket": os.getenv("S3_BUCKET", "").strip(),
        "region": os.getenv("AWS_REGION", "").strip(),
    }
    missing = [
        name
        for name, key in [
            ("AWS_ACCESS_KEY_ID", "access_key"),
            ("AWS_SECRET_ACCESS_KEY", "secret_key"),
            ("S3_BUCKET", "bucket"),
            ("AWS_REGION", "region"),
        ]
        if not cfg[key]
    ]
    if missing:
        sys.exit(f"Missing values in .env: {', '.join(missing)}")
    return cfg


def make_client(cfg: dict):
    return boto3.client(
        "s3",
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        region_name=cfg["region"],
        config=Config(retries={"max_attempts": 10, "mode": "standard"}, max_pool_connections=64),
    )


def remote_size(s3, bucket: str, key: str) -> int | None:
    try:
        return s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def make_thumbnail(path: Path) -> bytes:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((THUMB_SIZE, THUMB_SIZE))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82, optimize=True)
        return buf.getvalue()


def upload_one(s3, cfg: dict, path: Path, key: str, pbar: tqdm, lock: threading.Lock, thumbs: bool, previews: bool) -> str:
    """Returns 'uploaded' or 'skipped'. Raises on failure."""
    bucket = cfg["bucket"]
    size = path.stat().st_size

    if remote_size(s3, bucket, key) == size:
        with lock:
            pbar.update(size)
        status = "skipped"
    else:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # upload_file streams the original file as-is: no re-encoding anywhere.
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": ctype,
                "ContentDisposition": content_disposition(path.name),
                "CacheControl": "public, max-age=31536000, immutable",
            },
            Config=TRANSFER_CONFIG,
            Callback=lambda n: pbar.update(n),
        )
        status = "uploaded"

    # Small JPEG previews so the gallery grid doesn't pull 5 MB originals.
    # Originals are never modified.
    if thumbs and HAVE_PIL and media_kind(path) == "image":
        thumb_key = f"{THUMB_PREFIX}{key}.jpg"
        try:
            if remote_size(s3, bucket, thumb_key) is None:
                s3.put_object(
                    Bucket=bucket,
                    Key=thumb_key,
                    Body=make_thumbnail(path),
                    ContentType="image/jpeg",
                    CacheControl="public, max-age=31536000, immutable",
                )
        except Exception as e:  # thumbnail is best-effort
            tqdm.write(f"  (no thumbnail for {path.name}: {e})")

    # Videos: a poster frame for the grid + a browser-friendly preview for playback.
    # The original is what "Download" serves; these are only for viewing.
    if media_kind(path) == "video" and find_ffmpeg():
        immutable = "public, max-age=31536000, immutable"
        with tempfile.TemporaryDirectory() as tmp:
            try:
                poster_key = f"{THUMB_PREFIX}{key}.jpg"
                if thumbs and remote_size(s3, bucket, poster_key) is None:
                    out = Path(tmp) / "poster.jpg"
                    if make_video_poster(path, out):
                        s3.upload_file(str(out), bucket, poster_key,
                                       ExtraArgs={"ContentType": "image/jpeg", "CacheControl": immutable})
                preview_key = f"{PREVIEW_PREFIX}{key}.mp4"
                if previews and remote_size(s3, bucket, preview_key) is None:
                    out = Path(tmp) / "preview.mp4"
                    with _preview_slots:
                        make_video_preview(path, out)
                    s3.upload_file(str(out), bucket, preview_key, Config=TRANSFER_CONFIG,
                                   ExtraArgs={"ContentType": "video/mp4", "CacheControl": immutable})
            except Exception as e:  # derived assets are best-effort
                tqdm.write(f"  (no preview for {path.name}: {e})")
    return status


def list_bucket(s3, bucket: str, prefix: str) -> dict[str, dict]:
    objects: dict[str, dict] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            objects[obj["Key"]] = obj
    return objects


def build_manifest(s3, cfg: dict, prefix: str, out_path: Path) -> int:
    bucket, region = cfg["bucket"], cfg["region"]
    print("Building manifest from bucket contents…")
    media = list_bucket(s3, bucket, prefix)
    thumbs = list_bucket(s3, bucket, THUMB_PREFIX + prefix)
    previews = list_bucket(s3, bucket, PREVIEW_PREFIX + prefix)

    items = []
    for key, obj in media.items():
        kind = media_kind(key)
        if kind is None or key.endswith("/") or key.startswith((THUMB_PREFIX, PREVIEW_PREFIX)):
            continue
        rel = key[len(prefix):] if prefix and key.startswith(prefix) else key
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        thumb_key = f"{THUMB_PREFIX}{key}.jpg"
        preview_key = f"{PREVIEW_PREFIX}{key}.mp4"
        items.append(
            {
                "key": key,
                "name": key.rsplit("/", 1)[-1],
                "folder": folder,
                "type": kind,
                "size": obj["Size"],
                "url": public_url(bucket, region, key),
                "thumb": public_url(bucket, region, thumb_key) if thumb_key in thumbs else None,
                "preview": public_url(bucket, region, preview_key) if preview_key in previews else None,
            }
        )
    items.sort(key=lambda i: (i["folder"].lower(), i["name"].lower()))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"items": items}, indent=1), encoding="utf-8")
    print(f"Wrote {len(items)} items to {out_path}")
    return len(items)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> None:
    ap = argparse.ArgumentParser(description="Upload photos/videos to S3 at original quality.")
    ap.add_argument("path", nargs="?", help="Local folder (or single file) to upload")
    ap.add_argument("--prefix", default=os.getenv("S3_PREFIX", ""), help="Key prefix in the bucket, e.g. 'trip/'")
    ap.add_argument("--workers", type=int, default=4, help="Files uploaded in parallel (default 4)")
    ap.add_argument("--no-thumbs", action="store_true", help="Skip generating image thumbnails")
    ap.add_argument("--no-previews", action="store_true", help="Skip making H.264 1080p video previews (needs ffmpeg)")
    ap.add_argument("--dry-run", action="store_true", help="List what would be uploaded, don't upload")
    ap.add_argument("--manifest-only", action="store_true", help="Skip uploading; just rebuild manifest.json")
    ap.add_argument(
        "--manifest",
        default=str(Path(__file__).resolve().parent / "web" / "public" / "manifest.json"),
        help="Where to write manifest.json (default: web/public/manifest.json)",
    )
    args = ap.parse_args()

    cfg = load_config()
    prefix = args.prefix.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    s3 = make_client(cfg)

    try:
        s3.head_bucket(Bucket=cfg["bucket"])
    except (ClientError, BotoCoreError) as e:
        sys.exit(f"Cannot access bucket '{cfg['bucket']}': {e}")

    failures: list[tuple[Path, str]] = []

    if not args.manifest_only:
        if not args.path:
            ap.error("path is required (or use --manifest-only)")
        root = Path(args.path).expanduser().resolve()
        if not root.exists():
            sys.exit(f"Not found: {root}")

        if root.is_file():
            files = [root] if media_kind(root) else []
            base = root.parent
        else:
            files = sorted(p for p in root.rglob("*") if p.is_file() and media_kind(p) and not p.name.startswith("."))
            base = root
        if not files:
            sys.exit("No images or videos found.")

        total = sum(f.stat().st_size for f in files)
        print(f"Found {len(files)} files ({human(total)}) -> s3://{cfg['bucket']}/{prefix}")
        if not args.no_thumbs and not HAVE_PIL:
            print("Pillow not installed: skipping image thumbnails (pip install Pillow).")
        if any(media_kind(f) == "video" for f in files):
            if not find_ffmpeg():
                print("ffmpeg not found: videos will have no poster/preview (pip install imageio-ffmpeg).")
            elif not args.no_previews:
                print("Making video posters + 1080p previews for any video that lacks them "
                      "(slow the first time; use --no-previews to skip).")

        if args.dry_run:
            for f in files:
                print(f"  {prefix}{f.relative_to(base).as_posix()}  ({human(f.stat().st_size)})")
            return

        lock = threading.Lock()
        counts = {"uploaded": 0, "skipped": 0}
        with tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024, desc="Uploading") as pbar:
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = {
                    pool.submit(
                        upload_one, s3, cfg, f, prefix + f.relative_to(base).as_posix(), pbar, lock, not args.no_thumbs,
                        not args.no_previews,
                    ): f
                    for f in files
                }
                for fut in as_completed(futures):
                    f = futures[fut]
                    try:
                        counts[fut.result()] += 1
                    except Exception as e:
                        failures.append((f, str(e)))
                        tqdm.write(f"FAILED {f.name}: {e}")

        print(f"Done: {counts['uploaded']} uploaded, {counts['skipped']} already there (same size), {len(failures)} failed.")

    build_manifest(s3, cfg, prefix, Path(args.manifest))

    if failures:
        print("\nFailed files (re-run the same command to retry only these):")
        for f, err in failures:
            print(f"  {f}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
