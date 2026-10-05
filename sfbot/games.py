"""Recognition of daily puzzle-game share text.

Each `GAMES` entry pairs a parser with its name, emoji, and URL. A parser reads
a message body and returns a `ParsedResult`, or `None` when the body is not that
game's share text. Adding a game is a function plus an entry in the registry.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

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
# Each question is a numbered line carrying its own multiplier, above the total.
_FERMI_QUESTION = re.compile(rf"(?m)^{_BLANK}*\d+{_BLANK}+([\d,.]+)×{_BLANK}*$")
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
_CONNECTIONS_CATEGORIES = 4
# The puzzle orders its categories from easiest to hardest, so pulling a purple
# before a yellow is the harder feat and ranks ahead of it.
_CONNECTIONS_DIFFICULTY = {"🟨": 0, "🟩": 1, "🟦": 2, "🟪": 3}
# Both adjustments stay well inside one mistake - an unsolved category costs at
# most 0.3 and the worst solve order 0.07 - so mistakes still decide first.
_CONNECTIONS_UNSOLVED_WEIGHT = 0.1
_CONNECTIONS_ORDER_WEIGHT = 0.005

# Ranks are compared directly, so an unsolved puzzle needs a value that sorts
# after every solved one.
_FAILED_RANK = 7


@dataclass(frozen=True, slots=True)
class ParsedResult:
    game: str
    puzzle_id: str
    score: str
    rank_key: float
    # A game's own shorthand for how the result was reached, shown beside the
    # name on the board. Empty for games whose score already says everything.
    detail: str = ""


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
    # Everything below the score is the dive itself, one emoji per answer. The
    # run ends at the share link, so the ASCII of a URL is dropped along with
    # the blank lines and separators.
    dive = "".join(
        character
        for character in text[match.end() :]
        if not character.isascii() and not character.isspace()
    )
    return ParsedResult(
        game="Krillion",
        puzzle_id=match.group(1),
        score=str(depth),
        # A deeper dive is a better result, so the rank is negated to keep the
        # leaderboard's lowest-first ordering.
        rank_key=-depth,
        detail=dive,
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

    questions = _FERMI_QUESTION.findall(text)
    breakdown = ", ".join(f"{question}×" for question in questions)
    return ParsedResult(
        game="Fermi",
        puzzle_id=puzzle.group(1),
        score=f"{score.group(1)}× ({breakdown})" if questions else f"{score.group(1)}×",
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

    solved = [row[0] for row in rows if len(set(row)) == 1]
    mistakes = len(rows) - len(solved)
    if mistakes > _CONNECTIONS_MISTAKE_LIMIT:
        return None

    if mistakes == _CONNECTIONS_MISTAKE_LIMIT:
        score = "lost"
    elif mistakes == 0:
        score = "perfect"
    else:
        score = f"{mistakes} mistake{'s' if mistakes > 1 else ''}"

    # Solving a hard category late costs more than solving it early, so the
    # penalty is each category's difficulty weighted by when it fell.
    order_penalty = sum(
        position * _CONNECTIONS_DIFFICULTY[colour]
        for position, colour in enumerate(solved)
    )
    return ParsedResult(
        game="Connections",
        puzzle_id=match.group(1),
        score=score,
        rank_key=(
            mistakes
            + _CONNECTIONS_UNSOLVED_WEIGHT * (_CONNECTIONS_CATEGORIES - len(solved))
            + _CONNECTIONS_ORDER_WEIGHT * order_penalty
        ),
        detail="".join(solved),
    )


@dataclass(frozen=True, slots=True)
class Game:
    name: str
    emoji: str
    url: str
    parse: Callable[[str], ParsedResult | None]
    # A puzzle number and the SGT date it was released; one puzzle a day follows.
    anchor: tuple[int, date] | None = None


GAMES: tuple[Game, ...] = (
    Game(
        "Wordle",
        "🆆",
        "https://www.nytimes.com/games/wordle/index.html",
        _parse_wordle,
        (1933, date(2026, 10, 4)),
    ),
    Game(
        "Krillion",
        "🦐",
        "https://krillion.io/",
        _parse_krillion,
        (81, date(2026, 10, 4)),
    ),
    Game("Fermi", "🧮", "https://fermi.gg/", _parse_fermi, (70, date(2026, 10, 4))),
    Game(
        "Connections",
        "🧩",
        "https://www.nytimes.com/games/connections",
        _parse_connections,
        (1211, date(2026, 10, 4)),
    ),
)

GAME_BY_NAME = {game.name: game for game in GAMES}


def expected_puzzle(game: str, day: str) -> str | None:
    """Return the puzzle number a game should show on a local date (YYYY-MM-DD).

    None when the game has no anchor, in which case any number is accepted.
    """

    anchor = GAME_BY_NAME[game].anchor
    if anchor is None:
        return None
    number, released = anchor
    return str(number + (date.fromisoformat(day) - released).days)


def parse_result(text: str) -> ParsedResult | None:
    """Return the first recognized daily-game result in a message body."""

    for game in GAMES:
        result = game.parse(text)
        if result is not None:
            return result
    return None
