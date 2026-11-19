"""
Power steps: what a source delivers at each of a list of drive levels, read off
power-against-time traces, and the amplitude table that follows from them.

No instrument and no GUI in here. The E4405B panel (esa_steps.py) hands this
module zero-span traces; it also runs by itself on saved ones:

    python powersteps.py traces.csv --first 2e-3 --length 5e-3 --period 10e-3
           --count 20 --words 204,409,614,... --table amplitude_table_MW.txt

What it is for (November 2026): the microwave chain of the DDS. The pulse
compiler turns a typed power into the AD9852's 12-bit amplitude word through a
table, and the chain after the DDS - amplifier, mixer, power amplifier - turns
the word into microwave power. Playing a known list of words and measuring the
power of each gives that second curve directly, and with it the table the
compiler should have been using: dds-control's Cal-V2 compiler takes one as a
file (power in W against word / 4095, see its README, "The amplitude table"),
and table_text() writes that file.

The measurement in three statements:

* A **step** is a stretch of trace during which the source held one level. Its
  level is the mean POWER of the middle of the stretch (mean of milliwatts,
  not of dBm - the mean of a log trace reads noise 2.5 dB low and is biased
  for any step that is not perfectly quiet). The edges are left out because
  the resolution filter takes about 1/RBW to settle and the source's own
  timing is not the analyser's.
* Steps are found either from typed timing (first step, length, period,
  count - what a triggered capture gives exactly) or, with no timing, as
  bursts that stand clear of the trace's floor.
* A spectrum analyser's absolute accuracy is about a decibel; its accuracy
  from one level to another at fixed settings is several times better. The
  table needs only the second: it is scaled so that one chosen word keeps a
  chosen power (by default the word the compiler's built-in table gives 1 W,
  so pulses calibrated near 1 W change least), and the atoms set the absolute
  scale as they always have.
"""

import argparse
import datetime
import math
import os
import sys
from collections import namedtuple

import numpy as np

FULL_SCALE = 4095            # the AD9852's amplitude word at full scale
# The word the compiler's built-in (2015) table gives a typed 1 W, and so the
# default point a measured table is pinned to.
ANCHOR_WORD = 1577
ANCHOR_W = 1.0
KEEP = 0.6                   # the middle fraction of a step that is averaged
CLEAR_DB = 10.0              # a step this far above the floor reads true to
#                              0.4 dB; closer, the floor's power adds to it
MAX_TABLE_POINTS = 500       # the compiler's AMPTABLEMAX

Step = namedtuple("Step", "index t_lo t_hi dbm sd_db points")


def to_mw(dbm):
    return 10.0 ** (np.asarray(dbm, dtype=float) / 10.0)


def to_dbm(mw):
    with np.errstate(divide="ignore", invalid="ignore"):
        return 10.0 * np.log10(np.asarray(mw, dtype=float))


def parse_words(text):
    """'204, 409 614' as a list of amplitude words. Empty text is no words."""
    words = []
    for field in text.replace(",", " ").replace(";", " ").split():
        try:
            w = int(field)
        except ValueError:
            raise ValueError(f"'{field}' is not an amplitude word (a whole "
                             f"number, 1 to {FULL_SCALE})") from None
        if not 1 <= w <= FULL_SCALE:
            raise ValueError(f"amplitude word {w} is outside 1 to {FULL_SCALE}")
        words.append(w)
    return words


def windows(first, length, period, count, keep=KEEP):
    """The stretch of each step that is averaged, as (t_lo, t_hi): the middle
    `keep` of a step `length` long, the steps `period` apart from `first`."""
    if length <= 0 or count < 1:
        raise ValueError("a step needs a length above zero and a count of at "
                         "least one")
    if count > 1 and period < length:
        raise ValueError(f"the step period ({period:g} s) is shorter than the "
                         f"step ({length:g} s)")
    edge = length * (1.0 - keep) / 2.0
    return [(first + i * period + edge, first + i * period + length - edge)
            for i in range(count)]


def _level(index, t, y, lo, hi):
    inside = (t >= lo) & (t <= hi)
    n = int(np.count_nonzero(inside))
    if n == 0:
        return Step(index, lo, hi, float("nan"), float("nan"), 0)
    part = y[inside]
    return Step(index, lo, hi, float(to_dbm(np.mean(to_mw(part)))),
                float(np.std(part)), n)


def step_levels(t, dbm, first, length, period, count, keep=KEEP):
    """One Step per typed window. A window with no trace points in it comes
    back with a nan level and points 0, so a step that fell off the end of
    the sweep is seen rather than dropped."""
    t, y = np.asarray(t, dtype=float), np.asarray(dbm, dtype=float)
    return [_level(i, t, y, lo, hi)
            for i, (lo, hi) in enumerate(windows(first, length, period,
                                                 count, keep))]


def lowest_level(dbm):
    """The trace's floor taken as the median of its lowest twentieth - right
    when some of the sweep is spent with the source off."""
    y = np.sort(np.asarray(dbm, dtype=float))
    return float(np.median(y[:max(1, y.size // 20)]))


def find_bursts(t, dbm, margin_db=CLEAR_DB, min_points=5, keep=KEEP):
    """Steps found without timing: every run of at least `min_points` points
    more than `margin_db` above the floor. Returns (steps, floor in dBm).

    Only for a train of pulses with the source off between them. Steps that
    follow one another without a gap are one burst to this; they need timing.
    """
    t, y = np.asarray(t, dtype=float), np.asarray(dbm, dtype=float)
    floor = lowest_level(y)
    above = np.concatenate([[False], y > floor + margin_db, [False]])
    edges = np.flatnonzero(above[1:] != above[:-1])
    steps = []
    for a, b in zip(edges[::2], edges[1::2]):          # points a .. b-1
        if b - a < min_points:
            continue
        cut = (b - a) * (1.0 - keep) / 2.0
        lo, hi = int(math.ceil(a + cut)), int(math.floor(b - 1 - cut))
        if hi < lo:
            lo = hi = (a + b - 1) // 2
        steps.append(_level(len(steps), t, y, t[lo], t[hi]))
    return steps, floor


def floor_between(t, dbm, first, length, period, count):
    """The level with the source off, from the trace outside every step (each
    widened by a fifth of its length), as mean power in dBm. nan when the
    steps leave no such points."""
    t, y = np.asarray(t, dtype=float), np.asarray(dbm, dtype=float)
    off = np.ones(t.size, dtype=bool)
    for i in range(count):
        a = first + i * period
        off &= (t < a - 0.2 * length) | (t > a + 1.2 * length)
    if not off.any():
        return float("nan")
    return float(to_dbm(np.mean(to_mw(y[off]))))


def response(words, levels_dbm, floor_dbm=float("nan")):
    """How the measured levels depart from 'power goes as the word squared'.

    Returns (rel_db, law_dbm, usable): for each step its level minus the law's,
    the law's level, and whether the step stands CLEAR_DB above the floor. The
    law is 20 log10(word) plus a constant, the constant being the median over
    the usable steps in the lowest quarter of the words played (all usable
    steps, when fewer than two are that low). It is a reference to read the
    curve against, not a claim that the bottom of the chain is linear.
    """
    words = np.asarray(words, dtype=float)
    levels = np.asarray(levels_dbm, dtype=float)
    usable = np.isfinite(levels)
    if np.isfinite(floor_dbm):
        usable &= levels >= floor_dbm + CLEAR_DB
    slope = 20.0 * np.log10(words)
    gain = levels - slope
    low = usable & (words <= 0.25 * words.max()) if words.size else usable
    ref = low if np.count_nonzero(low) >= 2 else usable
    const = float(np.median(gain[ref])) if ref.any() else float("nan")
    return gain - const, slope + const, usable


def amplitude_table(words, levels_dbm, anchor_w=ANCHOR_W,
                    anchor_word=ANCHOR_WORD):
    """The compiler's table from measured steps: [(power in W, fraction)],
    starting (0, 0).

    The powers are the measured ones scaled by a single factor, so that
    `anchor_word` comes out at `anchor_w` (its level interpolated between the
    two steps that bracket it, in dB against log word). Refuses, with the
    reason, what the compiler would refuse or what is not a calibration: fewer
    than two steps, a word played twice, a power that does not rise with the
    word (noise at the bottom, or a chain driven past saturation at the top),
    and an anchor outside the words that were played.
    """
    pairs = sorted((int(w), float(l)) for w, l in zip(words, levels_dbm)
                   if np.isfinite(l))
    if len(pairs) < 2:
        raise ValueError("a table needs at least two measured steps")
    if len(pairs) + 1 > MAX_TABLE_POINTS:
        raise ValueError(f"{len(pairs)} steps is more than the compiler's "
                         f"{MAX_TABLE_POINTS - 1}")
    ws = [w for w, _ in pairs]
    ls = [l for _, l in pairs]
    for a, b in zip(ws, ws[1:]):
        if a == b:
            raise ValueError(f"word {a} was played more than once - average "
                             f"the repeats before making a table")
    for (wa, la), (wb, lb) in zip(pairs, pairs[1:]):
        if lb <= la:
            raise ValueError(f"the power does not rise from word {wa} "
                             f"({la:.2f} dBm) to word {wb} ({lb:.2f} dBm), "
                             f"and a table has to rise")
    if not ws[0] <= anchor_word <= ws[-1]:
        raise ValueError(f"the anchor word {anchor_word} is outside the words "
                         f"played ({ws[0]} to {ws[-1]})")
    at = float(np.interp(math.log10(anchor_word), np.log10(ws), ls))
    return [(0.0, 0.0)] + [(anchor_w * 10.0 ** ((l - at) / 10.0),
                            w / FULL_SCALE) for w, l in pairs]


def table_text(points, comments=()):
    """`points` as the file the Cal-V2 compiler and Freq-Scan-GUI read."""
    lines = ["# Amplitude table for NewDDS2-Cooling2.0-Cal-V2 (the compiler's "
             "optional second argument) and Freq-Scan-GUI."]
    lines += ["# " + c if c else "#" for c in comments]
    lines += ["version 1", "interpolation sqrt-linear", "rounding nearest", "",
              "# power (W)       fraction"]
    for power, fraction in points:
        lines.append(f"{power:<17.10g} {fraction:.10g}")
    return "\n".join(lines) + "\n"


def read_table(text):
    """A table file's points back, by the compiler's rules for what a table
    is. Enough of a reader to check what table_text wrote."""
    keywords, points = [], []
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(keywords) < 3:
            keywords.append(line)
            continue
        if len(fields) != 2:
            raise ValueError(f"'{line}' is not 'power fraction'")
        points.append((float(fields[0]), float(fields[1])))
    if keywords != ["version 1", "interpolation sqrt-linear",
                    "rounding nearest"]:
        raise ValueError("the three keyword lines are missing or changed")
    if len(points) < 2 or points[0] != (0.0, 0.0):
        raise ValueError("the first point has to be 0 0, and there have to "
                         "be at least two")
    for (pa, fa), (pb, fb) in zip(points, points[1:]):
        if pb <= pa or fb <= fa:
            raise ValueError("both columns have to rise")
    if points[-1][1] > 1:
        raise ValueError("the fraction is at most 1")
    return points


def levels_csv(path, steps, words=None, floor_dbm=float("nan"), captures=None):
    """One row a step: what was averaged and what came out."""
    have = words is not None and len(words) == len(steps)
    if have:
        rel, law, usable = response(words, [s.dbm for s in steps], floor_dbm)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("step,capture,word,fraction,t_lo (s),t_hi (s),points,"
                 "level (dBm),sd (dB),above floor (dB),"
                 "vs word-squared law (dB)\n")
        for i, s in enumerate(steps):
            cap = captures[i] if captures is not None else 1
            word = f"{words[i]}" if have else ""
            frac = f"{words[i] / FULL_SCALE:.6f}" if have else ""
            clear = (f"{s.dbm - floor_dbm:.2f}"
                     if np.isfinite(floor_dbm) and np.isfinite(s.dbm) else "")
            versus = (f"{rel[i]:.3f}" if have and np.isfinite(rel[i]) else "")
            fh.write(f"{i + 1},{cap},{word},{frac},{s.t_lo:.9g},{s.t_hi:.9g},"
                     f"{s.points},{s.dbm:.4f},{s.sd_db:.4f},{clear},"
                     f"{versus}\n")


def write_traces(path, t, traces):
    """Time and one column a capture. The header says what the columns are."""
    rows = np.column_stack([t] + list(traces))
    head = "Time (s)," + ",".join(f"capture {i + 1} (dBm)"
                                  for i in range(len(traces)))
    np.savetxt(path, rows, delimiter=",", comments="", header=head)


def read_traces(path):
    """(t, [trace, ...]) from write_traces' file, or from a single zero-span
    capture's two-column CSV."""
    with open(path, encoding="utf-8") as fh:
        first = fh.readline().split(",")[0].strip()
    try:
        float(first)
        skip = 0
    except ValueError:
        skip = 1
    rows = np.loadtxt(path, delimiter=",", skiprows=skip, ndmin=2)
    if rows.shape[0] < 2 or rows.shape[1] < 2:
        raise ValueError(f"{os.path.basename(path)} has no trace in it")
    return rows[:, 0], [rows[:, i] for i in range(1, rows.shape[1])]


def analyse(t, traces, first=None, length=None, period=None, count=1,
            margin_db=CLEAR_DB):
    """Steps from every capture in turn, in the order played.

    With `length` the steps are the typed windows, the same in every capture;
    without, each capture's bursts. Returns (steps, captures, floor): the
    Steps renumbered across captures, which capture each came from (1-based),
    and the floor in dBm (the highest any capture showed).
    """
    steps, captures, floors = [], [], []
    for ic, y in enumerate(traces):
        if length:
            got = step_levels(t, y, first or 0.0, length, period or length,
                              count)
            floors.append(floor_between(t, y, first or 0.0, length,
                                        period or length, count))
        else:
            got, floor = find_bursts(t, y, margin_db)
            floors.append(floor)
        for s in got:
            steps.append(s._replace(index=len(steps)))
            captures.append(ic + 1)
    known = [f for f in floors if np.isfinite(f)]
    return steps, captures, (max(known) if known else float("nan"))


def draw(fig, t, traces, steps, captures, words=None, floor_dbm=float("nan"),
         title="", finish=None):
    """The figure: the traces as captured with the averaged stretches marked,
    and under it the level of each step against its word (or its number).

    `finish(ax, xlabel, ylabel)` is the lab's axis styling when the caller has
    one. Every panel has its own x axis and no verdict is written on either.
    """
    def done(ax, xlabel, ylabel):
        if finish is not None:
            finish(ax, xlabel, ylabel)
        else:
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.grid(True, alpha=0.3, which="both")

    top, low = fig.subplots(2, 1)
    for i, y in enumerate(traces):
        top.plot(np.asarray(t) * 1e3, y, lw=0.9,
                 label=f"capture {i + 1}" if len(traces) <= 8 else None)
    shown = set()
    for s, cap in zip(steps, captures):
        key = (round(s.t_lo, 9), round(s.t_hi, 9))
        if key in shown:
            continue
        shown.add(key)
        top.axvspan(s.t_lo * 1e3, s.t_hi * 1e3, color="0.5", alpha=0.2, lw=0)
    if title:
        top.set_title(title)
    done(top, "Time from the start of the sweep (ms); grey: averaged for "
              "each step", "Power (dBm)")
    if 1 < len(traces) <= 8:
        top.legend(fontsize="small")

    levels = np.array([s.dbm for s in steps], dtype=float)
    if words is not None and len(words) == len(steps) and len(steps):
        rel, law, usable = response(words, levels, floor_dbm)
        order = np.argsort(words)
        w = np.asarray(words, dtype=float)
        low.plot(w[usable], levels[usable], "o", ms=4, label="measured")
        if (~usable).any():
            low.plot(w[~usable], levels[~usable], "o", ms=4, mfc="none",
                     label=f"measured, within {CLEAR_DB:g} dB of the floor")
        if np.isfinite(law).any():
            low.plot(w[order], law[order], "-", lw=0.9, color="0.4",
                     label="power proportional to word squared, through the "
                           "low steps")
        low.set_xscale("log")
        low.legend(fontsize="small", loc="lower right")
        done(low, "DDS amplitude word (full scale 4095)", "Power (dBm)")
    else:
        low.plot(np.arange(1, len(steps) + 1), levels, "o", ms=4)
        done(low, "Step number, in the order played", "Power (dBm)")
    # The floor is drawn only when a step is near enough for it to matter;
    # otherwise it would stretch the axis and flatten the curve.
    if (np.isfinite(floor_dbm) and np.isfinite(levels).any()
            and np.nanmin(levels) - floor_dbm < 2 * CLEAR_DB):
        low.axhline(floor_dbm, color="0.6", lw=0.8, ls="--")
    return top, low


def summary_lines(steps, captures, words=None, floor_dbm=float("nan")):
    """The table for the log: a line a step."""
    have = words is not None and len(words) == len(steps)
    if have:
        rel, _law, usable = response(words, [s.dbm for s in steps], floor_dbm)
    lines = []
    for i, s in enumerate(steps):
        bits = [f"step {i + 1:>3}"]
        if have:
            bits.append(f"word {words[i]:>4}")
        if s.points == 0:
            bits.append("no trace points in its window")
        else:
            bits.append(f"{s.dbm:8.2f} dBm  (sd {s.sd_db:.2f} dB, "
                        f"{s.points} points)")
            if have and np.isfinite(rel[i]):
                bits.append(f"{rel[i]:+6.2f} dB vs word squared"
                            + ("" if usable[i] else "  [near the floor]"))
        lines.append("  ".join(bits))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Step levels from saved "
                                 "zero-span traces, and the amplitude table.")
    ap.add_argument("csv", help="traces: time, then one column a capture")
    ap.add_argument("--first", type=float, default=0.0,
                    help="when the first step starts (s)")
    ap.add_argument("--length", type=float, default=None,
                    help="step length (s); without it steps are found as "
                         "bursts above the floor")
    ap.add_argument("--period", type=float, default=None,
                    help="step to step (s), default the length")
    ap.add_argument("--count", type=int, default=1, help="steps a capture")
    ap.add_argument("--margin", type=float, default=CLEAR_DB,
                    help="burst threshold above the floor (dB)")
    ap.add_argument("--words", default="", help="the words played, in order")
    ap.add_argument("--table", default="", help="write the amplitude table "
                                                "to this file")
    ap.add_argument("--anchor-w", type=float, default=ANCHOR_W)
    ap.add_argument("--anchor-word", type=int, default=ANCHOR_WORD)
    args = ap.parse_args(argv)

    t, traces = read_traces(args.csv)
    steps, captures, floor = analyse(t, traces, args.first, args.length,
                                     args.period, args.count, args.margin)
    words = parse_words(args.words) or None
    if words is not None and len(words) != len(steps):
        print(f"{len(words)} words for {len(steps)} steps - the words are "
              f"left out")
        words = None
    print(f"{len(steps)} steps, floor {floor:.2f} dBm")
    for line in summary_lines(steps, captures, words, floor):
        print(line)
    base = os.path.splitext(args.csv)[0]
    levels_csv(base + "_levels.csv", steps, words, floor, captures)
    print("wrote", base + "_levels.csv")
    if args.table:
        if words is None:
            print("no table: it needs the words")
            return 2
        points = amplitude_table(words, [s.dbm for s in steps],
                                 args.anchor_w, args.anchor_word)
        with open(args.table, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(table_text(points, [
                f"Measured: {os.path.basename(args.csv)}, written "
                f"{datetime.datetime.now().isoformat(timespec='seconds')}.",
                f"Scaled so that word {args.anchor_word} is "
                f"{args.anchor_w:g} W."]))
        print("wrote", args.table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
