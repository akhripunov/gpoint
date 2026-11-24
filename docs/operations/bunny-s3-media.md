# Bunny media: existing S3 backend

Use the existing Frankfurt gpoint-media bucket and CDN Pull Zone:
MEDIA_STORAGE_PROVIDER=s3, S3_ENDPOINT=https://de-s3.storage.bunnycdn.com,
S3_REGION=de, S3_BUCKET=gpoint-media, S3_ACCESS_KEY_ID=gpoint-media,
S3_PUBLIC_BASE_URL=https://cdn.gpoint.one, S3_SIGNED_READ=true.
Provision S3_SECRET_ACCESS_KEY and BUNNY_CDN_TOKEN_KEY only in the protected
runtime environment, never in Git. CDN advanced token authentication is required;
IP validation is disabled. Signed links expire in 900 seconds. Existing local
assets remain readable if their bytes exist; legacy channel avatars can be
rehydrated from Telegram. Verify an actual imported post, S3 readback, CDN
authorization, and no durable local copy. Do not touch the backup zone.
