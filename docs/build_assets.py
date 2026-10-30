"""Generate the README's SVG graphics into docs/assets/.

The graphics are plain SVG with CSS and SMIL animation, so GitHub renders and animates them
inside the README. Edit the data below and re-run:

    .venv\\Scripts\\python.exe docs\\build_assets.py
"""
import math
from pathlib import Path

OUT = Path(__file__).resolve().parent / "assets"

SANS = "'Segoe UI', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif"
MONO = "'Cascadia Code', SFMono-Regular, Consolas, Menlo, monospace"

BG0, BG1 = "#0A0F1F", "#151B38"
CARD, EDGE = "#121933", "#2A3463"
TEXT, MUTED, FAINT = "#F1F5F9", "#94A3B8", "#475569"
VIOLET, CYAN, GREEN, AMBER = "#A78BFA", "#22D3EE", "#34D399", "#F59E0B"

# AssemblyAI latency (ms), median of 3 runs: before, FLAC upload via OpenRouter, native + FLAC
SPEED = [
    ("Short command", "2 to 4 s of audio", 640, 340, 200),
    ("Typical dictation", "12 s of audio", 969, 636, 551),
    ("Long paragraph", "55 s of audio", 3254, 1631, 1371),
]

# Shootout via OpenRouter: name, median latency ms, WER %, fillers kept, kept in config, label placement
SHOOTOUT = [
    ("AssemblyAI", 638, 1.9, 1, True, "right"),
    ("MAI-Transcribe 2", 1042, 2.5, 0, True, "right-low"),
    ("GPT Transcribe", 964, 3.2, 0, True, "left-high"),
    ("Voxtral Mini", 630, 5.1, 5, False, "left"),
    ("Voxtral Small", 1095, 5.7, 3, False, "right"),
    ("Scribe v2", 1017, 5.7, 0, False, "left-high"),
    ("Gemini 3.5", 2897, 12.1, 4, False, "left"),
]


def frame(w, h, title, desc, body, extra_defs="", style=""):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-labelledby="title desc">
<title id="title">{title}</title>
<desc id="desc">{desc}</desc>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{BG0}"/><stop offset="1" stop-color="{BG1}"/></linearGradient>
  <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{VIOLET}"/><stop offset="1" stop-color="{CYAN}"/></linearGradient>
  <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse"><path d="M32 0H0V32" fill="none" stroke="#FFFFFF" stroke-opacity="0.035"/></pattern>
  <radialGradient id="glowV" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#7C3AED" stop-opacity="0.32"/><stop offset="1" stop-color="#7C3AED" stop-opacity="0"/></radialGradient>
  <radialGradient id="glowC" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#0891B2" stop-opacity="0.26"/><stop offset="1" stop-color="#0891B2" stop-opacity="0"/></radialGradient>
  <clipPath id="round"><rect width="{w}" height="{h}" rx="22"/></clipPath>
  {extra_defs}
</defs>
<style>
  text {{ font-family: {SANS}; }}
  .mono {{ font-family: {MONO}; }}
  @media (prefers-reduced-motion: reduce) {{ * {{ animation: none !important; }} }}
  {style}
</style>
<g clip-path="url(#round)">
  <rect width="{w}" height="{h}" fill="url(#bg)"/>
  <rect width="{w}" height="{h}" fill="url(#grid)"/>
  {body}
</g>
<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="22" fill="none" stroke="{EDGE}"/>
</svg>
"""


def pill(x, y, text, color):
    w = len(text) * 8.4 + 34
    return (f'<g transform="translate({x:.0f} {y})"><rect width="{w:.0f}" height="34" rx="17" fill="{color}" fill-opacity="0.12" '
            f'stroke="{color}" stroke-opacity="0.45"/><circle cx="17" cy="17" r="4" fill="{color}"/>'
            f'<text class="mono" x="29" y="22" font-size="14.5" fill="{TEXT}">{text}</text></g>'), w


def banner():
    bars = []
    n, x0, step = 34, 70, 13
    for i in range(n):
        env = math.sin(math.pi * (i + 0.5) / n) ** 0.8
        h = 26 + 150 * env * (0.55 + 0.45 * abs(math.sin(i * 1.7)))
        x = x0 + i * step
        bars.append(f'<rect class="bar" x="{x}" y="{210 - h / 2:.1f}" width="7" height="{h:.1f}" rx="3.5" '
                    f'fill="url(#wave)" style="animation-delay:{-(i * 0.09) % 1.6:.2f}s"/>')
    pills, x = [], 560
    for text, color in (("~200 ms commands", GREEN), ("up to 3.2x faster", CYAN), ("127.0.0.1 only", VIOLET)):
        p, w = pill(x, 330, text, color)
        pills.append(p)
        x += w + 12
    body = f"""
  <ellipse cx="290" cy="210" rx="330" ry="230" fill="url(#glowV)"/>
  <ellipse cx="1120" cy="80" rx="380" ry="220" fill="url(#glowC)"/>
  <g>{''.join(bars)}</g>
  <line x1="508" y1="120" x2="508" y2="300" stroke="{EDGE}" stroke-width="1.5"/>
  <text x="560" y="128" font-size="14" font-weight="600" letter-spacing="3.2" fill="#A5B4FC">LOCAL SPEECH-TO-TEXT PROXY FOR SPOKENLY</text>
  <text x="556" y="196" font-size="64" font-weight="700" fill="{TEXT}">Spokenly BYOK</text>
  <text x="556" y="264" font-size="64" font-weight="700" fill="url(#accent)">API Upgrade<tspan class="cursor" fill="{CYAN}" font-weight="300">|</tspan></text>
  <text x="560" y="304" font-size="20" fill="{MUTED}">Clean, fast dictation through your own API keys.</text>
  {''.join(pills)}
"""
    defs = (f'<linearGradient id="wave" gradientUnits="userSpaceOnUse" x1="70" y1="0" x2="500" y2="0">'
            f'<stop offset="0" stop-color="{VIOLET}"/><stop offset="1" stop-color="{CYAN}"/></linearGradient>')
    style = """
  .bar { transform-box: fill-box; transform-origin: center; animation: talk 1.6s ease-in-out infinite; }
  @keyframes talk { 0%, 100% { transform: scaleY(0.35); } 50% { transform: scaleY(1); } }
  .cursor { animation: blink 1.1s steps(1) infinite; }
  @keyframes blink { 50% { opacity: 0; } }
"""
    return frame(1280, 420, "Spokenly BYOK API Upgrade",
                 "A local speech-to-text proxy for Spokenly. Short commands come back in about 200 ms, "
                 "up to 3.2 times faster than before, and the proxy listens on 127.0.0.1 only.",
                 body, defs, style)


def card(x, y, w, h, accent=None, dashed=False):
    stroke = accent or EDGE
    dash = ' stroke-dasharray="6 6"' if dashed else ""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="16" fill="{CARD}" fill-opacity="0.92" '
            f'stroke="{stroke}" stroke-opacity="{0.8 if accent else 1}" stroke-width="1.5"{dash}/>')


def pipeline():
    def dot(path_id, color, begin, reverse=False, dur=2.6):
        keys = ' keyPoints="1;0" keyTimes="0;1" calcMode="linear"' if reverse else ""
        return (f'<circle r="5.5" fill="{color}" filter="url(#soft)"><animateMotion dur="{dur}s" begin="{begin}s" '
                f'repeatCount="indefinite"{keys}><mpath href="#{path_id}"/></animateMotion></circle>')

    stages = [
        ("1", "Route", "the model name picks", "a route in config.yaml", VIOLET),
        ("2", "FLAC", "lossless re-encode", "45% of the size", "#818CF8"),
        ("3", "Translate", "provider options", "and your vocabulary", CYAN),
    ]
    chips = []
    for i, (num, title, l1, l2, color) in enumerate(stages):
        x = 352 + i * 172
        chips.append(f"""
    {card(x, 186, 150, 132, color)}
    <circle cx="{x + 26}" cy="214" r="13" fill="{color}" fill-opacity="0.18" stroke="{color}"/>
    <text x="{x + 26}" y="219" text-anchor="middle" font-size="13" font-weight="700" fill="{color}">{num}</text>
    <text x="{x + 48}" y="219" font-size="17" font-weight="700" fill="{TEXT}">{title}</text>
    <text x="{x + 16}" y="262" font-size="13" fill="{MUTED}">{l1}</text>
    <text x="{x + 16}" y="282" font-size="13" fill="{MUTED}">{l2}</text>""")
        if i < 2:
            chips.append(f'<path d="M{x + 156} 252 l10 0 m-5 -5 l5 5 l-5 5" fill="none" stroke="{FAINT}" stroke-width="2" stroke-linecap="round"/>')

    body = f"""
  <ellipse cx="600" cy="280" rx="420" ry="260" fill="url(#glowV)"/>
  <ellipse cx="1090" cy="270" rx="260" ry="240" fill="url(#glowC)"/>
  <text x="40" y="58" font-size="22" font-weight="700" fill="{TEXT}">How a dictation travels</text>
  <text x="40" y="84" font-size="14" fill="{MUTED}">One request, from push-to-talk to transcript. The dots show live traffic.</text>

  <path id="p-in" d="M240 252 H322" fill="none" stroke="{EDGE}" stroke-width="2" marker-end="url(#arrow)"/>
  <path id="p-or" d="M856 230 C 895 230, 895 168, 932 168" fill="none" stroke="{EDGE}" stroke-width="2" marker-end="url(#arrow)"/>
  <path id="p-nat" d="M856 290 C 895 290, 895 372, 932 372" fill="none" stroke="{EDGE}" stroke-width="2" marker-end="url(#arrow)"/>
  <path id="p-log" d="M590 420 V 466" fill="none" stroke="{EDGE}" stroke-width="2" stroke-dasharray="4 5"/>

  {card(40, 182, 200, 140)}
  <rect x="128" y="200" width="24" height="38" rx="12" fill="none" stroke="url(#accent)" stroke-width="3"/>
  <path d="M120 226 a20 20 0 0 0 40 0 M140 246 v10" fill="none" stroke="url(#accent)" stroke-width="3" stroke-linecap="round"/>
  <text x="140" y="290" text-anchor="middle" font-size="18" font-weight="700" fill="{TEXT}">Spokenly</text>
  <text x="140" y="310" text-anchor="middle" font-size="13" fill="{MUTED}">push-to-talk, sends WAV</text>

  {card(322, 120, 534, 300, VIOLET, dashed=True)}
  <text x="346" y="158" font-size="18" font-weight="700" fill="{TEXT}">Dictation proxy</text>
  <text class="mono" x="832" y="158" text-anchor="end" font-size="13" fill="#A5B4FC">127.0.0.1:8765</text>
  {''.join(chips)}
  <text class="mono" x="346" y="396" font-size="12.5" fill="{MUTED}">keep-warm · hot reload · toasts · 7-day retention</text>

  {card(932, 108, 308, 120)}
  <text x="956" y="146" font-size="18" font-weight="700" fill="{TEXT}">OpenRouter</text>
  <text x="956" y="172" font-size="13" fill="{MUTED}">MAI-Transcribe 2, GPT Transcribe</text>
  <rect x="956" y="186" width="152" height="24" rx="6" fill="{VIOLET}" fill-opacity="0.14"/>
  <text class="mono" x="966" y="203" font-size="12.5" fill="#C4B5FD">backend: openrouter</text>

  {card(932, 312, 308, 120, GREEN)}
  <text x="956" y="350" font-size="18" font-weight="700" fill="{TEXT}">Native API</text>
  <text x="956" y="376" font-size="13" fill="{MUTED}">AssemblyAI Sync, EU endpoint</text>
  <rect x="956" y="390" width="124" height="24" rx="6" fill="{GREEN}" fill-opacity="0.14"/>
  <text class="mono" x="966" y="407" font-size="12.5" fill="#6EE7B7">backend: native</text>

  <ellipse cx="590" cy="476" rx="96" ry="12" fill="{CARD}" stroke="{EDGE}" stroke-width="1.5"/>
  <path d="M494 476 V 512 a96 12 0 0 0 192 0 V 476" fill="{CARD}" stroke="{EDGE}" stroke-width="1.5"/>
  <ellipse cx="590" cy="476" rx="96" ry="12" fill="#18213F" stroke="{EDGE}" stroke-width="1.5"/>
  <text x="590" y="506" text-anchor="middle" font-size="14" font-weight="700" fill="{TEXT}">SQLite log</text>
  <text x="712" y="500" font-size="13" fill="{MUTED}">latency, cost and transcript for every request</text>

  {dot("p-in", VIOLET, 0)}
  {dot("p-or", VIOLET, 0.9)}
  {dot("p-nat", GREEN, 1.6)}
  {dot("p-or", CYAN, 2.2, reverse=True)}
  {dot("p-nat", GREEN, 0.4, reverse=True)}
  {dot("p-in", CYAN, 1.3, reverse=True)}
  {dot("p-log", MUTED, 0.7, dur=1.8)}
"""
    defs = (f'<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
            f'<path d="M0 0 L10 5 L0 10z" fill="{FAINT}"/></marker>'
            '<filter id="soft" x="-2" y="-2" width="5" height="5"><feGaussianBlur stdDeviation="1.6" result="b"/>'
            '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>')
    return frame(1280, 548, "How the proxy works",
                 "Spokenly sends a WAV recording to the local proxy. The proxy picks a route, re-encodes the audio "
                 "to FLAC, adds provider options and vocabulary, and sends it to OpenRouter or a native API. "
                 "The transcript goes back to Spokenly and every request is logged to SQLite.",
                 body, defs)


def speed():
    x0, maxw, scale_max = 280, 720, 3300
    colors = [(AMBER, "Before"), (VIOLET, "FLAC upload"), (GREEN, "Native and FLAC")]
    rows = []
    legend_x = 760
    for i, (c, label) in enumerate(colors):
        rows.append(f'<rect x="{legend_x}" y="52" width="14" height="14" rx="4" fill="{c}"/>'
                    f'<text x="{legend_x + 22}" y="64" font-size="14" fill="{MUTED}">{label}</text>')
        legend_x += 22 + len(label) * 7.6 + 26
    delay = 0.0
    for g, (name, sub, *vals) in enumerate(SPEED):
        y = 124 + g * 116
        rows.append(f'<text x="40" y="{y + 26}" font-size="17" font-weight="700" fill="{TEXT}">{name}</text>'
                    f'<text x="40" y="{y + 48}" font-size="13" fill="{MUTED}">{sub}</text>')
        for b, (v, (c, _)) in enumerate(zip(vals, colors)):
            w = maxw * v / scale_max
            by = y + b * 29
            rows.append(f'<rect class="grow" x="{x0}" y="{by}" width="{w:.1f}" height="21" rx="5" fill="{c}" '
                        f'fill-opacity="{1 if b == 2 else 0.78}" style="animation-delay:{delay:.2f}s"/>'
                        f'<text class="mono fade" x="{x0 + w + 10:.1f}" y="{by + 16}" font-size="13.5" fill="{TEXT}" '
                        f'style="animation-delay:{delay + 0.6:.2f}s">{v:,} ms</text>')
            delay += 0.12
        factor = vals[0] / vals[2]
        rows.append(f'<text x="1236" y="{y + 44}" text-anchor="end" font-size="40" font-weight="800" fill="url(#accent)">{factor:.1f}x</text>'
                    f'<text x="1236" y="{y + 66}" text-anchor="end" font-size="13" fill="{MUTED}">faster</text>')
        if g < len(SPEED) - 1:
            rows.append(f'<line x1="40" x2="1240" y1="{y + 102}" y2="{y + 102}" stroke="{EDGE}" stroke-opacity="0.6"/>')
    body = f"""
  <ellipse cx="1050" cy="120" rx="360" ry="200" fill="url(#glowC)"/>
  <text x="40" y="58" font-size="22" font-weight="700" fill="{TEXT}">AssemblyAI latency per dictation</text>
  <text x="40" y="84" font-size="14" fill="{MUTED}">Median of 3 runs on real recordings, October 2026. Lower is better.</text>
  {''.join(rows)}
"""
    style = """
  .grow { transform-box: fill-box; transform-origin: left center; animation: grow 1.1s cubic-bezier(.2,.8,.2,1) both; }
  @keyframes grow { from { transform: scaleX(0); } to { transform: scaleX(1); } }
  .fade { animation: fade 0.6s ease-out both; }
  @keyframes fade { from { opacity: 0; } to { opacity: 1; } }
"""
    return frame(1280, 470, "AssemblyAI latency before and after optimizing",
                 "Short commands went from 640 to 200 ms, a 12 second dictation from 969 to 551 ms, "
                 "and a 55 second paragraph from 3254 to 1371 ms.", body, style=style)


def shootout():
    L, R, T, B = 120, 1190, 116, 470
    xmax, ymax = 3000, 13
    px = lambda ms: L + ms / xmax * (R - L)
    py = lambda wer: B - wer / ymax * (B - T)
    parts = [f'<rect x="{L}" y="{py(4):.0f}" width="{px(1150) - L:.0f}" height="{B - py(4):.0f}" rx="12" '
             f'fill="{GREEN}" fill-opacity="0.07" stroke="{GREEN}" stroke-opacity="0.35" stroke-dasharray="5 6"/>',
             f'<text x="{L + 14}" y="{py(4) + 22:.0f}" font-size="13" font-weight="600" fill="#6EE7B7">fast and accurate</text>']
    for ms in range(0, xmax + 1, 500):
        parts.append(f'<line x1="{px(ms):.1f}" x2="{px(ms):.1f}" y1="{T}" y2="{B}" stroke="{EDGE}" stroke-opacity="0.55"/>'
                     f'<text class="mono" x="{px(ms):.1f}" y="{B + 22}" text-anchor="middle" font-size="12.5" fill="{MUTED}">{ms}</text>')
    for wer in range(0, ymax + 1, 2):
        parts.append(f'<line x1="{L}" x2="{R}" y1="{py(wer):.1f}" y2="{py(wer):.1f}" stroke="{EDGE}" stroke-opacity="0.55"/>'
                     f'<text class="mono" x="{L - 12}" y="{py(wer) + 4:.1f}" text-anchor="end" font-size="12.5" fill="{MUTED}">{wer}%</text>')
    parts.append(f'<text x="{(L + R) / 2:.0f}" y="{B + 48}" text-anchor="middle" font-size="14" fill="{MUTED}">median latency (ms)</text>'
                 f'<text transform="translate(52 {(T + B) / 2:.0f}) rotate(-90)" text-anchor="middle" font-size="14" fill="{MUTED}">word error rate</text>')
    for i, (name, ms, wer, fill, kept, place) in enumerate(SHOOTOUT):
        x, y, r = px(ms), py(wer), 9 + fill * 3
        color = "url(#bubble)" if kept else "#64748B"
        stroke = CYAN if kept else "#94A3B8"
        parts.append(f'<circle class="pop" cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" fill-opacity="{0.9 if kept else 0.45}" '
                     f'stroke="{stroke}" stroke-opacity="0.8" style="animation-delay:{0.15 * i:.2f}s"/>')
        if kept:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="none" stroke="{CYAN}" stroke-width="2">'
                         f'<animate attributeName="r" values="{r};{r + 14}" dur="2.4s" begin="{0.4 * i}s" repeatCount="indefinite"/>'
                         f'<animate attributeName="opacity" values="0.7;0" dur="2.4s" begin="{0.4 * i}s" repeatCount="indefinite"/></circle>')
        dx, dy, anchor = {"right": (r + 10, 5, "start"), "left": (-r - 10, 5, "end"),
                          "right-low": (r + 10, 18, "start"), "left-high": (-r - 6, -14, "end")}[place]
        parts.append(f'<text x="{x + dx:.1f}" y="{y + dy:.1f}" text-anchor="{anchor}" font-size="14" '
                     f'font-weight="{700 if kept else 500}" fill="{TEXT if kept else MUTED}">{name}'
                     f'<tspan class="mono" font-size="12" fill="{MUTED}" font-weight="400"> {wer}%</tspan></text>')
    legend = (f'<circle cx="790" cy="62" r="8" fill="url(#bubble)" stroke="{CYAN}"/>'
              f'<text x="806" y="67" font-size="14" fill="{MUTED}">in the default config</text>'
              f'<circle cx="980" cy="62" r="8" fill="#64748B" fill-opacity="0.45" stroke="#94A3B8"/>'
              f'<text x="996" y="67" font-size="14" fill="{MUTED}">dropped after testing</text>')
    body = f"""
  <ellipse cx="300" cy="420" rx="380" ry="220" fill="url(#glowV)"/>
  <text x="40" y="58" font-size="22" font-weight="700" fill="{TEXT}">Model shootout on real dictation</text>
  <text x="40" y="84" font-size="14" fill="{MUTED}">7 recorded clips through OpenRouter. Bigger bubbles kept more filler words.</text>
  {legend}
  {''.join(parts)}
"""
    defs = (f'<radialGradient id="bubble" cx="0.35" cy="0.35" r="0.75"><stop offset="0" stop-color="{CYAN}"/>'
            f'<stop offset="1" stop-color="{VIOLET}"/></radialGradient>')
    style = """
  .pop { transform-box: fill-box; transform-origin: center; animation: pop 0.7s cubic-bezier(.3,1.6,.5,1) both; }
  @keyframes pop { from { transform: scale(0); } to { transform: scale(1); } }
"""
    return frame(1280, 548, "Model shootout results",
                 "Word error rate against median latency for seven speech-to-text models. AssemblyAI had the "
                 "lowest error rate at 1.9 percent with 638 ms, MAI-Transcribe 2 had 2.5 percent and removed all "
                 "fillers, and Gemini 3.5 was slowest and least accurate.", body, defs, style)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in (("banner", banner), ("pipeline", pipeline), ("speed", speed), ("shootout", shootout)):
        (OUT / f"{name}.svg").write_text(fn(), encoding="utf-8")
        print(f"wrote docs/assets/{name}.svg")
