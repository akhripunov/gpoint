# GPoint third-party infrastructure audit

This file records the permissive open-source boundaries adopted by GPoint. No
source was copied from Misskey, Pixelfed, Mastodon, or Bonfire.

| Component | Version/tag | Upstream | License | GPoint use | Boundary |
|---|---|---|---|---|---|
| Sharp | `0.35.4` | https://github.com/lovell/sharp | Apache-2.0 | bounded image metadata/preview decode | linked npm dependency; originals remain canonical |
| BlurHash | `2.0.5` | https://github.com/woltapp/blurhash | MIT | durable image placeholder encoding | linked npm dependency |
| imgproxy | `v4.0.12` | https://github.com/imgproxy/imgproxy | Apache-2.0 | optional signed image transforms | external container, optional `media` profile; source allowlist required |
| sensitive-detector | `v0.0.2` (`f91d4bc978bea30aee7c1be1c2b6932c5818c7ec`) | https://github.com/misskey-dev/sensitive-detector | MIT | optional external image evidence provider | external HTTP sidecar only; GPoint owns thresholds/policy; no Misskey code copied |
| Meilisearch CE | `v1.53.2` | https://github.com/meilisearch/meilisearch | MIT for CE | optional derived creator/post search indexes | external container, optional `search` profile; no EE binaries/features |
| Novu core | not deployed | https://github.com/novuhq/novu | MIT core; `enterprise/**` proprietary | provider boundary only | `NOVU_RUNTIME=DEFERRED`; no enterprise modules or files used |

## License and security decisions

- Meilisearch Enterprise assets/features are excluded. The optional service is
  the Community Edition image only; the server-side master key is never sent to
  the Mini App.
- Novu is not installed or started in this phase. Its upstream repository
  explicitly marks the root `enterprise/**` tree as commercially licensed.
- sensitive-detector receives normalized images over a private service URL.
  GPoint treats timeout, malformed output, unsupported media, and low confidence
  as `UNCERTAIN`, never `SAFE`.
- imgproxy is optional and configured with signed URLs, size limits, and an
  allowlisted GPoint-controlled source. When unavailable, GPoint falls back to
  its existing authorization-checked media route.
