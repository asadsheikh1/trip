# Trip media → S3 → gallery

Two parts:

1. `upload.py` uploads a local folder of photos/videos to S3 **byte-for-byte** (no re-encoding, no compression), then writes `web/public/manifest.json`.
2. `web/` is a React (Vite) gallery with no backend. It reads the manifest and shows/downloads files straight from the public S3 URLs.

## 1. Bucket setup (one time)

The bucket must allow public reads:

- S3 → bucket → Permissions → **Block public access**: turn off "Block all public access".
- Bucket policy (replace `YOUR_BUCKET`):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicRead",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::YOUR_BUCKET/*"
  }]
}
```

The IAM user for your keys needs `s3:ListBucket`, `s3:GetObject`, `s3:PutObject` on the bucket.

## 2. Upload

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# fill in .env first (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET, AWS_REGION)
python upload.py /path/to/your/media
```

- Subfolders are preserved and become folder filters in the gallery.
- Re-running is safe: files already in S3 with the same size are skipped, so an interrupted 16 GB upload resumes.
- Large files use multipart upload (64 MB parts, parallel). Tune with `--workers N`.
- Image thumbnails (800px JPEG) go to `_thumbs/` so the grid loads fast. Originals are never touched. Skip with `--no-thumbs`.
- Other options: `--prefix trip/`, `--dry-run`, `--manifest-only` (rebuild the manifest from what's in the bucket).
- Every object is stored with `Content-Disposition: attachment`, so the download button gives the exact original file. (Inline display in the gallery is unaffected.)

## 3. Gallery

```bash
cd web
npm install
npm run dev        # local preview
npm run build      # static site in web/dist — host anywhere (S3, Netlify, Vercel, GitHub Pages…)
```

Re-run `upload.py` whenever you add media, then rebuild/redeploy `web/` (the manifest is bundled with it).

## Note on keys

The gallery deliberately does **not** contain your access/secret keys. Anything in a browser app is visible to everyone who opens it, and your friends would get write access to your bucket. Since the bucket is public, the script lists it once with your keys and bakes the public URLs into `manifest.json`.
