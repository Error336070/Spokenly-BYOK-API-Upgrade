"""Generate the README's SVG graphics into docs/assets/.

Design: a transcription editor's alignment view. Paper-coloured sheets, ink lines, Georgia for words
that were spoken or written, and one accent, record red, used only for the audio signal.
The banner is the only animated graphic; the others are static.

Edit the data below and re-run:

    .venv\\Scripts\\python.exe docs\\build_assets.py
"""
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent / "assets"

SERIF = "Georgia, 'Times New Roman', Times, serif"
SANS = "'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "Consolas, 'SFMono-Regular', Menlo, monospace"

PAPER = "#EEF2F6"
INK = "#102A43"
MUTED = "#52667A"
RULE = "#C5D0DB"
SLATE = "#486581"
RED = "#E03A3E"

# One real benchmark command, laid out as an alignment view: word, start px, end px, syllables
WORDS = [("Open", 64, 168, 2), ("the", 196, 252, 1), ("OpenRouter", 282, 586, 4), ("dashboard.", 616, 864, 3)]
WAVE_X0, WAVE_X1, CLIP_SECONDS, RELEASE_X = 64, 864, 2.6, 900

# AssemblyAI latency (ms), median of 3 runs: before, FLAC upload via OpenRouter, AssemblyAI API with FLAC
SPEED = [
    ("Short command", "2 to 4 seconds of audio", 640, 340, 200),
    ("Dictation", "12 seconds of audio", 969, 636, 551),
    ("Paragraph", "55 seconds of audio", 3254, 1631, 1371),
]

# Shootout via OpenRouter: name, median latency ms, WER %, fillers kept, kept in config, label side
SHOOTOUT = [
    ("AssemblyAI", 638, 1.9, 1, True, "left"),
    ("MAI-Transcribe 2", 1042, 2.5, 0, True, "right-low"),
    ("GPT Transcribe", 964, 3.2, 0, True, "left"),
    ("Voxtral Mini", 630, 5.1, 5, False, "left"),
    ("Voxtral Small", 1095, 5.7, 3, False, "right"),
    ("Scribe v2", 1017, 5.7, 0, False, "left-high"),
    ("Gemini 3.5", 2897, 12.1, 4, False, "left"),
]


def frame(w, h, title, desc, body, style="", defs=""):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-labelledby="title desc">
<title id="title">{title}</title>
<desc id="desc">{desc}</desc>
<defs>{defs}</defs>
<style>
  text {{ font-family: {SANS}; fill: {INK}; font-variant-numeric: tabular-nums; }}
  .serif {{ font-family: {SERIF}; }}
  .mono {{ font-family: {MONO}; }}
  .muted {{ fill: {MUTED}; }}
  {style}
</style>
<rect x="1" y="1" width="{w - 2}" height="{h - 2}" rx="14" fill="{PAPER}" stroke="{RULE}" stroke-width="2"/>
{body}
</svg>
"""


def heading(title, sub):
    return (f'<text class="serif" x="64" y="72" font-size="28" font-weight="700">{title}</text>'
            f'<text class="muted" x="64" y="102" font-size="15">{sub}</text>')


# ----------------------------------------------------------------------------- banner

def banner():
    period = 6.0                       # seconds per loop
    sweep = 2.6 / period * 100         # % of the loop the playhead takes to cross the clip
    width = WAVE_X1 - WAVE_X0
    wave, cy = [], 282

    def amplitude(x):
        for _, a, b, syl in WORDS:
            if a <= x <= b:
                t = (x - a) / (b - a)
                env = math.sin(math.pi * t) ** 0.6
                bumps = abs(math.sin(math.pi * syl * t)) ** 0.7
                jitter = 0.72 + 0.28 * abs(math.sin(x * 0.91) * math.cos(x * 0.37))
                return 6 + 52 * env * (0.35 + 0.65 * bumps) * jitter
        return 2.5
    for x in range(WAVE_X0, WAVE_X1 + 1, 6):
        h = amplitude(x)
        wave.append(f'<rect x="{x}" y="{cy - h:.1f}" width="3" height="{2 * h:.1f}" rx="1.5"/>')

    words, styles = [], []
    for i, (word, a, b, _) in enumerate(WORDS):
        seconds = (a - WAVE_X0) / width * CLIP_SECONDS
        p = (a - WAVE_X0) / width * sweep
        words.append(f"""
  <g class="w{i}">
    <path d="M{a} 352 v-6 H{b} v6" fill="none" stroke="{SLATE}" stroke-width="1.5"/>
    <text class="serif" x="{a}" y="380" font-size="24">{word}</text>
    <text class="muted" x="{a}" y="404" font-size="13">{seconds:.2f} s</text>
  </g>""")
        styles.append(f".w{i} {{ animation: w{i} {period}s linear infinite; }}"
                      f"@keyframes w{i} {{ 0%, {p:.1f}% {{ opacity: 0; }} {p + 2:.1f}%, 92% {{ opacity: 1; }} 98%, 100% {{ opacity: 0; }} }}")

    dim_len = 1040 - RELEASE_X
    body = f"""
  <text class="serif" x="64" y="104" font-size="54" font-weight="700">Spokenly BYOK API Upgrade</text>
  <text class="muted" x="66" y="146" font-size="20">A local proxy between Spokenly and the speech-to-text provider you choose.</text>

  <circle cx="72" cy="196" r="6" fill="{RED}"/>
  <text x="88" y="201" font-size="14" font-weight="600" fill="{RED}">Push-to-talk recording</text>

  <g fill="{RULE}">{''.join(wave)}</g>
  <g clip-path="url(#heard)"><g fill="{INK}">{''.join(wave)}</g></g>
  <line x1="{WAVE_X0}" x2="{WAVE_X1}" y1="{cy}" y2="{cy}" stroke="{RULE}" stroke-width="1"/>
  {''.join(words)}

  <g class="playhead"><line x1="{WAVE_X0}" x2="{WAVE_X0}" y1="214" y2="350" stroke="{RED}" stroke-width="2"/>
    <circle cx="{WAVE_X0}" cy="214" r="4" fill="{RED}"/></g>

  <g class="result">
    <line x1="{RELEASE_X}" x2="{RELEASE_X}" y1="214" y2="350" stroke="{RED}" stroke-width="2"/>
    <text x="{RELEASE_X + 12}" y="228" font-size="14" font-weight="600" fill="{RED}">Key released</text>
    <path class="dim" d="M{RELEASE_X} 300 H1040" fill="none" stroke="{INK}" stroke-width="2" stroke-dasharray="{dim_len}"/>
    <path d="M1040 290 v20" stroke="{INK}" stroke-width="2"/>
    <text x="{RELEASE_X + 12}" y="282" font-size="34" font-weight="700">200 ms</text>
    <text class="serif" x="{RELEASE_X + 12}" y="346" font-size="21">Open the OpenRouter dashboard.</text>
    <text class="muted" x="{RELEASE_X + 12}" y="368" font-size="13">appears in Spokenly</text>
  </g>
"""
    defs = (f'<clipPath id="heard"><rect class="sweep" x="{WAVE_X0 - 2}" y="200" width="{width + 6}" height="170"/></clipPath>')
    style = f"""
  .playhead {{ opacity: 0; animation: play {period}s linear infinite; }}
  @keyframes play {{ 0% {{ opacity: 1; transform: translateX(0); }} {sweep:.1f}% {{ opacity: 1; transform: translateX({width}px); }}
                     {sweep + 0.1:.1f}%, 100% {{ opacity: 0; transform: translateX({width}px); }} }}
  .sweep {{ transform-box: view-box; transform-origin: {WAVE_X0}px 0; animation: sweep {period}s linear infinite; }}
  @keyframes sweep {{ 0% {{ transform: scaleX(0); }} {sweep:.1f}%, 100% {{ transform: scaleX(1); }} }}
  .result {{ animation: result {period}s linear infinite; }}
  @keyframes result {{ 0%, {sweep:.1f}% {{ opacity: 0; }} {sweep + 1.5:.1f}%, 92% {{ opacity: 1; }} 98%, 100% {{ opacity: 0; }} }}
  .dim {{ animation: dim {period}s ease-out infinite; }}
  @keyframes dim {{ 0%, {sweep + 1:.1f}% {{ stroke-dashoffset: {dim_len}; }} {sweep + 9:.1f}%, 100% {{ stroke-dashoffset: 0; }} }}
  {' '.join(styles)}
  @media (prefers-reduced-motion: reduce) {{
    .playhead {{ display: none; }}
    .sweep, .result, .dim, {', '.join(f'.w{i}' for i in range(len(WORDS)))} {{ animation: none; }}
  }}
"""
    return frame(1280, 440, "Spokenly BYOK API Upgrade",
                 "A local proxy between Spokenly and the speech-to-text provider you choose. A recorded "
                 "command, Open the OpenRouter dashboard, is aligned word by word under its waveform. "
                 "The transcript arrives 200 ms after the key is released.", body, style, defs)


# ----------------------------------------------------------------------------- pipeline

def pipeline():
    y = 262
    steps = [(430, "Pick the route", "from the model name"),
             (600, "Re-encode to FLAC", "45% of the WAV size"),
             (770, "Add options", "and your vocabulary")]
    nodes = []
    for i, (x, title, sub) in enumerate(steps, 1):
        nodes.append(f'<circle cx="{x}" cy="{y}" r="15" fill="{PAPER}" stroke="{INK}" stroke-width="2"/>'
                     f'<text x="{x}" y="{y + 5}" text-anchor="middle" font-size="14" font-weight="700">{i}</text>'
                     f'<text x="{x}" y="{y + 46}" text-anchor="middle" font-size="15" font-weight="600">{title}</text>'
                     f'<text class="muted" x="{x}" y="{y + 66}" text-anchor="middle" font-size="13">{sub}</text>')
    arrow = lambda x, yy, d="right": (
        f'<path d="M{x} {yy} l-9 -5 v10 z" fill="{RED}"/>' if d == "right" else
        f'<path d="M{x} {yy} l9 -5 v10 z" fill="{SLATE}"/>' if d == "left" else
        f'<path d="M{x} {yy} l-5 9 h10 z" fill="{SLATE}"/>')
    body = f"""
  {heading("How a dictation travels", "Red carries your audio. Blue carries the transcript back.")}

  <g fill="none" stroke="{INK}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <rect x="128" y="214" width="32" height="54" rx="16"/>
    <path d="M116 250 a28 28 0 0 0 56 0 M144 278 v14 M128 292 h32"/>
  </g>
  <text class="serif" x="144" y="326" text-anchor="middle" font-size="20" font-weight="700">Spokenly</text>
  <text class="muted" x="144" y="346" text-anchor="middle" font-size="13">records WAV while you</text>
  <text class="muted" x="144" y="363" text-anchor="middle" font-size="13">hold the key</text>

  <rect x="330" y="150" width="540" height="248" rx="6" fill="none" stroke="{INK}" stroke-width="1.5"/>
  <text class="serif" x="352" y="186" font-size="20" font-weight="700">Dictation proxy</text>
  <text class="muted" x="852" y="186" text-anchor="end" font-size="13">listens on 127.0.0.1:8765</text>
  <text class="muted" x="352" y="378" font-size="13">Logs every request to SQLite with its latency, cost and transcript.</text>

  <path d="M190 {y} H415 M445 {y} H585 M615 {y} H755 M785 {y} H900 V176 H960 M900 {y} V352 H960"
        fill="none" stroke="{RED}" stroke-width="2.5"/>
  {arrow(330, y)}{arrow(960, 176)}{arrow(960, 352)}
  <text x="214" y="{y - 12}" font-size="13" fill="{RED}">audio and</text>
  <text x="214" y="{y + 22}" font-size="13" fill="{RED}">route name</text>
  {''.join(nodes)}

  <text class="mono" x="908" y="{y - 34}" font-size="13" fill="{RED}">backend: openrouter</text>
  <text class="mono" x="908" y="{y + 44}" font-size="13" fill="{RED}">backend: native</text>

  <line x1="980" x2="980" y1="152" y2="204" stroke="{INK}" stroke-width="2"/>
  <text class="serif" x="996" y="172" font-size="20" font-weight="700">OpenRouter</text>
  <text class="muted" x="996" y="196" font-size="13">MAI-Transcribe 2, AssemblyAI, GPT Transcribe</text>

  <line x1="980" x2="980" y1="328" y2="380" stroke="{INK}" stroke-width="2"/>
  <text class="serif" x="996" y="348" font-size="20" font-weight="700">Provider's own API</text>
  <text class="muted" x="996" y="372" font-size="13">AssemblyAI Sync API in the EU</text>

  <path d="M1140 382 V448 H144 V378" fill="none" stroke="{SLATE}" stroke-width="2" stroke-dasharray="7 5"/>
  {arrow(144, 372, "up")}
  <text x="620" y="472" text-anchor="middle" font-size="13" fill="{SLATE}">transcript, returned in OpenAI's format</text>
"""
    return frame(1280, 500, "How a dictation travels",
                 "Spokenly records WAV audio and sends it with a route name to the local proxy. The proxy picks "
                 "the route, re-encodes the audio to FLAC, adds provider options and vocabulary, and sends it to "
                 "OpenRouter or the provider's own API. The transcript returns to Spokenly in OpenAI's format.",
                 body)


# ----------------------------------------------------------------------------- speed

def speed():
    x0, x1, vmax = 470, 1210, 3400
    px = lambda ms: x0 + ms / vmax * (x1 - x0)
    top, gap = 168, 82
    bottom = top + gap * (len(SPEED) - 1) + 44
    parts = []
    for ms in range(0, 3001, 500):
        label = f"{ms:,}" + (" ms" if ms == 3000 else "")
        parts.append(f'<line x1="{px(ms):.1f}" x2="{px(ms):.1f}" y1="{top - 30}" y2="{bottom - 18}" stroke="{RULE}"/>'
                     f'<text class="muted" x="{px(ms):.1f}" y="{bottom + 4}" text-anchor="middle" font-size="13">{label}</text>')
    for i, (name, sub, before, flac, native) in enumerate(SPEED):
        y = top + i * gap
        xb, xf, xn = px(before), px(flac), px(native)
        parts.append(f"""
  <text class="serif" x="64" y="{y - 2}" font-size="20" font-weight="700">{name}</text>
  <text class="muted" x="64" y="{y + 18}" font-size="13">{sub}</text>
  <text class="serif" x="420" y="{y + 9}" text-anchor="end" font-size="28" font-weight="700">{before / native:.1f}x</text>
  <line x1="{xn:.1f}" x2="{xb:.1f}" y1="{y}" y2="{y}" stroke="{INK}" stroke-width="2"/>
  <circle cx="{xb:.1f}" cy="{y}" r="7" fill="{PAPER}" stroke="{MUTED}" stroke-width="2"/>
  <circle cx="{xf:.1f}" cy="{y}" r="5" fill="{SLATE}"/>
  <circle cx="{xn:.1f}" cy="{y}" r="8" fill="{RED}"/>
  <text class="muted" x="{xb:.1f}" y="{y - 16}" text-anchor="middle" font-size="13">{before:,}</text>
  <text x="{xn:.1f}" y="{y - 16}" text-anchor="middle" font-size="14" font-weight="700" fill="{RED}">{native:,}</text>""")
    legend = (f'<circle cx="{x0 + 6}" cy="128" r="6" fill="{PAPER}" stroke="{MUTED}" stroke-width="2"/>'
              f'<text class="muted" x="{x0 + 20}" y="133" font-size="13">before</text>'
              f'<circle cx="{x0 + 96}" cy="128" r="5" fill="{SLATE}"/>'
              f'<text class="muted" x="{x0 + 108}" y="133" font-size="13">FLAC upload through OpenRouter</text>'
              f'<circle cx="{x0 + 330}" cy="128" r="7" fill="{RED}"/>'
              f'<text class="muted" x="{x0 + 344}" y="133" font-size="13">AssemblyAI\'s own API with FLAC</text>'
              f'<text class="muted" x="420" y="133" text-anchor="end" font-size="13">faster</text>')
    body = f"""
  {heading("AssemblyAI latency per dictation", "Median of 3 runs on real recordings, October 2026. Milliseconds from upload to transcript.")}
  {legend}
  {''.join(parts)}
"""
    return frame(1280, bottom + 36, "AssemblyAI latency before and after optimizing",
                 "Short commands went from 640 to 200 ms, 3.2 times faster. A 12 second dictation went from "
                 "969 to 551 ms, and a 55 second paragraph from 3254 to 1371 ms.", body)


# ----------------------------------------------------------------------------- shootout

def shootout():
    L, R, T, B = 150, 1210, 142, 448
    xmax, ymax = 3000, 13
    px = lambda ms: L + ms / xmax * (R - L)
    py = lambda wer: B - wer / ymax * (B - T)
    parts = [f'<rect x="{L}" y="{py(4):.0f}" width="{px(1200) - L:.0f}" height="{B - py(4):.0f}" fill="{RULE}" fill-opacity="0.35"/>',
             f'<text class="muted" x="{L + 12}" y="{B - 12}" font-size="13">faster and more accurate</text>']
    for ms in range(0, xmax + 1, 500):
        label = f"{ms:,}" + (" ms" if ms == xmax else "")
        parts.append(f'<line x1="{px(ms):.1f}" x2="{px(ms):.1f}" y1="{T}" y2="{B}" stroke="{RULE}"/>'
                     f'<text class="muted" x="{px(ms):.1f}" y="{B + 24}" text-anchor="middle" font-size="13">{label}</text>')
    for wer in range(0, ymax + 1, 2):
        parts.append(f'<line x1="{L}" x2="{R}" y1="{py(wer):.1f}" y2="{py(wer):.1f}" stroke="{RULE}"/>'
                     f'<text class="muted" x="{L - 12}" y="{py(wer) + 4:.1f}" text-anchor="end" font-size="13">{wer}%</text>')
    parts.append(f'<text class="muted" x="{L - 12}" y="{T - 16}" text-anchor="end" font-size="13">word errors</text>')
    for name, ms, wer, fill, kept, side in SHOOTOUT:
        x, y, r = px(ms), py(wer), 7 + fill * 2.6
        if kept:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{INK}"/>')
        else:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{PAPER}" fill-opacity="0.7" stroke="{MUTED}" stroke-width="1.5"/>')
        dx, dy, anchor = {"right": (r + 10, 5, "start"), "left": (-r - 10, 5, "end"),
                          "right-low": (r + 10, 20, "start"), "left-high": (-r - 6, -12, "end")}[side]
        cls = 'class="serif" font-size="17" font-weight="700"' if kept else 'class="muted" font-size="14"'
        parts.append(f'<text {cls} x="{x + dx:.1f}" y="{y + dy:.1f}" text-anchor="{anchor}">{name}'
                     f'<tspan class="muted" font-family="{SANS}" font-size="13" font-weight="400"> {wer}%</tspan></text>')
    legend = (f'<circle cx="760" cy="96" r="7" fill="{INK}"/><text class="muted" x="774" y="101" font-size="13">kept in the default config</text>'
              f'<circle cx="958" cy="96" r="7" fill="{PAPER}" stroke="{MUTED}" stroke-width="1.5"/><text class="muted" x="972" y="101" font-size="13">dropped after testing</text>'
              f'<circle cx="1128" cy="96" r="12" fill="none" stroke="{MUTED}" stroke-width="1.5"/><text class="muted" x="1146" y="101" font-size="13">more fillers</text>')
    body = f"""
  {heading("Seven models on the same recordings", "Word error rate against median latency, all through OpenRouter.")}
  {legend}
  {''.join(parts)}
"""
    return frame(1280, 510, "Model shootout results",
                 "Word error rate against median latency for seven speech-to-text models on the same seven recordings. "
                 "AssemblyAI had 1.9 percent at 638 ms, MAI-Transcribe 2 had 2.5 percent and kept no filler words, "
                 "GPT Transcribe had 3.2 percent, and Gemini 3.5 was slowest at 12.1 percent.", body)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in (("banner", banner), ("pipeline", pipeline), ("speed", speed), ("shootout", shootout)):
        (OUT / f"{name}.svg").write_text(fn(), encoding="utf-8")
        print(f"wrote docs/assets/{name}.svg")
