"""Recognition of daily puzzle-game share text.

Each game contributes one parser to `GAMES`. A parser reads a message body and
returns a `ParsedResult`, or `None` when the body is not that game's share text.
Adding a game is a function plus an entry in the registry.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

# Telegram delivers share text verbatim, including the non-breaking spaces some
# games emit, so the separators are matched as horizontal whitespace only. The
# line anchors keep a loose pattern from matching prose that merely mentions a
# game and a fraction.
_BLANK = r"[^\S\n]"
_WORDLE = re.compile(
    rf"(?im)^{_BLANK}*wordle{_BLANK}+([\d,.]+){_BLANK}+([1-6X])/6\*?{_BLANK}*$"
)
# The header line ends in a shrimp emoji and the dive score is on its own line
# below it, so both lines are matched together.
_KRILLION = re.compile(
    rf"(?im)^{_BLANK}*krillion{_BLANK}*#(\d+)[^\n]*\n{_BLANK}*([\d,]+){_BLANK}*$"
)
_FERMI_PUZZLE = re.compile(rf"(?im)^{_BLANK}*fermi\b[^\n\d]*(\d+){_BLANK}*$")
_FERMI_SCORE = re.compile(rf"(?im)^{_BLANK}*([\d,.]+)×{_BLANK}*score\b")
_CONNECTIONS = re.compile(
    rf"(?im)^{_BLANK}*connections{_BLANK}*\n{_BLANK}*puzzle{_BLANK}*#(\d+){_BLANK}*$"
)
# One guess per line. Connections never reports a count, so the result has to be
# read off the grid: a row of four matching colours is a solved category and any
# other row is a mistake.
_CONNECTIONS_ROW = re.compile(rf"(?m)^{_BLANK}*([🟨🟩🟦🟪]{{4}}){_BLANK}*$")
_NON_DIGIT = re.compile(r"\D")

# Connections ends after four mistakes, so that count is also the loss.
_CONNECTIONS_MISTAKE_LIMIT = 4

# Ranks are compared directly, so an unsolved puzzle needs a value that sorts
# after every solved one.
_FAILED_RANK = 7


@dataclass(frozen=True, slots=True)
class ParsedResult:
    game: str
    puzzle_id: str
    score: str
    rank_key: float


def _parse_wordle(text: str) -> ParsedResult | None:
    match = _WORDLE.search(text)
    if match is None:
        return None

    puzzle_id = _NON_DIGIT.sub("", match.group(1))
    if not puzzle_id:
        return None

    guesses = match.group(2).upper()
    return ParsedResult(
        game="Wordle",
        puzzle_id=puzzle_id,
        score=f"{guesses}/6",
        rank_key=_FAILED_RANK if guesses == "X" else int(guesses),
    )


def _parse_krillion(text: str) -> ParsedResult | None:
    match = _KRILLION.search(text)
    if match is None:
        return None

    depth = int(match.group(2).replace(",", ""))
    return ParsedResult(
        game="Krillion",
        puzzle_id=match.group(1),
        score=str(depth),
        # A deeper dive is a better result, so the rank is negated to keep the
        # leaderboard's lowest-first ordering.
        rank_key=-depth,
    )


def _parse_fermi(text: str) -> ParsedResult | None:
    puzzle = _FERMI_PUZZLE.search(text)
    score = _FERMI_SCORE.search(text)
    if puzzle is None or score is None:
        return None

    try:
        factor = float(score.group(1).replace(",", ""))
    except ValueError:
        return None

    return ParsedResult(
        game="Fermi",
        puzzle_id=puzzle.group(1),
        score=f"{score.group(1)}×",
        # The score is how far off the guesses were, so a perfect round is 1x
        # and smaller is better.
        rank_key=factor,
    )


def _parse_connections(text: str) -> ParsedResult | None:
    match = _CONNECTIONS.search(text)
    if match is None:
        return None

    rows = _CONNECTIONS_ROW.findall(text)
    if not rows:
        return None

    mistakes = sum(1 for row in rows if len(set(row)) != 1)
    if mistakes > _CONNECTIONS_MISTAKE_LIMIT:
        return None

    if mistakes == _CONNECTIONS_MISTAKE_LIMIT:
        score = "lost"
    elif mistakes == 0:
        score = "perfect"
    else:
        score = f"{mistakes} mistake{'s' if mistakes > 1 else ''}"

    return ParsedResult(
        game="Connections",
        puzzle_id=match.group(1),
        score=score,
        rank_key=mistakes,
    )


GAMES: tuple[Callable[[str], ParsedResult | None], ...] = (
    _parse_wordle,
    _parse_krillion,
    _parse_fermi,
    _parse_connections,
)

# What the daily reminder checks each member against, in the order it lists
# them. The names have to match what the parsers above put on a ParsedResult.
GAME_NAMES: tuple[str, ...] = ("Wordle", "Krillion", "Fermi", "Connections")


def parse_result(text: str) -> ParsedResult | None:
    """Return the first recognized daily-game result in a message body."""

    for parse in GAMES:
        result = parse(text)
        if result is not None:
            return result
    return None
