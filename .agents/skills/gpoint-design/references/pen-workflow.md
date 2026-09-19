# Pen workflow

For any Pen read or write, follow this order:

1. Read the native Pen skill and the relevant referenced instructions.
2. Call `read_skill()` before using Pen operations.
3. Call `get_app_state()` and inspect the current document; call `get_style()`
   when style guidance is relevant.
4. Inspect the existing component/template before changing it. Use canonical
   components and tokens; do not create detached screen copies.
5. Execute only the smallest required change. For no-change audits, stop after
   the read/capability probe.
6. Take a screenshot of the actual rendered result after each major section and
   inspect the image, not only node geometry. Correct at most three times per
   section and distinguish renderer limitations from implementation errors.
7. Save the physical `.pen`, record size/hash/mtime, reopen or re-read it, and
   verify the changed nodes and reusable components persist.

Never replace an editable Pen screen with a PNG. A Golden may be locked beside
the editable screen for comparison only. Do not claim owner approval.
