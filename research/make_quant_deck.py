"""Quant analysis deck: 5 landscape slides (960x540pt)."""

import json
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

ACCENT = colors.HexColor('#1e7693')
TEXT_PRIMARY = colors.HexColor('#1f2022')
TEXT_MUTED = colors.HexColor('#6e7479')
BG_SURFACE = colors.HexColor('#dde0e3')
BG_PAGE = colors.HexColor('#ebecee')

FDIR = "/System/Library/Fonts/Supplemental/"
pdfmetrics.registerFont(TTFont('Times New Roman', FDIR + 'Times New Roman.ttf'))
pdfmetrics.registerFont(TTFont('Times New Roman Bold', FDIR + 'Times New Roman Bold.ttf'))
registerFontFamily('Times New Roman', normal='Times New Roman', bold='Times New Roman Bold')

OUT = "/Users/paretoc4p/.zcode/workspace/default/London_Temperature_Quant_Deck.pdf"
W, H = 960, 540
M = 42  # margin

doc = SimpleDocTemplate(OUT, pagesize=landscape((W, H)),
                        leftMargin=M, rightMargin=M, topMargin=34, bottomMargin=30,
                        title="London Temperature Markets - Quantitative Analysis",
                        author="Z.ai", creator="Z.ai",
                        subject="Quantitative analysis of the model-band strategy")
AW = W - 2 * M

kicker = ParagraphStyle('Kicker', fontName='Times New Roman', fontSize=10,
                        leading=13, textColor=ACCENT, spaceAfter=3)
title_s = ParagraphStyle('TitleS', fontName='Times New Roman', fontSize=30,
                         leading=34, textColor=TEXT_PRIMARY, spaceAfter=4)
sub_s = ParagraphStyle('SubS', fontName='Times New Roman', fontSize=12.5,
                       leading=16, textColor=TEXT_MUTED, spaceAfter=2)
h_s = ParagraphStyle('HS', fontName='Times New Roman', fontSize=17, leading=21,
                     textColor=ACCENT, spaceAfter=5)
body = ParagraphStyle('Body', fontName='Times New Roman', fontSize=11.5,
                      leading=16, textColor=TEXT_PRIMARY, spaceAfter=5)
bullet = ParagraphStyle('Bullet', fontName='Times New Roman', fontSize=11.5,
                        leading=16, textColor=TEXT_PRIMARY, leftIndent=12, spaceAfter=4)
stat_big = ParagraphStyle('StatBig', fontName='Times New Roman', fontSize=19,
                          leading=22, textColor=ACCENT, alignment=TA_CENTER)
stat_lab = ParagraphStyle('StatLab', fontName='Times New Roman', fontSize=8.5,
                          leading=11, textColor=TEXT_MUTED, alignment=TA_CENTER)
th = ParagraphStyle('TH', fontName='Times New Roman', fontSize=10, leading=12.5,
                    textColor=colors.white)
tc = ParagraphStyle('TC', fontName='Times New Roman', fontSize=10, leading=13,
                    textColor=TEXT_PRIMARY)

def footer(canvas, doc_):
    canvas.saveState()
    canvas.setStrokeColor(BG_SURFACE); canvas.setLineWidth(0.7)
    canvas.line(M, 26, W - M, 26)
    canvas.setFont('Times New Roman', 8); canvas.setFillColor(TEXT_MUTED)
    canvas.drawString(M, 15, "London temperature markets · quantitative analysis · 23 Aug 2026")
    canvas.drawRightString(W - M, 15, "slide %d / 7" % doc_.page)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, H - 8, W, 8, stroke=0, fill=1)
    canvas.restoreState()

def img(path, width):
    ir = ImageReader(path)
    w0, h0 = ir.getSize()
    return Image(path, width=width, height=width * h0 / w0)

story = []
D = json.load(open("quant_deck_stats.json"))["stats"]

# ── Slide 1: title ──
story.append(Spacer(1, 30))
story.append(Paragraph('QUANTITATIVE ANALYSIS · POLYMARKET WEATHER MARKETS', kicker))
story.append(Paragraph('<b>The London Temperature Edge</b>', title_s))
story.append(Paragraph('Model-band strategy on daily London temperature events · backtested on 707 events, Jan 2025 - Aug 2026', sub_s))
story.append(Spacer(1, 26))
def callout(num, lab, w):
    t = Table([[Paragraph('<b>%s</b>' % num, stat_big)], [Paragraph(lab, stat_lab)]],
              colWidths=[w])
    t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), BG_PAGE),
                           ('BOX', (0, 0), (-1, -1), 0.8, ACCENT),
                           ('TOPPADDING', (0, 0), (0, 0), 9), ('BOTTOMPADDING', (0, 0), (0, 0), 1),
                           ('TOPPADDING', (0, 1), (0, 1), 1), ('BOTTOMPADDING', (0, 1), (0, 1), 9)]))
    return t
cw = AW / 4.0 - 12
row = Table([[callout('60%', 'win rate, 490 backtested events', cw),
              callout('+$1.09 / $1', 'realistic day-before entry, net of the 10-50¢ filter', cw),
              callout('0.47° vs 1.09°C', 'model vs market error on the daily high', cw),
              callout('96%', 'of resolutions exactly matched by our METAR ground truth', cw)]],
            colWidths=[AW / 4.0] * 4, hAlign='CENTER')
row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                         ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6)]))
story.append(row)
story.append(Spacer(1, 22))
story.append(Paragraph('Data: 19 months of trades, prices, orderbooks and airport observations, collected nightly by our own free automation.', sub_s))
story.append(PageBreak())


# ── Slide 2: thesis ──
story.append(Paragraph('THE THESIS', kicker))
story.append(Paragraph('<b>Recap: what we believe and why</b>', h_s))
story.append(Paragraph(
    'Polymarket’s daily London temperature markets settle on a public, free source of truth — the '
    'Met Office observations at London City airport — yet their opening prices systematically lag the '
    'free official forecast by half a degree to a full degree. That lag is measurable, persistent across '
    '19 months and both market eras, and large enough that simply buying the forecast’s band would have '
    'returned roughly one dollar per dollar staked after realistic entry timing. We therefore propose a '
    'deliberately boring strategy — one trade per day, sized small, held to resolution — governed by '
    'measured rules for when to size up and when to stop entirely.', body))
story.append(Spacer(1, 6))
tpill = [
    ('The edge', 'Free Met Office forecast beats opening prices 66% head-to-head (0.47°C vs 1.09°C error)'),
    ('The mechanism', 'The market reprices slowly: win rate stays flat as the correct band’s price climbs 30¢ → 50¢'),
    ('The discipline', 'Spread-regime sizing, 45% kill switch, £50 pilot stakes, scale only on evidence'),
]
cells = []
for hd, tx in tpill:
    inner = Table([[Paragraph('<b>%s</b>' % hd, ParagraphStyle('ph', fontName='Times New Roman', fontSize=13, leading=16, textColor=ACCENT))],
                   [Paragraph(tx, ParagraphStyle('pt', fontName='Times New Roman', fontSize=10.5, leading=14.5, textColor=TEXT_PRIMARY))]],
                  colWidths=[AW / 3.0 - 16])
    inner.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), BG_PAGE),
                               ('BOX', (0, 0), (-1, -1), 0.8, ACCENT),
                               ('TOPPADDING', (0, 0), (-1, 0), 8), ('BOTTOMPADDING', (0, -1), (-1, -1), 9),
                               ('LEFTPADDING', (0, 0), (-1, -1), 9), ('RIGHTPADDING', (0, 0), (-1, -1), 9)]))
    cells.append(inner)
trow = Table([cells], colWidths=[AW / 3.0] * 3, hAlign='CENTER')
trow.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                          ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6)]))
story.append(trow)
story.append(Spacer(1, 10))
story.append(Paragraph('Every claim above is measured in our own 707-event dataset and re-verified nightly by automated collection.', sub_s))
story.append(PageBreak())

# ── Slide 2: market structure ──
story.append(Paragraph('MARKET STRUCTURE', kicker))
story.append(Paragraph('<b>What we are trading</b>', h_s))
left = [
    Paragraph('•  <b>Instrument</b>: daily binary events - "highest temperature in London" - 11 integer °C bands, mutually exclusive, resolving on London City airport (EGLC) METAR observations.', bullet),
    Paragraph('•  <b>Lifecycle</b>: events open ~2 days ahead; 61% of volume trades on event day, 28% the day before.', bullet),
    Paragraph('•  <b>Liquidity</b>: thin - median 1¢ spread, only ~$17k open interest per event, 72% held by the top-10 wallets.', bullet),
    Paragraph('•  <b>Why it matters</b>: thin books mean a modest, disciplined trader can always get filled without moving the price - and can dominate quoting later if we choose to.', bullet),
]
right_rows = [
    ['Structural metric', 'Value'],
    ['Events analysed (2025-2026)', '707'],
    ['Total traded volume', '~$90M'],
    ['Median volume per event-day', '$80k'],
    ['Trades per event (taker feed)', '~500-1,000'],
    ['Unique wallets per event', '~1,000'],
    ['Volume on event day', '61%'],
]
rt = [[Paragraph('<b>%s</b>' % c, th), Paragraph('<b>%s</b>' % r, th)] for c, r in [right_rows[0]]] + \
     [[Paragraph(a, tc), Paragraph(b, tc)] for a, b in right_rows[1:]]
t = Table(rt, colWidths=[0.62 * AW * 0.48, 0.38 * AW * 0.48], hAlign='CENTER')
sc = [('BACKGROUND', (0, 0), (-1, 0), ACCENT), ('GRID', (0, 0), (-1, -1), 0.4, TEXT_MUTED),
      ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
      ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
      ('LEFTPADDING', (0, 0), (-1, -1), 7)]
for i in range(1, len(rt)):
    sc.append(('BACKGROUND', (0, i), (-1, i), colors.white if i % 2 else BG_PAGE))
t.setStyle(TableStyle(sc))
two = Table([[left, t]], colWidths=[0.54 * AW, 0.46 * AW])
two.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                         ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
story.append(two)
story.append(PageBreak())

# ── Slide 3: information edge ──
story.append(Paragraph('THE INFORMATION EDGE', kicker))
story.append(Paragraph('<b>The best free forecast beats the opening price</b>', h_s))
stats3 = [
    Paragraph('•  Met Office model error on the daily high: <b>0.47°C</b>; the market’s opening implied temperature: <b>1.09°C</b>.', bullet),
    Paragraph('•  Head-to-head on the same events, the model was closer <b>66%</b> of the time (395 vs 149).', bullet),
    Paragraph('•  The market also carries a systematic <b>cold bias of -0.2°C</b> at open - it under-forecasts London temperatures.', bullet),
    Paragraph('•  Verified ground truth: 96% of all market resolutions match our independent METAR reconstruction - the edge is real mispricing, not data noise.', bullet),
    Paragraph('•  The signal is free, keyless and served as-is by Open-Meteo: no data bill, no decay of access.', bullet),
]
c3 = Table([[img("chart_accuracy.png", 4.4 * 72), stats3]],
           colWidths=[0.46 * AW, 0.54 * AW])
c3.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('LEFTPADDING', (0, 0), (0, 0), 0),
                        ('RIGHTPADDING', (1, 0), (1, 0), 0)]))
story.append(c3)
story.append(PageBreak())

# ── Slide 4: backtest ──
story.append(Paragraph('BACKTEST · MODEL-BAND STRATEGY', kicker))
story.append(Paragraph('<b>Buy the forecast band, hold to resolution</b>', h_s))
story.append(img("chart_equity.png", 6.7 * 72))
strip_items = [
    ('n events', '374'), ('win rate', '61%'),
    ('profit / $1', '+$1.09'), ('total at $50/event', '+$408'),
    ('max drawdown', '$355'), ('per-event Sharpe', '0.52'),
    ('rolling 50-event win rate', '44-76%'),
]
cells = []
for num, lab in [(v, k) for k, v in strip_items]:
    cells.append(callout(num, lab, AW / 7.0 - 8))
r4 = Table([cells], colWidths=[AW / 7.0] * 7, hAlign='CENTER')
r4.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4)]))
story.append(Spacer(1, 8))
story.append(r4)
story.append(Paragraph('Filtered entries (10-50¢), realistic day-before timing. Control: buying the market’s own favorite at open loses money (-$0.15/$1).', sub_s))
story.append(PageBreak())


# ── Slide 6: monte carlo ──
story.append(Paragraph('MONTE CARLO PROJECTION', kicker))
story.append(Paragraph('<b>$50 per trade for 6 weeks, then $500</b>', h_s))
story.append(img("chart_mc.png", 7.5 * 72))
story.append(Spacer(1, 6))
mcs = json.load(open("mc_results.json"))
mc_items = [
    ("median, full edge", "+$%s" % format(mcs["full"]["med"], ",.0f")),
    ("median, edge decays to zero", "+$%s" % format(mcs["decay"]["med"], ",.0f")),
    ("median, half edge", "+$%s" % format(mcs["half"]["med"], ",.0f")),
    ("5th percentile (full)", "+$%s" % format(mcs["full"]["p5"], ",.0f")),
    ("probability of loss", "%.0f%%" % (mcs["full"]["p_loss"] * 100)),
    ("median max drawdown", "$%s" % format(mcs["full"]["med_dd"], ",.0f")),
]
cells = [callout(num, lab, AW / 6.0 - 8) for num, lab in [(v, k) for k, v in mc_items]]
r6 = Table([cells], colWidths=[AW / 6.0] * 6, hAlign='CENTER')
r6.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4)]))
story.append(r6)
story.append(Spacer(1, 5))
story.append(Paragraph('10,000 bootstrap paths resampled from the 374 empirical trade results; kill switch modeled as halt-investigate-resume (~9% of time paused). '
                       'Assumes the historical edge persists with no market impact — the projection is a ceiling, and the kill switch is the protection when reality is worse.', sub_s))
story.append(PageBreak())

# ── Slide 7: risk & deployment ──
story.append(Paragraph('RISK & DEPLOYMENT', kicker))
story.append(Paragraph('<b>When to trade, when to stop</b>', h_s))
c5a = Table([[img("chart_timing.png", 3.35 * 72), img("chart_spread.png", 3.35 * 72),
              [Paragraph('•  <b>Sizing</b>: flat £50 stake; full size only when models mildly disagree (0.5-1.0°C); cut to quarter above 1.5°C.', bullet),
               Paragraph('•  <b>Kill switch</b>: trailing 30-event win rate below 45% halts trading - the backtest brushed 44% once, never broke below.', bullet),
               Paragraph('•  <b>Bankroll</b>: stake capped at 1/20 of capital; worst observed streak is 6 straight losses.', bullet),
               Paragraph('•  <b>Pilot</b>: £50/event for 4-6 weeks (40+ events). Go bar: +$0.80/$1 realised. Clear it and scale in doubled steps; miss it and stop.', bullet),
               Paragraph('•  <b>Known risks</b>: fills assumed near last trade; edge decays if competitors arrive; 4 of 10 days lose by design.', bullet)]]],
            colWidths=[0.27 * AW, 0.27 * AW, 0.46 * AW])
c5a.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                         ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 6)]))
story.append(c5a)

doc.build(story, onFirstPage=footer, onLaterPages=footer)
print('built', OUT)
