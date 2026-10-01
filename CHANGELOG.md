# Changelog

## Unreleased

- **Renders** takes several folders. **+** adds another one, for example trims rendered later into their own folder. If a clip was rendered into more than one of them, the newest render is used, with a warning, also when the clip is matched by timecode. In the command line, repeat `--renders`.
- When several different renders cover a clip that is matched by timecode, the warning says so instead of "no render found".
- The preview marks "no render" exactly where the export leaves a clip offline: it runs the export's own render matching, reads the render headers and lists the same warnings (several renders with one name, unreadable files, another frame rate, matches by timecode).
- Render folders made before the edit changed, where the numbers are off by one, are safer:
  - A clip is no longer linked to another source's render just because the render has its number.
  - When the render with a clip's name holds none of the clip's frames and exactly one other render of the same source covers it, the clip takes that one.
  - A render that holds some of the clip's frames stays the clip's own, as after a trim past the handles, and gets the "does not cover" warning.
  - Overlapping cuts of one source cannot be told apart by timecode, so the clip keeps the render with its name.
- A still is found by its clip number (`V2-0008_`) only in a one-frame render whose name matches apart from digits. Resolve rewrites only the digits (`1.png` → `V2-0008_0.mov`). Another clip's render with that number is left alone, with a warning. Stills named only by digits (`1.png`, `2.png`) leave nothing to compare, so they still take any one-frame render with their number.
- Stills keep holding their frame with **Bypass → Retime** on, instead of playing a one-frame render.
- With a timeline name that has characters file names cannot hold, stills are looked up by the right clip number. With `{INDEX}` after `{SOURCE}` in the naming template, stills are no longer matched by the part of the name before `{SOURCE}`, since that part holds no clip number.
- Render names with letters like й, ё or í match whether the file system stores them composed or decomposed. macOS network shares list them decomposed.
- **Bypass → Composite** no longer removes effects whose name the XML does not give.
- A render that cannot be read for any reason, such as a file without a video stream or ffprobe timing out, is reported as unreadable. The export and the preview go on without it.
- MXF renders: ffprobe takes the frame count from the MXF header instead of reading every render to the end.
- Changing a **Bypass** option asks to refresh the preview, since the preview now depends on it.
- A settings file with a value of the wrong type no longer keeps the window from opening; that setting falls back to its default.
- The startup error message names Conform Export instead of the tool's old name.

## 1.0.2

- The window remembers its settings and folders between runs (stored outside the install folder, so updates keep them).
- New **= Renders** button next to Output: writes the export into the render folder.
- Removed the `Start Resolve` helpers; Resolve is simply restarted after installing.
- Renders are matched by name whatever their extension, also when a render kept the source's extension, and otherwise by the clip number (`V2-0008_`), since Resolve rewrites the source part for stills; the preview finds them the same way as the export.
- Stills link to the one-frame render Resolve makes for them and keep holding that frame, instead of being left offline.
- Clips next to a transition are conformed instead of being left unchanged.
- New bypass option: composite mode (previously reset together with opacity).
- Documented the requirement: DaVinci Resolve Studio 19 or later (the free version has no script windows since 19.1 and no Python scripting since 21.1).

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
