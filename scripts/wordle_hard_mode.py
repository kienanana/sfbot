"""Guess counts behind Wordle's hard-mode scoring in sfbot/games.py.

A greedy solver plays every answer in normal and hard mode, picking the guess
that tells it the most (maximum expected information) each turn, after a fixed
opener. Running it over several openers and pooling the counts gives the
_WORDLE_GUESSES table. Every opener tried gives the same ranking of hard-mode
results among normal ones, so which openers are pooled does not change scores.

The word lists are the original answer list and the extra allowed guesses:
https://gist.github.com/cfreshman/a03ef2cba789d8cf00c08f767e0fad7b
https://gist.github.com/cfreshman/cdcdf777450c5b5301e439061d29694c

Usage (about a minute):
    uv run --with numpy scripts/wordle_hard_mode.py answers.txt allowed.txt \\
        soare adieu audio crane slate raise stare arise trace roate salet
"""

import sys
from collections import Counter

import numpy as np

answers = open(sys.argv[1]).read().split()
guesses = answers + [
    w for w in open(sys.argv[2]).read().split() if w not in set(answers)
]
A, G = len(answers), len(guesses)
gw = np.array([[ord(c) - 97 for c in w] for w in guesses], dtype=np.int8)
aw = gw[:A]


def pattern_matrix() -> np.ndarray:
    """pattern[g, a]: base-3 feedback (0 grey, 1 yellow, 2 green) of guess g on answer a."""
    pat = np.zeros((G, A), dtype=np.int16)
    for gi in range(G):
        g = gw[gi]
        green = aw == g  # (A,5)
        counts = np.zeros((A, 26), dtype=np.int8)
        for p in range(5):
            np.add.at(counts, (np.arange(A), aw[:, p]), ~green[:, p])
        marks = np.where(green, 2, 0).astype(np.int16)
        for p in range(5):
            avail = counts[np.arange(A), g[p]] > 0
            yellow = ~green[:, p] & avail
            marks[:, p] = np.where(yellow, 1, marks[:, p])
            counts[np.arange(A)[yellow], g[p]] -= 1
        pat[gi] = (marks * np.array([81, 27, 9, 3, 1])).sum(1)
    return pat


PAT = pattern_matrix()
WIN = 242


def decode(p: int) -> list[int]:
    return [(p // 3 ** (4 - i)) % 3 for i in range(5)]


def hard_ok(history: tuple[tuple[int, int], ...]) -> np.ndarray:
    """NYT hard mode: greens stay in place, revealed letters are reused."""
    ok = np.ones(G, dtype=bool)
    for gi, p in history:
        marks = decode(p)
        need = Counter()
        for i, m in enumerate(marks):
            if m == 2:
                ok &= gw[:, i] == gw[gi, i]
            if m:
                need[gw[gi, i]] += 1
        for letter, n in need.items():
            ok &= (gw == letter).sum(1) >= n
    return ok


def entropy(cands: np.ndarray, allowed: np.ndarray) -> np.ndarray:
    sub = PAT[np.ix_(allowed, cands)].astype(np.int64)
    sub += np.arange(len(allowed))[:, None] * 243
    c = np.bincount(sub.ravel(), minlength=len(allowed) * 243).reshape(-1, 243)
    q = c / len(cands)
    with np.errstate(divide="ignore", invalid="ignore"):
        return -np.nansum(q * np.log2(q), axis=1)


def choose(cands: np.ndarray, allowed: np.ndarray) -> int:
    if len(cands) <= 2:
        return int(cands[0])
    h = entropy(cands, allowed)
    # Among equally informative guesses, prefer one that could itself be the answer.
    bonus = np.isin(allowed, cands) * 1e-6
    return int(allowed[np.argmax(h + bonus)])


def solve(answer: int, hard: bool, cache: dict) -> int:
    cands = np.arange(A)
    history: tuple = ()
    for turn in range(1, 50):
        key = (hard, history)
        if key not in cache:
            allowed = np.flatnonzero(hard_ok(history)) if hard else np.arange(G)
            cache[key] = choose(cands, allowed)
        g = cache[key]
        p = int(PAT[g, answer])
        if p == WIN:
            return turn
        history += ((g, p),)
        cands = cands[PAT[g, cands] == p]
    raise RuntimeError


pooled: dict[bool, Counter] = {False: Counter(), True: Counter()}
for opener in sys.argv[3:]:
    for hard in (False, True):
        cache: dict = {(hard, ()): guesses.index(opener)}
        turns = Counter(solve(a, hard, cache) for a in range(A))
        # Wordle allows six guesses, so anything longer is a fail.
        pooled[hard].update("X" if t > 6 else t for t in turns.elements())
for hard in (False, True):
    counts = {k: pooled[hard][k] for k in (1, 2, 3, 4, 5, 6, "X")}
    print("hard" if hard else "normal", counts)
