# Changelog

## Unreleased

- Removed the `Start Resolve` helpers; Resolve is simply restarted after installing.
- Documented the requirement: DaVinci Resolve 19 or later.

## 1.0.1

- Keeps working on Resolve versions whose API has no Inspector properties: the flip warning is skipped instead of stopping the export.

## 1.0.0

First public release.

- Names clips the way DaVinci Resolve names individual-clip renders (`V1-0001_A003C014.mov`), numbered from 1 on every track.
- Points every clip at its render and recalculates in/out points from the render's timecode, so renders trimmed with handles line up. Handles reverse clips and mixed frame rates.
- Without a render folder, cuts the paths to the camera originals so clips stay offline under their new names.
- Bypass options for transforms, crop, retime and opacity that are already baked into the renders.
- Builds the DRT inside Resolve from the conformed XML, in a temporary bin that is deleted afterwards.
- Exports speed ramps as a constant average speed with exact start and end frames, since Resolve's own XML for ramps is wrong.
- Leaves clips without media ("Slug" in Resolve's XML) unrenamed and warns about them.
- Per-user installer for macOS, Windows and Linux. On macOS it sets `PYTHON3HOME` so Resolve finds a Python it can load.
