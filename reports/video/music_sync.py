"""
Music sync for the LinkedIn video: tempo, beats, onset strength and drops of music.mp3 (decoded with
ffmpeg, analysed with numpy), then the track start offset and the cut shifts (each <= 0.5 s) that put
the strongest musical moments on the key visual moments. Writes music_sync.json, which the video page
reads for its timing.

Cuts that may move: the scene cuts before "The problem", "Fix 3: congestion" and "Close", and the
sub-scene cuts inside Fix 1 (each slotting-ladder step) and Fix 2 (each example order).

Run: python reports/video/music_sync.py
"""
import json
import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SR, HOP, NFFT = 22050, 256, 2048
FPS = 30
MAX_SHIFT = 0.5

DUR = [4, 4.5, 4, 17, 28, 13, 9, 6, 6]                       # storyboard scene durations
START = np.concatenate([[0], np.cumsum(DUR)])
# (key, cut time in the storyboard timeline, moment = cut + lead, weight, label)
LADDER = [(f"step{k}", START[3] + 0.3 + k * 3.3, 0.0, 1.0, f"slotting ladder step {k} (boxes move, counter drops)") for k in range(1, 5)]
ORDERS = [(f"order{i}", START[4] + 14 + i * 2.75, 1.3, 1.0, f'"WMS picks: ..." order {"ABCD"[i]}') for i in range(4)]
CUTS = [("problem", START[1], 1.7, 1.0, '"933 m" reveal')] + LADDER + ORDERS + \
       [("congestion", START[5], 6.0, 1.5, "jam clearing"), ("close", START[8], 1.7, 3.0, '"They walk less."')]
SHIFTS = np.arange(-int(MAX_SHIFT * FPS), int(MAX_SHIFT * FPS) + 1) / FPS


def decode():
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(HERE / "music.mp3"), "-ac", "1", "-ar", str(SR),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def features(x):
    n = 1 + (len(x) - NFFT) // HOP
    win = np.hanning(NFFT).astype(np.float32)
    frames = np.lib.stride_tricks.as_strided(x, (n, NFFT), (x.strides[0] * HOP, x.strides[0]))
    mag = np.abs(np.fft.rfft(frames * win, axis=1))
    freqs = np.fft.rfftfreq(NFFT, 1 / SR)
    logm = np.log1p(100 * mag)
    d = np.maximum(np.diff(logm, axis=0, prepend=logm[:1]), 0)
    flux = d.sum(axis=1)                                      # all bands: any strong hit
    kick = d[:, freqs < 150].sum(axis=1)                      # low band: kick / bass hits
    low = (mag[:, freqs < 150] ** 2).sum(axis=1)              # low-band energy, for build-ups and drops
    return flux, kick, low, n


def tempo(env, fr):
    o = env - env.mean()
    ac = np.correlate(o, o, mode="full")[len(o) - 1:]
    lags = np.arange(int(fr * 60 / 180), int(fr * 60 / 70) + 1)
    bpm = 60 * fr / lags
    k = lags[np.argmax(ac[lags] * np.exp(-0.5 * (np.log2(bpm / 120) / 0.9) ** 2))]
    y0, y1, y2 = ac[k - 1], ac[k], ac[k + 1]
    kk = k + 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2)
    return 60 * fr / kk, kk


def beats(env, period, alpha=100.0):
    """Dynamic-programming beat tracker (Ellis 2007)."""
    n = len(env)
    score, back = env.astype(float).copy(), -np.ones(n, dtype=int)
    lo, hi = int(round(period / 2)), int(round(2 * period))
    for t in range(hi, n):
        tau = np.arange(t - hi, t - lo + 1)
        c = score[tau] - alpha * np.log((t - tau) / period) ** 2
        j = np.argmax(c)
        score[t], back[t] = env[t] + c[j], tau[j]
    t, out = int(np.argmax(score[-hi:])) + n - hi, []
    while t >= 0:
        out.append(t)
        t = back[t]
    return np.array(out[::-1])


def main():
    x = decode()
    fr = SR / HOP
    flux, kick, low, n = features(x)
    hit = 0.5 * flux / np.percentile(flux, 99) + 0.5 * kick / np.percentile(kick, 99)
    bpm, period = tempo(hit, fr)
    bt = beats(hit / hit.std(), period)
    beat_t = bt / fr
    # downbeat phase: the beat position (mod 4) with the strongest hits
    phase = int(np.argmax([hit[bt[p::4]].mean() for p in range(4)]))
    down = set(bt[phase::4].tolist())
    # strength of a moment landing at frame i: strongest hit within +-40 ms, + beat / downbeat bonus
    tol = int(round(0.04 * fr))
    pad = np.pad(hit, tol, mode="edge")
    S = np.lib.stride_tricks.sliding_window_view(pad, 2 * tol + 1).max(axis=1)
    on_beat = np.zeros(n)
    for b in bt:
        on_beat[max(0, b - tol):b + tol + 1] = np.maximum(on_beat[max(0, b - tol):b + tol + 1], 0.25 + 0.25 * (b in down))
    S = S + on_beat
    cl = np.cumsum(np.concatenate([[0], low]))
    def mean_low(a, b):
        a, b = max(0, a), min(n, b)
        return (cl[b] - cl[a]) / max(1, b - a)
    w2, w4 = int(2 * fr), int(4 * fr)
    drop = np.clip(np.array([mean_low(i, i + w2) / (mean_low(i - w4, i) + 1e-9) for i in range(n)]), 0, 20)
    big = hit * np.sqrt(drop)                                 # biggest moments: a strong hit that starts a louder section
    picked = []
    for i in np.argsort(-big):
        if all(abs(i - p) > 4 * fr for p in picked):
            picked.append(int(i))
        if len(picked) == 5:
            break
    top_hits = sorted(picked)
    total = START[-1]
    offsets = np.arange(0, int((len(x) / SR - total - 2) * fr)) / fr
    tot = np.zeros(len(offsets))
    best_d = {}
    for key, cut, lead, w, _ in CUTS:
        vals = []
        for d in SHIFTS:
            i = np.round((offsets + cut + d + lead) * fr).astype(int)
            v = (S[i] if key != "close" else S[i] * np.sqrt(drop[i])) - 0.3 * abs(d)
            vals.append(v)
        vals = np.array(vals)
        best_d[key] = SHIFTS[np.argmax(vals, axis=0)]
        tot += w * vals.max(axis=0)
    # "They walk less." must land on one of the track's 3 biggest moments
    biggest3 = sorted(top_hits, key=lambda i: -big[i])[:3]
    close_t = offsets + START[8] + 1.7 + best_d["close"]
    ok = np.array([min(abs(c * fr - b) for b in biggest3) <= tol for c in close_t])
    k = int(np.argmax(np.where(ok, tot, -1e9)))
    o = float(offsets[k])
    res = {"bpm": round(float(bpm), 2), "beat_period_s": round(float(60 / bpm), 4), "start_offset_s": round(o, 3),
           "track_biggest_moments_s": [round(i / fr, 2) for i in top_hits],
           "cut_shift_s": {key: round(float(best_d[key][k]), 4) for key, *_ in CUTS}, "moments": []}
    for key, cut, lead, w, label in CUTS:
        vt = cut + float(best_d[key][k]) + lead
        tt = o + vt
        bi = int(np.argmin(np.abs(beat_t - tt)))
        i = int(round(tt * fr))
        res["moments"].append({"moment": label, "video_time_s": round(vt, 3), "track_time_s": round(tt, 3),
                               "track_time": f"{int(tt // 60)}:{tt % 60:06.3f}", "beat_number": bi + 1,
                               "downbeat": bool(bt[bi] in down), "offset_from_beat_ms": int(round(1000 * (tt - beat_t[bi]))),
                               "hit_strength": round(float(hit[max(0, i - tol):i + tol + 1].max()), 2),
                               "drop_ratio": round(float(drop[i]), 2)})
    res["hit_strength_scale"] = "0.5 x (all-band onset / its 99th percentile) + 0.5 x (kick-band onset / its 99th percentile)"
    (HERE / "music_sync.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
