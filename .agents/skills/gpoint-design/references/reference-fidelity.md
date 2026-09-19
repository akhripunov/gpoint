# Reference fidelity

Inspect the actual reference pixels with an image viewer or Pen screenshot.
Classify the reference as a product-screen Golden, concept reference, external
inspiration, or runtime screenshot before implementation. A concept mockup's
device frame and status bar are not automatically product UI.

Plan geometry from the approved source and the exact viewport specification:
390x844 primary, then 360x800, 393x852, and 430x932 as needed. Compare header,
tabs, hero, cards, tool grid, settings, navigation, typography, surfaces,
radii, spacing, overflow, clipping, and safe areas. Use bounded, component-level
correction passes. Golden appearance wins over a proven renderer limitation,
but the editable Pen structure remains required.

For visual acceptance, inspect exported PNGs and record measurable deviations.
Hard-fail text clipping, tab overlap, horizontal overflow, fixed-navigation
overlap, missing components, placeholder icons, empty badges, or wrong section
order. Do not call a screen approved; owner approval is an explicit gate.
