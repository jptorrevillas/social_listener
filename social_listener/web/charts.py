"""Inline SVG chart builders.

Server-rendered SVG rather than a charting library: no external script, no CDN,
no build step, and the markup is identical in every browser. Each chart ships a
legend when it has two or more series, a <title> on every mark for the hover
layer, and a table view alongside for the accessibility path.
"""

from __future__ import annotations

from html import escape

SENTIMENT_ORDER = ("negative", "neutral", "positive")
SENTIMENT_TOKEN = {
    "negative": "var(--negative)",
    "neutral": "var(--neutral)",
    "positive": "var(--positive)",
}
# A 2px surface gap between stacked segments, per the mark spec.
SEGMENT_GAP = 2


def stacked_volume(series: list[dict], width: int = 840, height: int = 230) -> str:
    """Daily mention volume, stacked by sentiment.

    Sentiment is polarity, so the colours are the diverging pair with a neutral
    gray midpoint -- not three categorical hues.
    """
    if not series:
        return '<p class="empty">No data yet.</p>'

    pad_left, pad_right, pad_top, pad_bottom = 34, 8, 12, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    peak = max((point["total"] for point in series), default=0) or 1

    # Round the axis up to something legible.
    ceiling = peak if peak % 2 == 0 else peak + 1
    slot = plot_w / len(series)
    bar_w = max(min(slot - 3, 26), 3)

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="Daily mention volume by sentiment">'
    ]

    # Recessive gridlines and axis labels.
    for tick in range(0, 5):
        value = ceiling * tick / 4
        y = pad_top + plot_h - (value / ceiling) * plot_h
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}" '
            f'stroke="var(--grid)" stroke-width="1" />'
        )
        parts.append(
            f'<text x="{pad_left - 7}" y="{y + 3.5:.1f}" text-anchor="end" class="value">'
            f"{value:.0f}</text>"
        )

    for index, point in enumerate(series):
        x = pad_left + index * slot + (slot - bar_w) / 2
        cursor = pad_top + plot_h
        for sentiment in SENTIMENT_ORDER:
            count = point.get(sentiment, 0)
            if not count:
                continue
            seg_h = (count / ceiling) * plot_h
            drawn = max(seg_h - SEGMENT_GAP, 1.0)
            cursor -= seg_h
            parts.append(
                f'<rect class="bar-seg" x="{x:.1f}" y="{cursor:.1f}" width="{bar_w:.1f}" '
                f'height="{drawn:.1f}" rx="3" fill="{SENTIMENT_TOKEN[sentiment]}">'
                f"<title>{escape(point['date'])}: {count} {sentiment}</title></rect>"
            )

        # Date labels every fourth day, so they never collide.
        if index % 4 == 0:
            label = point["date"][5:]
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" text-anchor="middle">'
                f"{label}</text>"
            )

    baseline_y = pad_top + plot_h
    parts.append(
        f'<line x1="{pad_left}" y1="{baseline_y}" x2="{width - pad_right}" y2="{baseline_y}" '
        f'stroke="var(--axis)" stroke-width="1" />'
    )
    parts.append("</svg>")
    return "".join(parts)


def hbars(rows: list[dict], label_key: str, value_key: str, negative_key: str | None = None) -> str:
    """Horizontal bars for a single measure. One series, so no legend."""
    if not rows:
        return '<p class="empty">Nothing to show yet.</p>'
    peak = max(row[value_key] for row in rows) or 1
    out: list[str] = []
    for row in rows:
        width_pct = row[value_key] / peak * 100
        mostly_negative = (
            negative_key
            and row.get(negative_key, 0) / max(row[value_key], 1) >= 0.5
        )
        fill_class = "hbar-fill neg" if mostly_negative else "hbar-fill"
        suffix = ""
        if negative_key:
            suffix = f" <span style=\"color:var(--text-muted)\">({row[negative_key]} neg)</span>"
        out.append(
            f'<div class="hbar-row">'
            f'<div class="hbar-label">{escape(str(row[label_key]))}</div>'
            f'<div class="hbar-track"><div class="{fill_class}" style="width:{width_pct:.1f}%"></div></div>'
            f'<div class="hbar-value">{row[value_key]}{suffix}</div>'
            f"</div>"
        )
    return "".join(out)
