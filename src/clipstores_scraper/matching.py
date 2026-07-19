"""Turn filenames into search queries and score them against store clips.

Matching stacks three independent signals so a link can be trusted:
  * fuzzy title similarity (filename vs clip title, after cleaning),
  * duration agreement (Stash file runtime vs clip runtime),
  * date proximity (Stash scene date vs clip release date).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import date

from rapidfuzz import fuzz

from .models import Clip, MatchCandidate, Scene

_EXTENSIONS = r"\.(?:mp4|mkv|wmv|avi|mov|flv|webm|m4v|mpg|mpeg|ts)$"
_CODECS = r"(?i)\b(?:[hx]\.?26[45]|hevc|xvid|av1|aac|opus)\b"
_RESOLUTION = (
    r"(?i)\b(?:4k|uhd|2160|1440|1080|720|480|360)[pi]?\b"
    r"|\b(?:ultra\s*hd|full\s*hd|hd\s*tv|hd)\b"
)
# Stray container tokens sellers leave in titles ("... MP4", "WMV"). No "ts":
# it doubles as a meaningful tag in this domain.
_CONTAINERS = r"(?i)\b(?:mp4|mkv|wmv|avi|mov|flv|webm|m4v|mpe?g)\b"
_SITES = (
    r"(?i)\[?(?:iwantclips|clips4sale|manyvids|loyalfans|apclips|yourvids)"
    r"(?:\.com)?\]?"
)
# Format/quality noise C4S sellers append to titles ("(HD MP4 VERSION)",
# "Full HD Version", "High Quality", "Complete Movie", "Medium File Size").
_QUALITY = (
    r"(?i)\b(?:wmv|mov|mp4|hd)\s+version\b"
    r"|\b(?:high|full)\s+(?:quality|resolution|definition|version)\b"
    r"|\bin\s+high\s+definition\b|\bsuper[-\s]?hd\b"
    r"|\bmedium\s+file\s+size\b|\bcomplete\s+(?:film|movie)\b"
    r"|\bversion\b"
)
# Honorifics sellers prefix to the name ("Goddess …", "Mistress …") and credit
# noise ("… Starring", "… by"). Stripped from both filename and title, so it can
# only help the score. Honorific is leading-only — "Goddess Worship" mid-title
# is content, not a prefix.
_HONORIFIC = r"(?i)^\s*(?:goddess|mistress|miss|lady|princess|domme)\b\s*"
_CREDIT = r"(?i)\bstarring\b|\bby\s*$"
_LEADING_DATE = r"^\s*(?:\d{4}[-_.]\d{1,2}[-_.]\d{1,2}|\d{1,2}[-_.]\d{1,2}[-_.]\d{4})\b"

# Matching thresholds (see README "How it works"). Not env-tunable on purpose.
TITLE_MIN = 0.85  # title alone makes a candidate at/above this
TITLE_FLOOR = 0.70  # below TITLE_MIN, only if duration AND date both corroborate
TITLE_HIGH = 0.90  # title score needed to qualify as high confidence
DURATION_TOLERANCE = 90  # seconds (store durations are minute-rounded)
DATE_TOLERANCE = 2  # days


def clean_filename(name: str, performer_names: list[str]) -> str:
    """Reduce a filename to its probable scene title."""
    text = re.sub(_EXTENSIONS, "", name)
    text = re.sub(r"^11_*", "", text)  # known prefix junk
    # Our downloader appends "_Downloaded_YYYY_MM_DD_HH_MM_SS" before the
    # extension. Those 7 tokens are pure noise that sink the title score below
    # TITLE_MIN; drop the stamp (and anything after it) before scoring.
    text = re.sub(r"(?i)_downloaded_\d{4}_\d{2}_\d{2}.*$", "", text)
    # Leading release date (26-07-2023 - …, 2023.07.26_…): pure noise that
    # otherwise inflates the query and sinks the title score below TITLE_MIN.
    # Anchored + 4-digit-year required so titles like "1-2-1 session" survive.
    # ponytail: leading date only; handle trailing dates if those show up.
    text = re.sub(_LEADING_DATE, " ", text)
    text = re.sub(_CODECS, " ", text)
    # Glue dotted/dashed numbers ("1.2", "1-4") BEFORE the separator splits
    # below tear them apart: _normalize strips the punctuation from store
    # titles without a space ("1.2" -> "12"), so the filename side must land on
    # the same token or the sequel guard sees phantom numbers.
    text = re.sub(r"(?<=\d)[.\-–—](?=\d)", "", text)
    # Dots are the most common separator in release names (Name.Title.1080p),
    # so treat them like _ and +. Done after codec stripping, which relies on
    # the dot in tokens like "h.264" still being present.
    text = re.sub(r"[_+.]", " ", text)
    text = re.sub(r"[-–—]", " ", text)
    return _strip_common(text, performer_names)


def clean_title(title: str, performer_names: list[str]) -> str:
    """Reduce a store clip title to a comparable form."""
    return _strip_common(title, performer_names)


def _strip_common(text: str, performer_names: list[str]) -> str:
    text = re.sub(_SITES, " ", text)
    # Resolution before quality: "Full HD Version" -> drop "Full HD" as a unit,
    # then "Version" — otherwise "HD Version" goes first and strands "Full".
    text = re.sub(_RESOLUTION, " ", text)
    text = re.sub(_QUALITY, " ", text)
    text = re.sub(_CONTAINERS, " ", text)
    # Longest alias first: stripping a short alias ("Aria Sterling") before a
    # longer one that contains it ("Divine Aria Sterling") guts the middle and
    # strands the prefix ("Divine") in the query.
    for name in sorted(performer_names, key=len, reverse=True):
        if not name:
            continue
        pattern = r"\b" + re.escape(name).replace(r"\ ", r"\s*") + r"\b"
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = re.sub(_CREDIT, " ", text)
    text = re.sub(r"(?<!\w)&(?!\w)", "and", text)
    text = re.sub(r"[\[\]()]", " ", text)
    text = re.sub(r"\b\d{5,}\b", " ", text)  # long id-like numbers
    # Leading honorific last: the name strip above may have exposed it
    # ("Goddess Jane Tease" -> "Goddess  Tease" -> "Tease").
    text = re.sub(_HONORIFIC, " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _normalize(s: str) -> str:
    # Accents fold to bare letters: stores save French titles inconsistently
    # ("Pédagogique" vs "Pedagogique"), and the comparison shouldn't care.
    decomposed = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in decomposed if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s]", "", s.lower())
    # Split digits at word EDGES so "ep.21"/"Pt.2"/"Part2"/"2days" (punctuation
    # just stripped, or none to begin with) expose the same numeric token as
    # their spaced twins ("ep 21", "part 2") for both the fuzzy score and the
    # sequel-number guard. Edges only: a digit flanked by letters on both sides
    # ("Sm0thered", "Coerci0n") is censor-dodging leetspeak, not a sequence
    # number, and must stay inside its word.
    s = re.sub(r"\b(\d+)(?=[a-z])", r"\1 ", s)
    s = re.sub(r"(?<=[a-z])(\d+)\b", r" \1", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _glue_orphan_letters(s)


def _glue_orphan_letters(s: str) -> str:
    """Reattach stray single-letter tokens to an adjacent word.

    Two sources of stray letters:
      * A lone "s" is a split possessive/plural -- "Brother's"/"brother s" both
        become "brothers" -- so it glues *backward* onto the previous word.
      * Our downloader drops accented chars, splitting a word where the accent
        was ("Pédagogique" -> "P dagogique", "obsédé" -> "obs d"); that fragment
        glues *forward* when a real word follows, else *backward* if trailing.

    Keeping the "s" rule separate is what lets a possessive ("mom s ...") collapse
    to "moms" while a genuine extra word ("step-mom") stays distinct. Digits are
    left alone so a sequence number ("Part 1") keeps its own token."""
    toks = s.split()
    out: list[str] = []
    pending = ""
    for i, tok in enumerate(toks):
        if len(tok) == 1 and tok.isalpha():
            if tok == "s" and out and not pending:
                out[-1] += tok  # split possessive: "brother s" -> "brothers"
                continue
            following_word = any(not _is_orphan(x) for x in toks[i + 1 :])
            if following_word or not out:
                pending += tok  # real word follows (or nothing precedes): forward
            else:
                out[-1] += tok  # trailing accent fragment: glue backward
        else:
            out.append(pending + tok)
            pending = ""
    if pending:
        out.append(pending)
    return " ".join(out)


def _is_orphan(tok: str) -> bool:
    return len(tok) == 1 and tok.isalpha()


def title_score(query: str, clip_title: str, performer_names: list[str]) -> float:
    """Fuzzy similarity in 0..1, order-insensitive, with a sequel guard.

    Titles whose numbers contradict ("Part 2" vs "Part 3") score 0: same words,
    different clip. When only ONE side carries a number ("Tease 2" vs "Tease")
    the store may have dropped the sequel number (LoyalFans often does) or the
    clip may be the un-numbered part 1 -- ambiguous, so the score is capped
    below TITLE_MIN: never self-standing, never "high", but still rescuable by
    the duration+date corroboration path as a reviewable "medium"."""
    q = _normalize(query)
    t = _normalize(clean_title(clip_title, performer_names))
    if not q or not t:
        return 0.0
    score = fuzz.token_sort_ratio(q, t) / 100.0
    mismatch = _number_mismatch(q, t)
    if mismatch == "conflict":
        return 0.0
    if mismatch == "one-sided":
        return min(score, TITLE_MIN - 0.01)
    return score


def _number_mismatch(a: str, b: str) -> str | None:
    """Compare the numeric tokens of two titles ("part 2" -> {2}).

    None: same numbers (or none) on both sides. "one-sided": one side's numbers
    are a subset of the other's ("tease 2" vs "tease"). "conflict": both carry
    numbers that disagree ("part 2" vs "part 3").
    ponytail: digit tokens only; spelled-out ("part two") / roman ("II")
    sequels still rely on the fuzzy score."""
    ca = Counter(tok for tok in a.split() if tok.isdigit())
    cb = Counter(tok for tok in b.split() if tok.isdigit())
    if ca == cb:
        return None
    return "one-sided" if ca <= cb or cb <= ca else "conflict"


def _date_delta_days(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except ValueError:
        return None


def score_clip(
    scene: Scene,
    query: str,
    clip: Clip,
    performer_names: list[str],
) -> MatchCandidate | None:
    """Return a candidate if the title clears the bar, else None."""
    score = title_score(query, clip.title, performer_names)

    duration_delta = (
        abs(scene.duration - clip.duration)
        if scene.duration and clip.duration
        else None
    )
    date_delta = _date_delta_days(scene.date, clip.date)

    dur_ok = duration_delta is not None and duration_delta <= DURATION_TOLERANCE
    date_ok = date_delta is not None and date_delta <= DATE_TOLERANCE

    # A title at/above TITLE_MIN stands on its own. Below it, the clip still
    # qualifies (as "medium" via the branches below) only when BOTH duration and
    # date independently corroborate -- the joint signal a human uses to match a
    # renamed/abbreviated title. TITLE_FLOOR keeps unrelated titles out.
    if score < TITLE_MIN and not (score >= TITLE_FLOOR and dur_ok and date_ok):
        return None

    # Every candidate that cleared the gate above is at least review-worthy: a
    # title >= TITLE_MIN stands on its own, and a sub-MIN title only survived
    # because duration AND date both corroborate. So it's "high" only with both a
    # strong title and a corroborating signal, else "medium" (pending). It is never
    # "low" here — a self-standing title in [TITLE_MIN, TITLE_HIGH) used to fall
    # through to "low", which _DEFAULT_DECISION auto-rejects, silently dropping a
    # valid match.
    confidence = "high" if score >= TITLE_HIGH and (dur_ok or date_ok) else "medium"

    return MatchCandidate(
        scene=scene,
        clip=clip,
        title_score=round(score, 3),
        duration_delta=duration_delta,
        date_delta=date_delta,
        confidence=confidence,
    )


# Words clip-store TOS mangle in titles: family relatives get a forced "step-"
# prefix, banned words become "****". Longest-first so "mommy" wins over "mom".
_FAMILY = (
    r"(?:grandmother|grandfather|grandma|grandpa|granny|mommie|momma|mommy|mummy"
    r"|mother|father|brother|sister|auntie|cousin|daughter|nephew|niece|daddy"
    r"|uncle|aunt|mom|mum|dad|bro|sis|son)"
)
# s? covers plurals (stepsisters); the trailing \b stops the match from
# bleeding into longer words ("step-brotherly" is not a stepped "brother").
_STEPPED = rf"(?i)\bstep[-\s]?{_FAMILY}s?\b"
_CENSOR = r"\*{2,}"
_BARE_FAMILY = rf"(?i)(?<!step-)(?<!step )\b{_FAMILY}s?\b"


def destep_text(text: str) -> str:
    """Drop forced "step-" prefixes from family relatives, carrying a capital
    that sat on "Step" over to the term ("Stepmom" -> "Mom")."""

    def repl(m: re.Match) -> str:
        w = m.group(1)
        if m.group(0)[0].isupper() and w[0].islower():
            return w[0].upper() + w[1:]
        return w

    return re.sub(rf"(?i)\bstep[-\s]?({_FAMILY}s?)\b", repl, text)


def has_bare_family(text: str) -> bool:
    """True when the text names a family relative WITHOUT a step- prefix --
    the evidence that the seller's original wording is un-stepped."""
    return bool(re.search(_BARE_FAMILY, text))


def tos_penalty(title: str) -> int:
    """How TOS-mangled a title is: censoring asterisks weigh 2, a forced
    "step-" on a family relative weighs 1, clean is 0."""
    return (2 if re.search(_CENSOR, title) else 0) + (
        1 if re.search(_STEPPED, title) else 0
    )


def stepped_count(text: str) -> int:
    """How many step-<relative> occurrences the text carries."""
    return len(re.findall(_STEPPED, text))


def titles_equivalent_under_tos(a: str, b: str) -> bool:
    """True when the two titles are the same up to TOS mangling: forced
    "step-" prefixes are dropped from both, and a censoring *-run on either
    side wildcards the hidden word."""
    na, nb = _normalize_tos(a), _normalize_tos(b)
    if "*" not in na and "*" not in nb:
        return na == nb
    if "*" in na and "*" in nb:
        return na == nb  # both censored: only identical masking is equal
    censored, clear = (na, nb) if "*" in na else (nb, na)
    parts = [
        re.escape(p).replace(r"\ ", r"\s*") for p in re.split(r"\s*\*+\s*", censored)
    ]
    return bool(re.fullmatch(r"[^*]{1,40}?".join(parts), clear))


def _normalize_tos(s: str) -> str:
    """Comparable form for TOS-equivalence: de-stepped, accent-folded,
    lowercased, punctuation (except censoring asterisks) to whitespace."""
    s = re.sub(_STEPPED, lambda m: re.sub(r"(?i)^step[-\s]?", "", m.group(0)), s)
    decomposed = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in decomposed if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s*]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


# Higher is better; used to rank candidates so a corroborated match wins.
CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


def best_match(
    scene: Scene,
    query: str,
    clips: list[Clip],
    performer_names: list[str],
) -> MatchCandidate | None:
    """Best candidate clip for a scene, if any clear the threshold.

    Ranked by confidence first, then title score: a clip whose duration/date
    also agree (high) should beat a marginally closer title with no
    corroboration (medium), so the strongest auto-applyable link is the one we
    keep rather than being shadowed by a higher raw title score."""
    candidates = [
        c
        for c in (score_clip(scene, query, clip, performer_names) for clip in clips)
        if c
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda c: (CONFIDENCE_RANK[c.confidence], c.title_score))
