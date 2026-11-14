"""Small text helpers shared by the console and the app window."""

from __future__ import annotations

import time


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def time_ago(timestamp: float | None) -> str:
    if not timestamp:
        return "unknown"
    seconds = max(0, time.time() - timestamp)
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} min ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} h ago"
    days = hours / 24
    if days < 2:
        return "yesterday"
    if days < 31:
        return f"{int(days)} days ago"
    if days < 365:
        months = int(days / 30.4)
        return f"{months} month{'s' if months > 1 else ''} ago"
    return time.strftime("%b %Y", time.localtime(timestamp))
