"""Erzeugt die Icons für das Stream-Deck-Plugin (Tasten 72/144 px, Listen-Icons 20/40 px)."""
import math, os, sys
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'com.ximjxstinn.streammarathon.sdPlugin', 'imgs')
os.makedirs(OUT, exist_ok=True)
BG = {'time': ('#1b1033', '#3a1d7a'), 'toggle': ('#0f2a1f', '#12804f'), 'add': ('#0f2a1f', '#12804f'), 'sub': ('#2a0f15', '#9b2335'),
      'hh': ('#3a0d33', '#c2187a'), 'panic': ('#2a0f15', '#c0172f'), 'pause': ('#2a2410', '#b07d10'), 'profile': ('#10223a', '#1f5fae'),
      'wheel': ('#1b1033', '#6a3dff')}

def grad(size, a, b):
    im = Image.new('RGB', (size, size), a); px = im.load()
    ca = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5)); cb = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))
    return im

def glyph(d, name, S, col='white'):
    c = S / 2; u = S / 144
    W = lambda v: max(1, int(v * u))
    if name in ('toggle', 'a-toggle'):
        d.polygon([(c - 34 * u, c - 30 * u), (c - 34 * u, c + 30 * u), (c + 4 * u, c)], fill=col)
        d.rectangle([c + 12 * u, c - 28 * u, c + 20 * u, c + 28 * u], fill=col); d.rectangle([c + 28 * u, c - 28 * u, c + 36 * u, c + 28 * u], fill=col)
    elif name == 'add':
        d.rectangle([c - 30 * u, c - 6 * u, c + 30 * u, c + 6 * u], fill=col); d.rectangle([c - 6 * u, c - 30 * u, c + 6 * u, c + 30 * u], fill=col)
    elif name == 'sub':
        d.rectangle([c - 30 * u, c - 6 * u, c + 30 * u, c + 6 * u], fill=col)
    elif name == 'hh':
        d.polygon([(c + 8 * u, c - 42 * u), (c - 26 * u, c + 6 * u), (c - 2 * u, c + 6 * u), (c - 10 * u, c + 42 * u), (c + 26 * u, c - 8 * u), (c + 2 * u, c - 8 * u)], fill=col)
    elif name == 'panic':
        d.ellipse([c - 36 * u, c - 22 * u, c + 36 * u, c + 22 * u], outline=col, width=W(7)); d.ellipse([c - 11 * u, c - 11 * u, c + 11 * u, c + 11 * u], fill=col)
        d.line([c - 40 * u, c + 34 * u, c + 40 * u, c - 34 * u], fill=col, width=W(9))
    elif name == 'pause':
        d.rectangle([c - 30 * u, c - 6 * u, c + 22 * u, c + 34 * u], outline=col, width=W(7))
        d.arc([c + 12 * u, c + 2 * u, c + 40 * u, c + 26 * u], -90, 90, fill=col, width=W(7))
        for k in (-16, 0, 16): d.arc([c + k * u - 6 * u, c - 38 * u, c + k * u + 6 * u, c - 14 * u], 180, 360, fill=col, width=W(4))
    elif name == 'profile':
        d.ellipse([c - 16 * u, c - 38 * u, c + 16 * u, c - 6 * u], fill=col); d.pieslice([c - 34 * u, c - 2 * u, c + 34 * u, c + 62 * u], 180, 360, fill=col)
    elif name == 'wheel':
        r = 38 * u
        for i in range(8):
            d.pieslice([c - r, c - r, c + r, c + r], i * 45, i * 45 + 45, fill=col if i % 2 == 0 else None, outline=col, width=W(3))
        d.polygon([(c - 9 * u, c - r - 12 * u), (c + 9 * u, c - r - 12 * u), (c, c - r + 6 * u)], fill='#ffe14d')
    elif name == 'time':
        d.ellipse([c - 40 * u, c - 34 * u, c + 40 * u, c + 46 * u], outline=col, width=W(6))
        d.rectangle([c - 10 * u, c - 50 * u, c + 10 * u, c - 40 * u], fill=col)
        d.line([c, c + 6 * u, c, c - 18 * u], fill=col, width=W(6)); d.line([c, c + 6 * u, c + 18 * u, c + 18 * u], fill=col, width=W(6))

def key(name, S):
    a, b = BG[name]
    im = grad(S, a, b); d = ImageDraw.Draw(im)
    # Bei Tasten mit Titel unten das Symbol etwas nach oben setzen
    sub = Image.new('RGBA', (S, S), (0, 0, 0, 0)); sd = ImageDraw.Draw(sub)
    glyph(sd, name, S)
    if name != 'time':
        sub = sub.resize((int(S * .72), int(S * .72)), Image.LANCZOS)
        im.paste(sub, ((S - sub.width) // 2, int(S * .08)), sub)
    else:
        sub = sub.resize((int(S * .5), int(S * .5)), Image.LANCZOS)
        im.paste(sub, ((S - sub.width) // 2, int(S * .04)), sub)
    return im

def icon(name, S, col='white'):
    im = Image.new('RGBA', (S * 4, S * 4), (0, 0, 0, 0)); glyph(ImageDraw.Draw(im), name, S * 4, col)
    return im.resize((S, S), Image.LANCZOS)

for n in BG:
    key(n, 72).save(os.path.join(OUT, f'k-{n}.png')); key(n, 144).save(os.path.join(OUT, f'k-{n}@2x.png'))
    icon(n, 20).save(os.path.join(OUT, f'a-{n}.png')); icon(n, 40).save(os.path.join(OUT, f'a-{n}@2x.png'))
# Kategorie- und Plugin-Icon: Stoppuhr auf Lila
for S, suf in ((28, ''), (56, '@2x')): icon('time', S).save(os.path.join(OUT, f'category{suf}.png'))
for S, suf in ((72, ''), (144, '@2x'), (288, '@4x')):
    im = grad(S, '#9146ff', '#ff4fd8'); sub = icon('time', int(S * .7)); im.paste(sub, ((S - sub.width) // 2, (S - sub.height) // 2), sub)
    im.save(os.path.join(OUT, f'plugin{suf}.png'))
print('Icons erzeugt:', len(os.listdir(OUT)))
