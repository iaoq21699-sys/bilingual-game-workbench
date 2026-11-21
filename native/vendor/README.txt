Native dependency versions (downloaded from the upstream projects)

ReShade SDK: v6.8.0 / API 20
  Include directory: reshade/include
  Exact commit: 18deaa52de0c425a78b329e9cb3c497281cd00ec
  License: SDK headers BSD-3-Clause OR MIT; upstream LICENSE.md retained.
  The reference/source files are upstream configuration/API reference only;
  they are not compiled into the add-on.

Dear ImGui: v1.92.5-docking / IMGUI_VERSION_NUM 19250
  Include directory: imgui
  License: MIT, imgui/LICENSE.txt
  Include imgui.h BEFORE reshade.hpp. Never compile imgui.cpp for this add-on:
  the matching ReShade SDK supplies the wrappers and ReShade owns the context.

SQLite: 3.54.1
  Source: sqlite/sqlite3.c; include directory: sqlite
  Public domain, sqlite/LICENSE.txt. SOURCE.json contains the official archive
  SHA3-256, verified before extraction, plus the downloaded archive SHA-256.

Each dependency has a SOURCE.json. The ReShade runtime is cached separately in
tools/reshade-runtime for local use and must not be shipped in the product ZIP.

ReShade 6.8.0 runtime configuration keys verified in upstream source:
  [ADDON] AddonPath: add-on directory; resolved from ReShade's base directory.
  [INPUT] KeyOverlay: four integer values for the ReShade settings shortcut.
          InputProcessing: 0 = none, 1 = capture requested by ImGui,
                           2 = block while the overlay requests input (default).
  [STYLE] Font: custom font file, e.g. C:\Windows\Fonts\msyh.ttc
          FontSize: float, default 13; Alpha: float, global style opacity.
  [OVERLAY] TutorialProgress: tutorial stage; 4 indicates completion.
            ShowClock, ShowFPS, ShowFrameTime, ShowPresetName: boolean values.

The custom panel may render from addon_event::reshade_overlay, which runs even
when the ReShade settings window is closed. While that panel is interactive,
call effect_runtime::block_input_next_frame() each frame to block game input;
InputProcessing must not be 0. register_overlay() instead creates a window
inside the ReShade settings UI and is not required for the standalone panel.
