# LinkedIn video: build notes

Final cut of `linkedin_video_storyboard.html` (the approved storyboard, unchanged in this folder). Only the changes listed in the brief were made; everything else (scene order, story, wording, colours, pacing, layout) follows the storyboard.

## Files

| File | What it is |
|---|---|
| `linkedin_video_storyboard.html` | Approved storyboard (copied unchanged from the Desktop). |
| `linkedin_video.html` | Render page: the storyboard's drawing code at 1080x1920 with real numbers, frame-stepped clock. Changes are marked `VIDEO:` in the code. |
| `make_video_data.py` | Reads the repo's results (no analysis code or result is changed) and writes `video_data.json`. |
| `music_sync.py` | Beat / onset / drop analysis of `music.mp3` and the sync search; writes `music_sync.json`. |
| `render.js` | Headless Chromium (Playwright) frame-by-frame capture, piped to ffmpeg. |
| `linkedin_video_silent.mp4` | 1080x1920, 30 fps, H.264, yuv420p, 91.5 s, no audio. |
| `linkedin_video.mp4` | Same video with music (not in git, see decision 2). |
| `linkedin_video_contact_sheet.png` | One frame per scene, taken from the rendered MP4. |

Rebuild: `python reports/video/make_video_data.py`, `python reports/video/music_sync.py`, then `PW_DIR=<playwright folder> CHROMIUM=<chrome.exe> node reports/video/render.js video`, then the ffmpeg audio step in decision 13.

## Decisions

1. **Input files.** Both files were on the Desktop, not in `reports/video/`. They were copied here: the storyboard as `linkedin_video_storyboard.html`, and `Slayyyter - DANCE... (Official Instrumental).mp3` as `music.mp3`.
2. **Not in git:** `music.mp3` and `linkedin_video.mp4`. The track is a commercial recording, so pushing it, or the video that contains it, to a public repository would redistribute it. Both stay local (`.gitignore`). The silent video, the contact sheet and all sources are committed. `frames/` (preview PNGs) is ignored as well.
3. **Tooling.** Node Playwright 1.63 was installed in `C:\Users\Mathk\pw-video` (outside the repo). Its own Chromium build was not on the machine, so the cached Chromium 149 (`ms-playwright/chromium-1228`) is used through `executablePath`. Audio analysis uses ffmpeg to decode plus numpy from the existing Anaconda install, read-only (nothing was installed there).
4. **Separate render page.** The storyboard file is kept as the approved spec. `linkedin_video.html` uses its drawing code: the scene is drawn in the storyboard's 720x1280 space and scaled 1.5x to 1080x1920. The page's UI (buttons, scene list) is dropped.
5. **Frame-stepped clock.** `renderAt(seconds)` draws one exact frame. `render.js` calls it for every frame `f / 30` and captures the canvas. Fonts (Barlow Condensed 500/600/700, IBM Plex Sans 400/500, IBM Plex Mono 500, from Google Fonts) are loaded and checked with `document.fonts.check` before the first frame; the render stops if any face is missing.
6. **Text size.** Minimum 20 px in the 720 space (= 30 px on output) for all readable text; 27 px (= 40.5 px) for captions and explanations. Checked over all 2,745 frames: smallest text 30 px; everything under 40 px is a tag, chip label, axis label, value or the source citation. Removed instead of enlarged: the four "DOCK n" labels, "example order", "example orders", "workload shape is illustrative".
7. **Layout changes forced by the bigger text.**
   - Info cards (slotting layouts, routing rules, example orders) moved from over the racks into the empty band above the floor, in two rows.
   - The colour legend and the "MY DECISION" banner are in two rows.
   - The four layout chips in "Slotting types" are a 2x2 grid.
   - The WMS rule cards are 600 px wide.
   - Long captions wrap onto two lines.
8. **Two captions added** where the removed "example order" labels were:
   - Under the routing bars: "Average per order, next year's orders".
   - Under the per-order bars: "A real order from next year, metres per rule".
9. **Slotting floor.**
   - The animated A and B zones use the real year-over-year ABC shares (A 28.2%, B 22.6% of SKUs; storyboard 20% and 30%).
   - The new-SKU block moves to the rank where the B zone starts, which is the real rule (storyboard: a fixed mid-floor rank).
10. **Example orders (routing, per order).** These are four real orders from next year's evaluation (year-over-year split) on the recommended layout (Aisle-based velocity + new-product rule).
    - **Selection:** the first order (by index) with the storyboard's line count (3, 4, 7, 6 lines; all found), where the storyboard's rule is best and at least 3% shorter than the other rules, touching 2-5 aisles.
    - **Return and S-shape:** Combined is never longer than these two (Module 7), so for those orders the rule ties with Combined. The bars show the tie, and "✓ best" marks the storyboard's rule.
    - **Drawing:** the walk is drawn on the 10-aisle floor. Each order keeps its aisle count, aisle sequence and pick depth; the gaps between aisles are compressed.
    - **Numbers:** the metres are the real rule lengths.
11. **Routing bars (rule by rule).** Real next-year averages per order on the recommended layout. The storyboard showed lengths of one illustrative order.
12. **Staffing scene.**
    - **Real profile:** bars show orders per hour on an average working day at today's volume. These are the same arrivals as Modules 8a-8c and 11: 19,773 orders, 305 days; 1.8% arrive after 18:00.
    - **Cut-off animation:** with the 18:00 cut-off, the post-18:00 orders move onto the 08:00 bar, because carried orders are picked first the next morning.
    - **Capacity line removed:** the dashed "team needed" line was an illustrative height with no real scale, so it is dropped. The "team needed: 6/4 pickers" label is kept above the workers.
    - **Added line:** "0.94 → 0.62 wage-hours per order", since the brief lists it.
13. **Music.**
    - **Cut:** `atrim` from 155.4 s to 246.9 s.
    - **Fades:** 0.5 s fade-in at the start; 2 s fade-out over the last 2 s (from 89.5 s).
    - **Level:** the segment measured -17.6 LUFS integrated, so -2.4 dB of gain brings it to -20.1 LUFS (peak -6.0 dBFS), which keeps it in the background.
    - **Encoding:** AAC 192 kb/s, 48 kHz; the video stream is copied unchanged.
14. **Cut shifts for the music** (all within +-0.5 s). Story, order and wording are unchanged.
    - **Scene cuts:** the cuts before The problem, Fix 3 and Close.
    - **Sub-scene cuts:** each slotting-ladder step and each example order were treated as cuts too. With fixed 3.3 s and 2.75 s spacing they cannot all land on 0.53 s beats.
    - **Total length:** stays 91.5 s.

## Music sync

- **Track start:** 155.4 s (2:35.4) into `music.mp3`.
- **Tempo:** 112.9 BPM (beat every 0.531 s), from autocorrelation of the onset envelope; beats from a dynamic-programming beat tracker (Ellis 2007).
- **Hit strength:** all-band plus kick-band spectral flux (each scaled to its 99th percentile).
- **Drop ratio:** kick/bass energy in the next 2 s divided by the previous 4 s.
- **Search:** every start offset x every allowed cut shift (whole frames, +-0.5 s), maximising weighted hit strength at the moments (weights: "They walk less." 3, jam clearing 1.5, others 1). "They walk less." had to land on one of the track's three biggest moments.
- **Biggest moments in the track (hit x drop):** 0:28.4, 0:38.6, 0:55.3, 2:28.4, 4:02.2.

| Moment | Video time (s) | Track time | Beat no. | Downbeat | Offset from beat (ms) | Hit strength | Drop ratio |
|---|---|---|---|---|---|---|---|
| "933 m" reveal | 5.767 | 2:41.166 | 303 | yes | -27 | 1.09 | 1.10 |
| Slotting ladder step 1 | 16.400 | 2:51.800 | 323 | yes | -17 | 0.60 | 1.16 |
| Slotting ladder step 2 | 19.567 | 2:54.966 | 329 | no | -31 | 0.64 | 1.47 |
| Slotting ladder step 3 | 22.767 | 2:58.166 | 335 | yes | -24 | 0.65 | 0.86 |
| Slotting ladder step 4 | 26.000 | 3:01.400 | 341 | no | 28 | 0.56 | 1.40 |
| "WMS picks: Return" (order A) | 45.067 | 3:20.466 | 377 | no | -26 | 0.53 | 0.80 |
| "WMS picks: S-shape" (order B) | 47.217 | 3:22.616 | 381 | no | 10 | 0.68 | 1.24 |
| "WMS picks: Largest gap" (order C) | 50.367 | 3:25.766 | 387 | yes | -32 | 0.43 | 1.05 |
| "WMS picks: Combined" (order D) | 52.550 | 3:27.950 | 391 | yes | 38 | 0.52 | 0.25 |
| Jam clearing | 63.533 | 3:38.933 | 412 | no | -8 | 0.47 | 0.19 |
| "They walk less." | 86.800 | 4:02.200 | 456 | no | -240 | 0.73 | 20.0 |

"They walk less." lands 30 ms after the start of the track's biggest drop (4:02.17, the kick returns at 20x the energy of the 4 s before). The beat tracker's grid is 240 ms off at that point because the drop follows a break with no kick, so the drop onset, not the tracked beat, is the reference there. Every other moment is within 40 ms of a tracked beat.

Cut shifts applied (s, + = later):

| Cut | Shift (s) |
|---|---|
| Before "The problem" | +0.067 |
| Ladder step 1 | +0.300 |
| Ladder step 2 | +0.167 |
| Ladder step 3 | +0.067 |
| Ladder step 4 | 0 |
| Order A | +0.267 |
| Order B | -0.333 |
| Order C | +0.067 |
| Order D | -0.500 |
| Before "Fix 3" | +0.033 |
| Before "Close" | -0.400 |

## Numbers: storyboard vs. real values

Sources: `video_data.json`, built from `new_product_rule_routing.csv` (year-over-year, S-shape), `routing_sensitivity.csv`, `congestion_simulation.csv`, `congestion_refined.csv`, `staffing_by_layout_x1.csv` and Module 8a-8c arrivals.

| Where | Storyboard | Real (video) | Changed |
|---|---|---|---|
| Problem: walk per order, random | 933 m | 933 m | no |
| Ladder: ABC zones | 812 m | 816 m | yes |
| Ladder: ABC zones, next-year saving | ↓ 13% | ↓ 13% (12.6%) | no |
| Ladder: Full velocity | 756 m | 760 m | yes |
| Ladder: Full velocity, next-year saving | ↓ 19% | ↓ 19% (18.6%) | no |
| Ladder: Aisle-based | 653 m | 656 m | yes |
| Ladder: Aisle-based, next-year saving | ↓ 30% | ↓ 30% (29.7%) | no |
| Ladder: + New-SKU rule | 630 m | 630 m | no |
| Ladder: + New-SKU rule, next-year saving | ↓ 32% | ↓ 32% (32.4%) | no |
| Same-year Aisle-based figure | ↓ 37% | ↓ 37% (37.1%) | no |
| ABC info card: A zone | top 20% of SKUs | top 28% of SKUs | yes |
| Routing bars (S-shape, Return, Largest gap, Combined, Optimal) | lengths of one illustrative order | 630, 740, 515, 536, 480 m (next-year average per order) | yes |
| Optimal routing extra saving | ↓ 24% | ↓ 24% (23.9%) | no |
| Optimal routing: order base | "across all 19,773 orders" | "on next year's 18,957 orders" | yes (wording, because the 24% is measured on next year's orders) |
| Largest gap vs. optimal | within 7% | within 7% (7.4%) | no |
| Order A (Return), 3 lines: S-shape, Return, Largest gap, Combined | illustrative | 127, 93, 127, 93 m | yes |
| Order B (S-shape), 4 lines | illustrative | 138, 146, 150, 138 m | yes |
| Order C (Largest gap), 7 lines | illustrative | 344, 412, 228, 252 m | yes |
| Order D (Combined), 6 lines | illustrative | 430, 410, 402, 374 m | yes |
| Congestion: waiting per order, strict model | 18 min | 18 min (18.4) | no |
| Congestion: share in the 5 front aisles | 94% | 94% (93.7%) | no |
| Congestion: drop with 5 m segments | ↓ 8–9× | ↓ 8–9× | no |
| Skip-and-return: waiting reduction | ↓ 40% | ↓ 42% (Aisle-based, 99 → 57 s) | yes |
| Staffing: pickers | 6 → 4 | 6 → 4 | no |
| Staffing: labour cost per order | ↓ 33% | ↓ 33% (33.5%) | no |
| Staffing: wage-hours per order | not shown | 0.94 → 0.62 (added) | added |
| Staffing: orders by hour | illustrative shape | real average-day profile, 06-20 h (peak 10.5 orders at 12:00; 1.8% after 18:00) | yes |
| Hook: orders, products, months | 19,773 · 3,791 · 12 | 19,773 · 3,791 · 12 | no |
| Hook: "half their shift walking" | Tompkins et al. | unchanged (external source, not a repo result) | no |

## Render

- 2,745 frames in 100 s (Chromium canvas `toDataURL` PNG piped into ffmpeg, libx264 preset slow, CRF 18, yuv420p, faststart).
- `ffprobe`: h264, 1080x1920, 30/1 fps, yuv420p, 91.500 s. The music version adds AAC audio of 91.500 s.
