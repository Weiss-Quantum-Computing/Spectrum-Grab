"""
Control for the Stanford Research SR760 FFT spectrum analyser over GPIB.

    from sr760 import SR760

    with SR760() as an:                       # auto-finds the SR760 on GPIB
        print(an.idn)
        an.apply(PRESETS["protocol"])
        an.pin_range(-30)
        an.start()
        an.wait_done(600)
        an.refresh_status()                   # one read; the flags are cached
        f, a = an.trace(TRACE, N_BINS)

This is the project's only instrument layer: spectrum_grab.py imports it rather
than carrying its own copy, so a fix to the command spelling, the settings
model, the status handling or the file format lands in the panel and in scripts
at once. It has to sit beside the panel for the panel to run.

Nothing here imports tkinter, matplotlib or pillow, and nothing here draws: a
headless protocol runner imports this module on the bare system interpreter,
where numpy and pyvisa are all that exist.

Two things about this instrument that the code encodes and that are easy to
"fix" back into bugs:

* An SR760 command either takes the graph number as its first parameter or it
  does not, and that decides both how it is queried and how it is written.
  WNDO answers "WNDO? 0" and needs "WNDO 0,2"; a bare "WNDO 2" is read as a
  graph number and silently changes nothing. FMTS and MRLK go the other way -
  they are global underneath and take a single parameter, so "FMTS 0,1" sets
  nothing but the leading 0. Getting it wrong is silent in one direction and a
  timeout in the other, which is why `spellings` carries both and the class
  learns which one this analyser answers.
* Reading a status byte clears it. See `refresh_status`.

Requires: pyvisa + a VISA runtime with GPIB (NI-488.2 on this machine), numpy.
"""

from __future__ import annotations

import datetime
import os
import time
from collections import namedtuple

import numpy as np
import pyvisa

__all__ = [
    "SR760", "Analyzer", "Status", "PRESETS", "SCRIPT_DEFAULTS",
    "DEFAULT_ADDRESS", "TRACE", "N_BINS", "CONNECT_TIMEOUT_MS",
    "OP_TIMEOUT_MS", "SETTINGS_TIMEOUT_MS", "READY_PROBE", "READY_POLL_MS",
    "READY_TIMEOUT_S", "DEFAULT_EXP_WAIT_S", "DEFAULT_SETTLE_RECS",
    "SETTLE_KEYS", "SPACE_OWNERS", "BAD_NAME_CHARS", "SPAN_TOP_HZ",
    "SPAN_LABELS", "SPANS", "SPAN_CHOICES",
    "MAX_FREQ",
    "MAX_LIST_ITEMS", "Setting", "spellings", "num", "enum", "SETTING_GROUPS",
    "ALL_SETTINGS", "BY_KEY", "fmt_setting", "parse_setting", "parse_list",
    "code_of", "value_of", "label_of", "trace_units", "pretty_units",
    "reads_in_db",
    "trace_yscale", "binary_valid", "binary_refusal", "span_hz", "record_time",
    "SPAN_RESETS", "OVLP_ADVANCE_S", "default_overlap",
    "ERRS_OVERLOAD_BIT", "FFTS_OVERLOAD_BIT",
    "independent_records",
    "record_stats", "stats_notes", "averaging_fault", "overlap_fault", "readout_fault",
    "hold_notes", "safe_name", "unique_base", "write_csv", "metadata_text",
    "TRANSFER_BINARY_S", "TRANSFER_ASCII_S", "fmt_hms", "capture_time",
    "UNIT_BASES", "unit_parts", "canonical_units", "to_volts_pk",
    "from_volts_pk", "convert_amplitude", "read_csv",
]

DEFAULT_ADDRESS = "GPIB0::10::INSTR"
TRACE = 0                     # 0 = the active trace
N_BINS = 400                  # the SR760's x-axis resolution, fixed for FFT displays
CONNECT_TIMEOUT_MS = 5000
OP_TIMEOUT_MS = 20000
SETTINGS_TIMEOUT_MS = 2000    # a query the analyzer dislikes costs a whole timeout

# Auto offset takes the analyzer off the bus for several seconds: it accepts the
# command and then stops answering until the DC offset has settled, so anything
# asked in that window comes back VI_ERROR_TMO. Waiting it out means asking
# something harmless over and over with a short timeout - the full operation
# timeout would cost 20 s per miss - until an answer comes back. SPAN? is the
# probe because it is the first query the settings panel makes, so an analyzer
# that answers it is ready for the rest.
READY_PROBE = "SPAN?"
READY_POLL_MS = 1500
READY_TIMEOUT_S = 45.0

# How long to let an average run when it has no finish of its own - exponential
# averaging, or averaging switched off. The measurement timeout is the wrong
# knob for this: it is meant as the limit on a wait that normally ends by
# itself, so reusing it means every such capture takes the whole ten minutes.
DEFAULT_EXP_WAIT_S = 30.0

# Settling after a change of span, start frequency, range or coupling, counted
# in record lengths because that is the unit the analyzer's own settling is in:
# the decimation filter chain has to flush, and how long that takes scales with
# the record, not with the wall clock. wait_ready() is a different thing - it
# waits for the analyzer to answer a query again, which it will do while its
# filters are still full of the previous span's data.
DEFAULT_SETTLE_RECS = 5.0

# Writing any of these means the next average has to settle first.
SETTLE_KEYS = ("SPAN", "STRF", "CTRF", "IRNG", "ARNG", "ICPL")

# Widgets that already use Space themselves. Space is only the GRAB shortcut
# when the focus is not sitting on one of these - otherwise typing a space in
# the title box (or toggling a focused checkbox) would fire an acquisition.
SPACE_OWNERS = {
    "Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox",
    "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton",
    "Button", "TButton",
}

BAD_NAME_CHARS = r'<>:"/\|?*'

# Span is set by code, not by frequency, so this table is the only way back to
# Hz - which the file names, the stitch helper and the metadata all need.
# The labels are the analyzer's own, as printed on the front panel and in the
# manual. The frequencies are NOT: the manual rounds them for display, and this
# table used to carry the rounded figures - 390 Hz for a span that is really
# 390.625, 1.56 kHz for 1562.5.
#
# Every span is the widest one halved, exactly, and the instrument agrees. The
# default overlap a SPAN write installs is whatever holds a 16 ms record
# advance, and it was measured at exactly 93.75 % on code 13 and 98.4375 % on
# code 11 (31 Aug 2026, s/n 41234). Those are 1 - 0.016/T_rec for T_rec of
# 0.256 s and 1.024 s, which are 400 bins over 1562.5 Hz and 390.625 Hz. The
# rounded table gave 93.76 and 98.44 instead, and every record length, settle,
# run estimate and independent-record count inherited the error.
SPAN_TOP_HZ = 100000.0
SPAN_LABELS = (
    "191 mHz", "382 mHz", "763 mHz", "1.5 Hz", "3.1 Hz", "6.1 Hz", "12.2 Hz",
    "24.4 Hz", "48.75 Hz", "97.5 Hz", "195 Hz", "390 Hz", "780 Hz", "1.56 kHz",
    "3.125 kHz", "6.25 kHz", "12.5 kHz", "25 kHz", "50 kHz", "100 kHz",
)
SPANS = [(label, SPAN_TOP_HZ / 2.0 ** (len(SPAN_LABELS) - 1 - i))
         for i, label in enumerate(SPAN_LABELS)]
SPAN_CHOICES = tuple(f"{i} - {label}" for i, (label, _) in enumerate(SPANS))

# The top of the analyzer's band. The widest span starts at 0 and covers all of
# it, so nothing can be measured above this and a narrower span cannot begin any
# higher than MAX_FREQ minus its own width - the analyzer clamps a start
# frequency that would run off the end, silently, and two starts past the limit
# both land on the same place and measure the same band twice.
MAX_FREQ = max(hz for _label, hz in SPANS)

# Settings the panel shows and can push back. `key` doubles as the dict key
# everywhere.
#   num  - free-form number
#   enum - the analyzer answers with an integer; `choices` maps code -> label
#
# An SR760 command either takes the graph number as its first parameter or it
# does not, and that one fact decides how it is both queried and written. Which
# way round a command goes cannot be guessed reliably - WNDO is written
# "WNDO 0,2" but the manual's own summary reads "WNDO i", while FMTS and MRLK go
# the other way - and getting it wrong is silent: the analyzer accepts
# "WNDO 2" as a graph number and changes nothing.
#
# So `queries` holds both spellings, likeliest first, and the app asks with each
# until one answers. `writes` holds the matching write for each, so whichever
# query turned out to be right also fixes the shape of the write. A command that
# answers neither query ends up write-only, using the first spelling.
Setting = namedtuple("Setting", "label key queries writes kind choices")


def spellings(root, trace, first_indexed):
    """The (query, write) pair for each way the command could be spelled,
    likeliest first."""
    t = 0 if trace is None else trace
    indexed = (f"{root}? {t}", f"{root} {t},{{v}}")
    plain = (root + "?", root + " {v}")
    pair = (indexed, plain) if first_indexed else (plain, indexed)
    return tuple(p[0] for p in pair), tuple(p[1] for p in pair)


def num(label, key, root, first_indexed=False):
    queries, writes = spellings(root, None, first_indexed)
    return Setting(label, key, queries, writes, "num", None)


def enum(label, key, root, choices, trace=None, first_indexed=None):
    if first_indexed is None:
        first_indexed = trace is not None
    queries, writes = spellings(root, trace, first_indexed)
    return Setting(label, key, queries, writes, "enum", choices)


SETTING_GROUPS = [
    ("Frequency", [
        enum("Span", "SPAN", "SPAN", SPAN_CHOICES),
        num("Start freq (Hz)", "STRF", "STRF"),
        num("Center freq (Hz)", "CTRF", "CTRF"),
        # The window trades frequency resolution against spectral leakage:
        # uniform resolves best, flattop is most accurate on sine amplitude,
        # Hanning is the one to use on a noise floor, BMH separates close peaks.
        # WNDO is graph-indexed both ways: it answers "WNDO? 0" and needs
        # "WNDO 0,i". A bare "WNDO i" is taken as a graph number and does
        # nothing, which is what the bench scripts were sending.
        enum("Window", "WNDO", "WNDO",
             ("Uniform", "Flattop", "Hanning", "BMH"), first_indexed=True),
    ]),
    ("Measurement (trace 0)", [
        enum("Measurement", "MEAS0", "MEAS",
             ("Spectrum", "PSD", "Time record", "Octave"), TRACE),
        enum("Display", "DISP0", "DISP",
             ("LogMag", "LinMag", "Real", "Imag", "Phase"), TRACE),
        enum("Units", "UNIT0", "UNIT",
             ("Vpk / deg", "Vrms / rad", "dBV", "dBVrms"), TRACE),
        enum("Volts / EU", "VOEU0", "VOEU", ("Volts", "EU"), TRACE),
    ]),
    ("Input", [
        enum("Source", "ISRC", "ISRC", ("A", "A-B")),
        enum("Coupling", "ICPL", "ICPL", ("AC", "DC")),
        enum("Grounding", "IGND", "IGND", ("Float", "Ground")),
        num("Range (dBV)", "IRNG", "IRNG"),
        enum("Auto range", "ARNG", "ARNG", ("Manual", "Auto")),
        enum("Auto offset", "AOFM", "AOFM", ("Off", "On")),
    ]),
    ("Averaging", [
        enum("Averaging", "AVGO", "AVGO", ("Off", "On")),
        num("Number", "NAVG", "NAVG"),
        enum("Type", "AVGT", "AVGT", ("RMS", "Vector", "Peak hold")),
        enum("Mode", "AVGM", "AVGM", ("Linear", "Exponential")),
        num("Overlap (%)", "OVLP", "OVLP"),
    ]),
    ("Display", [
        enum("Active trace", "ACTG", "ACTG", ("Trace 0", "Trace 1")),
        # Single/dual and linked markers are global underneath: they answer
        # the plain query and take a single parameter, so "FMTS 0,1" sets
        # nothing but the leading 0.
        enum("Format", "FMTS0", "FMTS", ("Single", "Dual"), TRACE,
             first_indexed=False),
        enum("Grid", "GRID0", "GRID", ("Off", "8 div", "10 div"), TRACE),
        enum("Style", "FILS0", "FILS", ("Line", "Filled"), TRACE),
        enum("X axis", "XAXS0", "XAXS", ("Linear", "Log"), TRACE),
        enum("Expand", "EXPD0", "EXPD",
             ("8 bins", "15 bins", "30 bins", "64 bins", "128 bins",
              "No expand"), TRACE),
        enum("Marker", "MRKR0", "MRKR", ("Off", "On", "Track"), TRACE),
        enum("Marker width", "MRKW0", "MRKW", ("Norm", "Wide", "Spot"), TRACE),
        enum("Marker seek", "MRKM0", "MRKM", ("Max", "Min", "Mean"), TRACE),
        enum("Linked markers", "MRLK0", "MRLK", ("Off", "On"), TRACE,
             first_indexed=False),
    ]),
]
ALL_SETTINGS = [s for _, group in SETTING_GROUPS for s in group]
BY_KEY = {s.key: s for s in ALL_SETTINGS}

# Two presets, because one block cannot be both a reproduction of history and a
# statement of current discipline. Staged in the panel rather than written, so
# nothing reaches the analyzer without being looked at first.
PRESETS = {
    # Byte-identical to what the read_sr760fft_data bench scripts set at the top
    # of every run, ARNG:1 and NAVG:1000 included. Kept as history: this is what
    # the old data was taken under, and it is what to load to reproduce it. Auto
    # range is right for a survey and wrong for every comparison.
    "legacy": {
        "SPAN": "11", "STRF": "0", "WNDO": "2",
        "MEAS0": "1", "DISP0": "0", "UNIT0": "1", "VOEU0": "0",
        "ISRC": "0", "ICPL": "0", "IGND": "0", "ARNG": "1", "AOFM": "1",
        "AVGO": "1", "NAVG": "1000", "AVGT": "0", "AVGM": "0",
        "ACTG": "0", "FMTS0": "0", "GRID0": "2", "FILS0": "0", "XAXS0": "0",
        "EXPD0": "5", "MRKR0": "2", "MRKW0": "0", "MRKM0": "0", "MRLK0": "0",
    },
    # The RIN validation protocol. Differences from legacy that matter:
    #
    #   ARNG 0   the range is pinned for a whole measurement set, so a range
    #            step cannot masquerade as a step in the noise floor.
    #   OVLP 0   no overlap, so NAVG IS the independent record count and
    #            record_stats becomes a check on the run rather than a
    #            correction to it. The wall clock cost is nothing at wide spans
    #            and about 102 s at the 390 Hz span, which is affordable.
    #   NAVG 100 enough for a 10% (0.41 dB) bin error, which is the accuracy the
    #            segment overlaps are compared at.
    #
    # SPAN and STRF are deliberately absent: they are per-segment and belong in
    # the set definition, not in a global preset that would silently move the
    # band out from under a segment.
    #
    # MEAS0 1 + UNIT0 1 is PSD in Vrms, which trace_units() reports as
    # "Vrms/sqrtHz" - the V/rtHz the RIN maths wants. DISP0 0 (LogMag) with volt
    # units puts the plot on a log axis and keeps the fast binary readout valid.
    "protocol": {
        "WNDO": "2",
        "MEAS0": "1", "DISP0": "0", "UNIT0": "1",
        "ISRC": "0", "ICPL": "0", "ARNG": "0", "AOFM": "1",
        "AVGO": "1", "NAVG": "100", "AVGT": "0", "AVGM": "0", "OVLP": "0",
    },
}

# The old name. Anything that imported it keeps working and keeps meaning what
# it said: the historical block.
SCRIPT_DEFAULTS = PRESETS["legacy"]


def averaging_fault(snap):
    """Why this averaging setting invalidates a noise measurement, or "".

    Two of the analyser's averaging modes are wrong for noise in a way that
    looks right on the screen:

    * Vector averaging averages the complex spectrum, so anything not
      phase-locked to the trigger averages toward zero. On noise - which is all
      of it - that produces a floor that falls forever as NAVG rises. It is
      beautifully clean and completely wrong.
    * Exponential averaging never settles on a count, so the trace is a running
      weighted average of unknown depth and record_stats cannot say what it is
      worth.

    Checked on the snapshot taken with the trace, so a knob turned mid-set is
    caught on the trace it affected rather than at the end of the run.

    An AVGO that could not be read back is a fault in its own right. It is what
    record_stats has to be told to work out an error bar at all, and guessing it
    either invents an average that was never taken or throws away one that was.
    """
    if snap.get("AVGO") is None:
        return ("the averaging setting could not be read back, so what this "
                "trace is an average of cannot be stated")
    if code_of(snap, "AVGO", 0) != 1:
        return ""                     # averaging off is a separate question
    kind = label_of(snap, "AVGT")
    if kind and kind != "RMS":
        return (f"{kind.lower()} averaging: only RMS averaging measures noise, "
                f"{kind.lower()} drives uncorrelated content toward zero")
    mode = label_of(snap, "AVGM")
    if mode and mode != "Linear":
        return (f"{mode.lower()} averaging: the trace is a running average of "
                f"no definite depth, so the statistics cannot be stated")
    return ""


def overlap_fault(snap, span_code=None):
    """Why the overlap in force makes NAVG a poor description of this trace.

    The statistics in the metadata do not depend on this: record_stats counts
    independent records from the clock, elapsed over T_rec, which is right
    whatever the overlap turns out to be. What this adds is the attribution.
    NAVG is the number on the front panel and the number anyone reads off the
    file, and when the overlap has eaten it the useful thing to say is not only
    that it was eaten but what ate it.

    A SPAN write reinstalls that span's default overlap - see default_overlap -
    so an overlap sitting exactly on the default is the signature of one that
    was installed rather than chosen. It can also be a deliberate choice, which
    is why the message says "may have" and why this only speaks when the overlap
    is actually costing something: at or below 1.5x, the same threshold
    stats_notes calls out, there is nothing here worth a flag.
    """
    if code_of(snap, "AVGO", 0) != 1 or code_of(snap, "AVGM", 0) != 0:
        return ""                     # averaging_fault owns those
    navg, ovlp = code_of(snap, "NAVG"), value_of(snap, "OVLP")
    if not navg or ovlp is None or ovlp <= 0:
        return ""
    worth = independent_records(navg, ovlp)
    if not np.isfinite(worth) or navg / worth <= 1.5:
        return ""
    msg = (f"{ovlp:g}% overlap: {navg:g} averages are worth {worth:.3g} "
           f"independent records, so NAVG overstates the statistics "
           f"{navg / worth:.0f}x")
    if span_code is None:
        return msg
    default = default_overlap(span_code)
    if default > 0 and abs(ovlp - default) < 0.5:
        return (msg + f" - and {ovlp:g}% is this span's default, so a SPAN "
                      f"write may have installed it in place of the value "
                      f"that was asked for")
    return msg


def readout_fault(snap):
    """Why the scale this trace is labelled on is an assumption, or "".

    trace_units(), reads_in_db() and trace_yscale() all have to answer with
    something, so they fall back to dBV and LogMag when the snapshot has no
    UNIT0 or DISP0 in it - which is exactly what read_all_settings leaves
    behind once a setting has failed to answer twice and been dropped for the
    session.

    A guess about UNIT0 is a 160 dB guess. It is the one thing that decides
    whether trace_binary rebases bin 0 as a dB offset or through 20 log10, and
    the wrong way round pins bin 0 near zero and drags the whole trace with it -
    a floor the analyzer draws at 10 nV/sqrtHz coming out of the app at about
    0 dBVpk/sqrtHz. binary_valid() refuses the dump outright when either is
    missing, so the number is safe; this is about the label on it, which is
    still a fallback rather than a reading and has to say so.

    Unverified and verified are different claims - the same standard hold_notes
    holds the input range to.
    """
    missing = [BY_KEY[k].label for k in ("UNIT0", "DISP0")
               if k in BY_KEY and snap.get(k) is None]
    if not missing:
        return ""
    return (f"{' and '.join(missing)} could not be read back, so this trace is "
            f"labelled {trace_units(snap)} on a fallback, not on a reading")


def hold_notes(pinned_range, snap, set_name="", armed_at=""):
    """Compare the range a trace was taken on against the pin.

    Given the settings snapshot read straight after the average and before the
    trace is transferred, so the answer describes the trace about to be written
    rather than the state some time later. Returns (notes, ok).

    A range that could not be read back is NOT clean: unverified and verified
    are different claims, and only one of them belongs on a trace that a
    comparison rests on.
    """
    if pinned_range is None:
        return {}, True
    notes = {"range hold": f"{pinned_range:g} dBV"
                           + (f", set '{set_name}'" if set_name else "")
                           + (f", armed {armed_at}" if armed_at else "")}
    raw = snap.get("IRNG")
    if raw is None:
        notes["trace quality"] = ("SUSPECT: the range could not be read back, "
                                  "so the pin is unverified")
        return notes, False
    try:
        got = float(raw)
    except (TypeError, ValueError):
        notes["trace quality"] = f"SUSPECT: IRNG answered {raw!r}"
        return notes, False
    if got != pinned_range:
        notes["trace quality"] = (
            f"SUSPECT: the range moved to {got:g} dBV from the pinned "
            f"{pinned_range:g} dBV, so this trace does not compare with the "
            f"rest of the set")
        return notes, False
    notes["trace quality"] = "clean: range verified against the pin"
    return notes, True


# The analyser's two status bytes, decoded once and cached. See
# SR760.refresh_status for why the caching is not an optimisation.
ERRS_OVERLOAD_BIT = 7          # input overload
FFTS_OVERLOAD_BIT = 5          # FFT overload


class Status(namedtuple("Status", "errs ffts at")):
    """One read of ERRS and FFTS, decoded. `at` is a perf_counter stamp."""

    __slots__ = ()

    @property
    def overloaded(self) -> bool:
        return bool(self.errs_bit(ERRS_OVERLOAD_BIT)
                    or self.ffts_bit(FFTS_OVERLOAD_BIT))

    def errs_bit(self, n):
        return None if self.errs is None else bool(self.errs & (1 << n))

    def ffts_bit(self, n):
        return None if self.ffts is None else bool(self.ffts & (1 << n))

    @property
    def read(self) -> bool:
        """Whether the analyser answered at all."""
        return self.errs is not None or self.ffts is not None

    @property
    def complete(self) -> bool:
        """Whether BOTH bytes answered, which is what it takes to rule an
        overload out. Either byte alone can only ever rule one in: an ERRS that
        came back clear says nothing about the FFT overload in FFTS, and a
        half-read status reported as "no" is the clean-looking answer this
        whole class exists to stop."""
        return self.errs is not None and self.ffts is not None

    @staticmethod
    def _bit_state(value):
        """A bit that was never read is not a bit that was clear."""
        return "unread" if value is None else ("set" if value else "clear")

    def describe(self) -> str:
        if not self.read:
            return "unread"
        if self.overloaded:
            return (f"YES - ERRS bit {ERRS_OVERLOAD_BIT} "
                    f"{self._bit_state(self.errs_bit(ERRS_OVERLOAD_BIT))}, "
                    f"FFTS bit {FFTS_OVERLOAD_BIT} "
                    f"{self._bit_state(self.ffts_bit(FFTS_OVERLOAD_BIT))}")
        missing = [name for name, value in (("ERRS", self.errs),
                                            ("FFTS", self.ffts))
                   if value is None]
        if missing:
            return (f"UNVERIFIED - {' and '.join(missing)} did not answer, so "
                    f"an overload cannot be ruled out")
        return "no"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_name(name):
    """Turn typed text into something safe to put in a CSV header or a file
    name: ASCII word characters only, so no delimiter or encoding surprises."""
    out = "".join(c if (c.isascii() and (c.isalnum() or c in "-.")) else "_"
                  for c in name.strip())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def unique_base(path, base, suffixes):
    """First of '<base>', '<base>_1', '<base>_2' ... for which none of
    `suffixes` exists yet, returned as a path with no suffix on it. Testing
    every suffix together keeps the csv, png and txt of one capture sharing a
    name instead of drifting apart by a counter."""
    name, n = base, 0
    while any(os.path.exists(os.path.join(path, name + suffix))
              for suffix in suffixes):
        n += 1
        name = f"{base}_{n}"
    return os.path.join(path, name)


def fmt_setting(s, raw):
    """Normalise an analyzer reply into what the panel displays. An unexpected
    reply is shown as it arrived rather than swallowed."""
    raw = (raw or "").strip()
    if s.kind == "enum":
        try:
            code = int(float(raw))
        except ValueError:
            return raw
        # Bounds-checked rather than caught, because a negative index does not
        # raise - it wraps, and "-1" would come back as the last choice, which
        # is the plausible-looking wrong answer this whole module is built to
        # avoid. label_of() has always checked; this is the same check.
        return s.choices[code] if 0 <= code < len(s.choices) else raw
    try:
        return f"{float(raw):g}"
    except ValueError:
        return raw


def parse_setting(s, shown):
    """Panel text back to the code the analyzer expects. Raises ValueError on
    anything that is not a listed choice or a number."""
    shown = shown.strip()
    if s.kind == "enum":
        return str(s.choices.index(shown))
    return f"{float(shown):g}"


# The longest sweep axis either form of `parse_list` will hand back. A list is
# not just a list here: the panel allocates a [case][start][span][bin] matrix
# before the first trace, so 2000 start frequencies against 20 spans is already
# most of a gigabyte of NaN. The range form has always been capped; the comma
# form was not, and a pasted column of numbers went straight through it.
MAX_LIST_ITEMS = 2000


def parse_list(text):
    """Comma or space separated numbers, or 'start:stop:step'. An empty box
    gives [], which callers read as 'leave the instrument where it is'. Either
    form is capped at MAX_LIST_ITEMS."""
    text = text.strip()
    if not text:
        return []
    if ":" in text:
        parts = text.split(":")
        if len(parts) != 3:
            raise ValueError("ranges are start:stop:step")
        first, last, step = (float(p) for p in parts)
        if step == 0:
            raise ValueError("step cannot be 0")
        out, value = [], first
        while (value <= last + 1e-9) if step > 0 else (value >= last - 1e-9):
            out.append(value)
            value += step
            if len(out) > MAX_LIST_ITEMS:
                raise ValueError(f"that range is more than {MAX_LIST_ITEMS} "
                                 f"steps")
        return out
    out = [float(p) for p in text.replace(",", " ").split()]
    if len(out) > MAX_LIST_ITEMS:
        raise ValueError(f"that is {len(out)} values; {MAX_LIST_ITEMS} is as "
                         f"long a sweep axis as this will take")
    return out


def code_of(snap, key, default=None):
    """One instrument code out of a settings snapshot, as an int."""
    try:
        return int(float(snap[key]))
    except (KeyError, TypeError, ValueError):
        return default


def value_of(snap, key, default=None):
    """One setting out of a snapshot as a float.

    The counterpart of code_of for the `num` settings. code_of truncates - it
    is reading an enum's index, where an int is the whole point - and that is
    wrong for anything measured: OVLP 98.4375 came back from it as 98, which
    put a fifth of an independent record into every count derived from it and
    hid a reinstalled overlap from the check that looks for one. Use code_of
    for a choice, value_of for a quantity.
    """
    try:
        return float(snap[key])
    except (KeyError, TypeError, ValueError):
        return default


def label_of(snap, key, default=""):
    """The wording an enum code stands for, or `default` when the snapshot has
    nothing usable under that key."""
    s, code = BY_KEY.get(key), code_of(snap, key)
    if s is None or code is None or not 0 <= code < len(s.choices or ()):
        return default
    return s.choices[code]


def trace_units(snap):
    """Y-axis label implied by the measurement and unit codes, in plain ASCII -
    it goes in the CSV header and the metadata as well as on the plot.

    UNIT alone decides the scale the data comes back on. The display mode does
    not: a LogMag display with Vpk units still answers SPEC? in volts. This was
    measured, not assumed - a floor the analyzer drew at 10 nV/sqrtHz, and
    called -161 dBV/sqrtHz once its units were switched to dBV, is 1e-8 V, and
    that is what SPEC? returned while the display was on LogMag. The old rule
    here claimed dB whenever the display was LogMag, which mislabelled every
    volt-unit trace and, worse, let the binary dump rebase a dB trace with a
    linear number. PSD adds the per root hertz that a spectrum does not have."""
    meas = code_of(snap, "MEAS0", 0)
    disp = code_of(snap, "DISP0", 0)
    unit = code_of(snap, "UNIT0", 2)
    if unit not in (0, 1, 2, 3):
        unit = 2
    if disp == 4:                                     # phase
        return "deg" if unit == 0 else "rad"
    label = ("Vpk", "Vrms", "dBV", "dBVrms")[unit]
    if meas == 1:                                     # PSD
        label += "/sqrtHz"
    return label


def pretty_units(label):
    """The same label for a plot axis, where the root sign can be drawn. The
    underscore form is what safe_name() leaves in a CSV header, so a label read
    back off disk gets the same treatment as one straight from the analyzer."""
    return label.replace("/sqrtHz", "/√Hz").replace("_sqrtHz", "/√Hz")


def canonical_units(label):
    """A units label in the one spelling trace_units() uses.

    write_csv puts the label through safe_name(), so a trace measured in
    "dBVrms/sqrtHz" comes back out of its own header as "dBVrms_sqrtHz". They
    are one scale, and anything comparing a loaded trace against a live one has
    to see them as one - otherwise a sequence gets "converted" from a unit to
    itself, and the axis carries the underscore. Anything unrecognised is
    handed back as it came, since inventing a spelling for it would be worse.
    """
    parts = unit_parts(label)
    if parts is None:
        return (label or "").strip()
    base, per = parts
    return base + ("/sqrtHz" if per else "")


# The four scales UNIT can put a trace on. dBV is dB relative to one volt peak
# and dBVrms to one volt rms, which is the whole of the difference between them
# and the reason a conversion has to go through volts rather than adding an
# offset.
UNIT_BASES = ("Vpk", "Vrms", "dBV", "dBVrms")
ROOT2 = float(np.sqrt(2.0))


def unit_parts(label):
    """A trace-units label as (base, per_root_hz), or None.

    Takes what trace_units() produces and what write_csv() writes into a CSV
    header, which is the same string through safe_name() - so "dBVrms/sqrtHz"
    and "dBVrms_sqrtHz" are one label read two ways, and a comparison built on
    the files can recover what the trace was measured in.

    None for anything that is not one of the four amplitude scales. Phase is
    the case that matters: degrees do not convert into volts, and a compare
    that quietly put them on the same axis would be drawing nonsense.
    """
    text = (label or "").strip()
    per = False
    for tail in ("/sqrtHz", "_sqrtHz", "/√Hz"):
        if text.endswith(tail):
            text, per = text[:-len(tail)], True
            break
    for base in UNIT_BASES:
        if text.lower() == base.lower():
            return base, per
    return None


def to_volts_pk(amps, base):
    """One of the four scales into volts peak, which is the pivot everything
    converts through."""
    a = np.asarray(amps, dtype=float)
    if base == "Vpk":
        return a
    if base == "Vrms":
        return a * ROOT2
    if base == "dBV":
        return 10.0 ** (a / 20.0)
    if base == "dBVrms":
        return 10.0 ** (a / 20.0) * ROOT2
    raise ValueError(f"{base!r} is not one of {UNIT_BASES}")


def from_volts_pk(volts, base):
    """Volts peak back out onto one of the four scales. A value that is not
    positive has no dB equivalent and comes back NaN rather than -inf, so it
    leaves a gap in the plot instead of dragging the axis to the floor."""
    v = np.asarray(volts, dtype=float)
    if base == "Vpk":
        return v
    if base == "Vrms":
        return v / ROOT2
    if base in ("dBV", "dBVrms"):
        ref = v if base == "dBV" else v / ROOT2
        with np.errstate(divide="ignore", invalid="ignore"):
            out = 20.0 * np.log10(ref)
        return np.where(ref > 0, out, np.nan)
    raise ValueError(f"{base!r} is not one of {UNIT_BASES}")


def convert_amplitude(amps, from_label, to_label):
    """A trace from one unit label onto another. Raises ValueError when the two
    do not describe the same kind of quantity.

    The refusals are the point. A spectrum and a spectral density are not the
    same measurement and converting between them needs the bin width, which no
    file here carries; phase is not an amplitude at all. Both come back as an
    error rather than a plot, because the only thing worse than not being able
    to compare two traces is being shown a comparison that is wrong.
    """
    a, b = unit_parts(from_label), unit_parts(to_label)
    if a is None or b is None:
        bad = from_label if a is None else to_label
        raise ValueError(f"{bad!r} is not one of the amplitude scales "
                         f"{UNIT_BASES}, so it cannot be converted")
    if a[1] != b[1]:
        raise ValueError(
            f"{from_label!r} and {to_label!r} are not the same kind of "
            f"quantity - one is per root hertz and the other is not, and "
            f"converting between them needs the bin width the file does not "
            f"carry")
    if a[0] == b[0]:
        return np.asarray(amps, dtype=float)
    return from_volts_pk(to_volts_pk(amps, a[0]), b[0])


def reads_in_db(snap):
    """Whether the trace comes back in dB rather than in volts."""
    return code_of(snap, "UNIT0", 2) in (2, 3)


def trace_yscale(snap):
    """The y axis the analyzer is drawing this trace on, so the plot matches the
    screen: "log" or "linear".

    Only volt data ever gets a log axis. dB data is a log axis already - taking
    the log of it again means nothing, and a dB reading is usually negative,
    which a log axis cannot draw at all. Of the display modes only LogMag is
    logarithmic: Real and Imag are signed linear quantities and Phase is degrees
    or radians."""
    if reads_in_db(snap):
        return "linear"
    return "log" if code_of(snap, "DISP0", 0) == 0 else "linear"


def binary_valid(snap):
    """Whether the SPEB? dump can be used for the readout in force.

    Two things have to be known, not assumed. The counts are a dB mapping of
    the LogMag display, so DISP0 has to say LogMag; and the rebase of bin 0
    goes one way on dB units and quite another on volts, so UNIT0 decides
    whether the dump comes out on the right scale at all.

    Both are read with no default here, deliberately. Everywhere else a missing
    DISP0 falls back to LogMag and a missing UNIT0 to dBV, because trace_units()
    has to put something on the axis - but a setting that could not be read back
    is an unknown display, not a LogMag one, and rebasing the dump against an
    unknown display is how a volt trace comes back 160 dB adrift. Falling back
    to the ASCII readout costs a minute. Guessing costs the measurement.
    readout_fault() puts the same doubt on the label either way.
    """
    return code_of(snap, "DISP0") == 0 and code_of(snap, "UNIT0") is not None


def binary_refusal(snap):
    """Why binary_valid() said no, as the line to log, or "" when it said yes.

    Here rather than at the three call sites that used to word it themselves,
    because they had all learned the one reason there used to be - a linear
    display - and none of them would have mentioned the new one."""
    if binary_valid(snap):
        return ""
    unread = [BY_KEY[k].label for k in ("DISP0", "UNIT0")
              if k in BY_KEY and snap.get(k) is None]
    if unread:
        return (f"{' and '.join(unread)} could not be read back, so there is "
                f"no telling which way the binary dump would need rebasing - "
                f"reading bin by bin instead")
    return ("linear display: falling back to the ASCII readout, the binary "
            "dump is a dB mapping")


def span_hz(code):
    return SPANS[code][1] if 0 <= code < len(SPANS) else float("nan")


def record_time(span_code, n_bins=N_BINS):
    """The analyzer's own record length, bins / span, in seconds.

    35 minutes at the 191 mHz span and 4 ms at 100 kHz, which is why every
    settling and averaging time in this module is quoted in record lengths
    rather than in seconds."""
    hz = span_hz(span_code) if span_code is not None else float("nan")
    return n_bins / hz if hz and np.isfinite(hz) and hz > 0 else float("nan")


# Writing SPAN silently reinstalls a span-derived OVLP - measured 31 Aug 2026
# on s/n 41234, all three orderings. STRF does not, so a stitch is safe once
# the span is set; the exposure is any code that moves SPAN on its own, and
# PRESETS["protocol"] carries OVLP with no SPAN precisely so span stays
# per-segment - which puts every span write after it.
SPAN_RESETS = ("OVLP",)

# What it reinstalls: whatever overlap holds a 16 ms record advance, clamped
# at zero once the record is already longer than that.
OVLP_ADVANCE_S = 0.016


def default_overlap(span_code, n_bins=N_BINS):
    """The overlap % a SPAN write leaves behind, without asking the analyzer.

    0 at spans 17-19, then 50 / 75 / 87.5 / 93.75 / 98.44 as T_rec doubles
    below 16 ms. The trap is that span 19's default IS zero, so the one trace
    anyone spot-checks reads correctly while narrow spans quietly average
    16-64x fewer independent records than NAVG claims.
    """
    t = record_time(span_code, n_bins)
    if not np.isfinite(t) or t <= OVLP_ADVANCE_S:
        return 0.0
    return 100.0 * (1.0 - OVLP_ADVANCE_S / t)


# Per-capture overheads, seconds. Estimates for the clock only - nothing at run
# time is derived from them, and a sweep corrects them against its own measured
# runs as it goes. TRANSFER_BINARY_S is the figure run_protocol.plan_set costs a
# trace at; the ASCII one is 800 queries bin by bin, which is the two orders of
# magnitude the fast dump exists to avoid.
TRANSFER_BINARY_S = 1.5
TRANSFER_ASCII_S = 25.0


def independent_records(navg, ovlp=0.0):
    """What NAVG averages at OVLP% overlap are actually worth, in records.

    Records that share samples share information. Each new record advances by
    (1 - overlap) of a record length, so N of them span

        1 + (N - 1)(1 - overlap)

    record lengths, and that - not N - is what the error bar rests on. At no
    overlap it is N, which is the whole reason the protocol preset sets OVLP 0.

    This is the same count record_stats arrives at from the clock, elapsed
    divided by T_rec, and capture_time multiplies back up to predict a run. Here
    it is worked out from the two numbers on the front panel instead, so the
    panel can say what a setting is worth before the run rather than after.

    It matters because SPAN reinstalls its own default overlap - 98.44 % at the
    narrow end - so NAVG can be honoured to the letter while the trace is worth
    a fraction of it. See default_overlap.
    """
    try:
        n = float(navg)
        share = min(max(float(ovlp or 0.0) / 100.0, 0.0), 0.99)
    except (TypeError, ValueError):
        return float("nan")
    if not np.isfinite(n) or n < 1:
        return float("nan")
    return 1.0 + (n - 1.0) * (1.0 - share)


def fmt_hms(seconds):
    """Seconds as the shortest honest thing to read.

    The same function run_protocol carries. Duplicated rather than shared for
    the reason its SPAN table is: the planner has to cost a bench session on a
    machine with no instrument code, so it cannot import this module."""
    if not np.isfinite(seconds):
        return "?"
    s = int(round(seconds))
    if s < 60:
        return f"{seconds:.1f}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def capture_time(span_code, navg=None, ovlp=None, averaged=True,
                 settle_recs=0.0, extra_settle_s=0.0, autorange_s=0.0,
                 exp_wait_s=0.0, timeout_s=None,
                 transfer_s=TRANSFER_BINARY_S, n_bins=N_BINS):
    """How long one capture should take, in seconds. NaN if the span is unknown.

    Everything is in record lengths, because that is the only clock the
    analyzer has: T_rec = bins/span is 4 ms at the 100 kHz span and 35 minutes
    at 191 mHz, so nothing here can be quoted in seconds without knowing which
    span it is on.

        settle      recs * T_rec, the decimation chain flushing
        average     T_rec + (N-1)*(1-overlap)*T_rec
        transfer    a flat figure, and the smaller term by far

    At OVLP 0 the averaging term is NAVG * T_rec exactly, which is the same
    arithmetic run_protocol.plan_set does and the reason the protocol preset
    sets no overlap: the plan, the clock and record_stats then all agree.

    With overlap it is a floor rather than an estimate. Records that share
    samples arrive faster in principle, but the analyzer has to finish an FFT
    between them, and at wide spans that is what limits it rather than the
    acquisition - so the real run lands somewhere between this and the
    no-overlap figure. A sweep does not have to care, because it rescales what
    is left against the runs it has actually timed.

    An average with no finish of its own - exponential, or averaging off - is
    not counted in records at all: it runs for exp_wait_s, because that is what
    the panel will wait. `timeout_s` caps the estimate the way the measurement
    timeout caps the wait.
    """
    t_rec = record_time(span_code, n_bins)
    if not np.isfinite(t_rec):
        return float("nan")
    if averaged and navg:
        # One model, shared with the panel's own read-out: a run lasts as many
        # record lengths as the averaging is worth in independent records.
        measure = t_rec * independent_records(navg, ovlp)
    else:
        measure = float(exp_wait_s or 0.0)
    if timeout_s:
        measure = min(measure, float(timeout_s))
    return (float(settle_recs) * t_rec + float(extra_settle_s)
            + float(autorange_s) + measure + float(transfer_s))


def record_stats(span_code, elapsed_s, navg=None, ovlp=None, n_bins=N_BINS,
                 averaged=True):
    """How much independent averaging a run actually bought.

    NAVG counts records the analyzer averaged, not independent ones. With OVLP
    above zero the records share samples, so they carry less information than
    their count suggests - at 90% overlap ten records hold about the same as
    two, and the reported NAVG overstates the statistics several-fold. What the
    error bar actually rests on is how long the run was in units of the record
    length:

        T_rec   = bins / span            the analyzer's own record length
        N_indep = elapsed / T_rec        records that did not share samples
        rel_err = 1 / sqrt(N_indep)      1-sigma on an RMS-averaged PSD bin

    rel_err is the fractional error on the power in a bin, so a value of 0.1
    is +/- 10%, about +/- 0.41 dB. It is the honest bar to put on a segment
    before comparing it with its neighbour.

    All of that assumes the analyzer averaged what it acquired. `averaged` is
    the caller saying whether it did - AVGO, in practice. With averaging off the
    model collapses: the analyzer goes on acquiring records and goes on throwing
    them away, so what is on the screen at the end of a ten minute run is the
    newest record and nothing else. N_indep is 1 however long the run was, and
    rel_err is 1 - a single periodogram bin of noise is exponentially
    distributed, so its standard deviation equals its mean. The elapsed/T_rec
    count would have claimed 29 records and a 0.19 bar for a 30 s run at the
    390 Hz span, five times better than the trace really is.

    Returns a dict of plain numbers; NaN wherever the inputs do not support the
    calculation rather than a guess."""
    t_rec = record_time(span_code, n_bins)
    elapsed = float(elapsed_s)
    if averaged:
        n_indep = (elapsed / t_rec
                   if np.isfinite(t_rec) and t_rec > 0 else float("nan"))
    else:
        n_indep = 1.0
    rel_err = (1.0 / np.sqrt(n_indep)
               if np.isfinite(n_indep) and n_indep > 0 else float("nan"))
    out = {"t_rec_s": t_rec, "elapsed_s": elapsed, "n_indep": n_indep,
           "rel_err": rel_err, "navg": navg, "overlap_pct": ovlp,
           "averaged": bool(averaged)}
    # The ratio is the point of the whole calculation: it says how far NAVG is
    # from what the run can actually support. It is a statement about an average
    # that was taken, so it means nothing when none was.
    out["navg_over_indep"] = (navg / n_indep
                              if averaged and navg and np.isfinite(n_indep)
                              and n_indep > 0 else float("nan"))
    return out


def stats_notes(stats):
    """`record_stats` as the lines that go in the metadata file."""
    def g(key, fmt="{:.4g}"):
        v = stats.get(key)
        return fmt.format(v) if isinstance(v, (int, float)) and np.isfinite(v) \
            else "?"
    notes = {
        "record length T_rec (s)": g("t_rec_s"),
        "independent records": g("n_indep", "{:.1f}"),
        "relative error (1 sigma)": g("rel_err", "{:.3g}"),
        "relative error (dB)": (f"{10 * np.log10(1 + stats['rel_err']):.2f}"
                                if np.isfinite(stats.get("rel_err", np.nan))
                                else "?"),
    }
    if not stats.get("averaged", True):
        notes["averaging"] = ("OFF - the trace is one record, so the bar above "
                              "is a single bin's own scatter and not anything "
                              "the length of the run bought")
    if stats.get("navg"):
        notes["averages reported (NAVG)"] = (
            f"{stats['navg']:g}"
            + ("" if stats.get("averaged", True)
               else "  <- not in force, averaging is off"))
        ratio = stats.get("navg_over_indep", float("nan"))
        if np.isfinite(ratio):
            notes["NAVG / independent"] = (
                f"{ratio:.2f}" + ("  <- NAVG overstates the statistics"
                                  if ratio > 1.5 else ""))
    if stats.get("overlap_pct") is not None:
        notes["overlap (%)"] = f"{stats['overlap_pct']:g}"
    return notes


# ---------------------------------------------------------------------------
# Instrument layer
# ---------------------------------------------------------------------------

class SR760:
    """The SR760 over GPIB. One VISA session, so every exchange the GUI makes
    is serialised through the worker thread."""

    def __init__(self, addr=None, resource_manager=None, connect=True,
                 log=None):
        """connect=False builds the object without touching the bus, for a
        caller that opens the instrument later from a worker thread - which is
        what the panel does, so the GUI stays responsive while VISA blocks.

        `resource_manager`, if given, is never closed by this object. A process
        driving both this and the scope must hand the same RM to both: a second
        ResourceManager half-loads on this machine and then fails every
        open_resource with VI_ERROR_ALLOC. See ilc_bench._shared_rm.
        """
        self._given_rm = resource_manager
        self.rm = None
        self.inst = None
        self.idn = ""
        self.addr = addr or ""
        self.log = log if log is not None else (lambda _msg: None)
        # Which spelling of each command this analyzer turned out to answer,
        # and how many times a setting has failed to answer at all.
        self.qform = {}
        self.dead = {}
        self.last_raw = {}
        self._status = None
        self._pre_start = None
        if connect:
            self.connect(addr)

    @property
    def connected(self) -> bool:
        return self.inst is not None

    def connect(self, addr=None):
        self.close()
        self.rm = self._given_rm or pyvisa.ResourceManager()
        if addr:
            candidates = [addr]
        else:
            candidates = [r for r in self.rm.list_resources()
                          if r.upper().startswith("GPIB")]
        if not candidates:
            raise RuntimeError("No GPIB resources found - check the cable and "
                               "that NI MAX sees the SR760.")
        problems = []
        for res in candidates:
            dev = None
            try:
                dev = self.rm.open_resource(res)
                dev.timeout = CONNECT_TIMEOUT_MS
                # The SR760 ends a command at the first line feed, and pyvisa's
                # default terminator leaves a stray carriage return behind it,
                # so pin the write terminator down. Replies finish on EOI, which
                # is why nothing is set for reads.
                dev.write_termination = "\n"
                try:
                    idn = dev.query("*IDN?").strip()
                except Exception:
                    # An analyzer left on the RS232 output takes GPIB commands
                    # but answers down the serial port, so the first query just
                    # times out. Point the output back at GPIB and ask again -
                    # this is the "*IDN? always times out" the scripts lived with.
                    dev.clear()
                    dev.write("OUTP 1")
                    idn = dev.query("*IDN?").strip()
            except Exception as exc:
                problems.append(f"  {res}: {exc}")
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
                continue
            # A typed address is taken at its word; a scan only claims an SR760.
            if addr or "SR760" in idn.upper():
                dev.timeout = OP_TIMEOUT_MS
                self.inst, self.idn, self.addr = dev, idn, res
                return idn
            problems.append(f"  {res}: not an SR760 ({idn})")
            dev.close()
        raise RuntimeError("No SR760 found.\n" + "\n".join(problems))

    def close(self):
        # A given ResourceManager belongs to the caller and outlives us.
        for obj in (self.inst, None if self.rm is self._given_rm else self.rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.inst = self.rm = None

    # -- primitives -------------------------------------------------------

    def put(self, cmd):
        self.inst.write(cmd)

    def get(self, query):
        return self.inst.query(query).strip()

    def recover(self):
        """Flush a half-finished exchange after a timeout, so the next query
        cannot read the previous reply."""
        try:
            self.inst.clear()
        except Exception:
            pass

    def pin_range(self, dbv):
        """Put the input range on manual and hold it at `dbv`.

        ARNG has to go to manual first: writing IRNG while auto range is on
        sets a value the analyzer is free to move off again on the next
        overload, which is the whole failure this exists to stop.

        Through command() rather than spelled out, so that if this analyzer
        turns out to answer the graph-indexed form of either command, the write
        that pins the range moves with it. A bare "IRNG -30" against an analyzer
        that wanted "IRNG 0,-30" is read as a graph number and pins nothing, and
        says nothing about having done so."""
        self.put(self.command("ARNG", 0))
        self.put(self.command("IRNG", f"{dbv:g}"))

    def input_range(self):
        """The range the analyzer says it is on, as a float, or None."""
        try:
            return float(self.get(self.query_for("IRNG")))
        except Exception:
            self.recover()
            return None

    def lock_panel(self, locked):
        """OVRM 0 locks the front panel for the duration of a measurement, so a
        stray knob cannot change the settings the metadata says were used."""
        self.put("OVRM 0" if locked else "OVRM 1")

    def autoscale(self):
        self.put("AUTS 0")

    def wait_ready(self, probe=READY_PROBE, timeout=READY_TIMEOUT_S, poll=0.4):
        """Block until the analyzer answers `probe` again; return how long that
        took, or None if it never did.

        Auto offset is the command this exists for. It runs on the analyzer for
        several seconds with the bus ignored, so the settings read that used to
        follow it straight away lost its first few queries to a timeout each -
        the SPAN?/STRF?/CTRF? run of failures. Each probe is given a short
        timeout of its own and the previous half-finished exchange is flushed
        between tries, so nothing is left in the buffer for the next query to
        read as its own reply."""
        previous = self.inst.timeout
        self.inst.timeout = READY_POLL_MS
        t0 = time.time()
        try:
            while True:
                try:
                    self.get(probe)
                    return time.time() - t0
                except Exception:
                    self.recover()
                if time.time() - t0 >= timeout:
                    return None
                time.sleep(poll)
        finally:
            self.inst.timeout = previous

    # -- acquisition ------------------------------------------------------

    def autorange(self, seconds, stop=None, poll=0.5):
        """Step the input range down to the most sensitive setting that does not
        overload, then freeze it.

        Auto range walks the range up from -60 dBV until the overload clears, so
        it needs to be left on long enough to settle before going back to
        manual - otherwise a later run in the same sweep moves the range and the
        noise floors no longer compare. Returns the range it settled on and how
        often an overload bit showed up while it worked."""
        self.put(self.command("ARNG", 0))
        self.put(self.command("ARNG", 1))
        overloads = polls = 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            if stop is not None and stop():
                break
            # Through the same cached read as everything else, so there is one
            # code path and one pair of queries.
            st = self.refresh_status()
            if not st.read:
                break
            polls += 1
            if st.overloaded:
                overloads += 1
            time.sleep(poll)
        self.put(self.command("ARNG", 0))
        try:
            rng = self.get(self.query_for("IRNG"))
        except Exception:
            self.recover()
            rng = "?"
        return rng, overloads, polls

    def bin_spacing(self, trace, n_bins):
        """Hz between neighbouring bins at the span the analyzer is on.

        Read from the instrument rather than worked out from the span table:
        the table holds the manual's printed values, which are rounded (390 Hz
        for a span that is really 390.625), and that error would accumulate into
        a visible misalignment over a long stitch.

        Measured across the whole trace rather than between two adjacent bins.
        The analyzer prints its bin frequencies to a handful of digits, and
        differencing neighbours would keep all of that rounding while
        differencing end to end divides it by the number of intervals. It also
        makes the spacing the same one trace_binary lays into the frequency
        column of the CSV, so a stitch overlap lands on the saved points."""
        first = self.inst.query_ascii_values(f"BVAL? {trace},0")[0]
        last = self.inst.query_ascii_values(f"BVAL? {trace},{n_bins - 1}")[0]
        return (last - first) / (n_bins - 1)

    def start(self, log=None):
        """Restart the average. Same as the [START] key.

        Reads the status bytes first, which CLEARS them. ERRS bit 7 latches, so
        a transient from before the run - swapping a resistor on the input, a
        range change, reconnecting a cable - otherwise survives into the next
        average and is reported as though the run had overloaded. Measured
        31 Aug 2026: every span-11 Johnson trace came back flagged, 50 ohm
        included, which cannot overload a -60 dBV range at 0.91 nV/rtHz; the
        flagged traces agreed with the unflagged ones to 0.16 dB.

        What was latched is kept in pre_start() rather than thrown away. A bit
        set before a run is not a fault in the run, but it is worth knowing
        about - it usually means something was touched while the input was
        live. The cache is then dropped so the post-run read stands alone.
        """
        self._pre_start = self.refresh_status()
        if log is not None and self._pre_start.overloaded:
            log("  (an overload flag was already latched before this run - "
                "cleared, and not counted against it)")
        self.invalidate_status()
        self.put("STRT")

    def pre_start(self):
        """What was latched at the last start(), already cleared by reading it.

        A flag here belongs to whatever happened before the run, not to the run.
        Worth putting in the metadata: it is the difference between a trace that
        saturated and a trace taken after someone touched the input.
        """
        return self._pre_start

    def wait_done(self, timeout, stop=None, poll=0.25):
        """Wait for the averaged measurement to finish.

        Bit 0 of the serial poll byte is clear while a measurement runs, so the
        poll starts after a short settle: asking immediately can catch the bit
        still set from the previous run and return at once. Returns 'done',
        'timeout', 'stopped' or 'error'."""
        time.sleep(0.5)
        deadline = time.time() + timeout
        misses = 0
        while time.time() < deadline:
            if stop is not None and stop():
                return "stopped"
            try:
                if int(self.get("*STB?0")):
                    return "done"
                misses = 0
            except Exception:
                self.recover()
                misses += 1
                if misses >= 3:
                    return "error"
            time.sleep(poll)
        return "timeout"

    def trace_ascii(self, trace, n_bins, progress=None):
        """Bin-by-bin ASCII readout: two queries per bin, so slow, but valid
        whatever the display is set to."""
        freqs, amps = [], []
        for i in range(n_bins):
            f = self.inst.query_ascii_values(f"BVAL? {trace},{i}")[0]
            time.sleep(0.0005)   # the analyzer drops replies if pushed harder
            a = self.inst.query_ascii_values(f"SPEC? {trace},{i}")[0]
            time.sleep(0.0005)
            freqs.append(f)
            amps.append(a)
            if progress is not None and (i + 1) % 100 == 0:
                progress(i + 1, n_bins)
        return np.array(freqs), np.array(amps)

    def trace_binary(self, trace, n_bins, in_db=True):
        """SPEB? dump: the whole trace as int16 display counts in one read, some
        two orders of magnitude faster than asking bin by bin.

        The counts carry a dB mapping, so this is only valid while the display
        is LogMag - the caller checks before choosing it. Bin 0 is also read the
        slow way with SPEC? and used to put the dump back on the analyzer's own
        scale, which absorbs whatever reference the chosen units imply.

        `in_db` says which scale SPEC? is answering on, which is decided by UNIT
        and nothing else. On dBV or dBVrms the rebase is a straight offset. On
        Vpk or Vrms it is not: SPEC? hands back volts, and adding a linear value
        to a dB trace pinned bin 0 near zero and dragged the rest of the trace
        with it - a floor the analyzer drew at 10 nV/sqrtHz came out of the app
        at about 0 dBVpk/sqrtHz, some 160 dB adrift. So bin 0 goes into dB for
        the rebase and the whole trace comes back out of it afterwards; 20 log10
        is the right conversion here, the same one that makes the analyzer's own
        10 nV/sqrtHz and -161 dBV/sqrtHz two readings of one noise floor."""
        start_freq = self.inst.query_ascii_values(f"BVAL? {trace},0")[0]
        stop_freq = self.inst.query_ascii_values(f"BVAL? {trace},{n_bins - 1}")[0]
        freqs = np.linspace(start_freq, stop_freq, n_bins)

        first_bin = self.inst.query_ascii_values(f"SPEC? {trace},0")[0]
        if not in_db and not first_bin > 0:
            # No dB reference to rebase against, so say so and let the caller
            # fall back rather than return a trace that is quietly wrong.
            raise ValueError(f"bin 0 reads {first_bin:g}, which has no dB "
                             f"equivalent to rebase the binary dump against")
        self.inst.write(f"SPEB? {trace}")
        raw = self.inst.read_bytes(2 * n_bins, break_on_termchar=False)
        counts = np.frombuffer(raw, dtype="<i2")
        amps = (3.0103 * counts) / 512.0 - 114.3914
        if in_db:
            return freqs, amps + (first_bin - amps[0])
        amps = amps + (20.0 * np.log10(first_bin) - amps[0])
        return freqs, 10.0 ** (amps / 20.0)

    # -- status ------------------------------------------------------------

    def refresh_status(self, log=None):
        """Read ERRS and FFTS once and cache the decoded result.

        **Reading a status byte clears it**, so whoever reads first consumes the
        flag and everyone after them sees a clean instrument. That made the
        overload check a race: report_status() after a settings apply, or a
        second caller wanting the same answer, would swallow the bit the capture
        was about to look for. So there is exactly one read, here, and
        error_byte(), overload() and status_line() all report from the cache.

        start() calls this to consume whatever was latched before the run - the
        read is what clears it - keeps the result in pre_start(), and then drops
        the cache, because a flag raised before a run says nothing about the run.
        If nothing has refreshed since, overload() refreshes rather than
        returning a pre-capture value - stale-but-plausible is the failure this
        exists to stop.

        The whole byte is read rather than a single bit: it costs the same two
        queries and keeps every other bit available for the caller who wants it.
        """
        errs = ffts = None
        for query, slot in (("ERRS?", "errs"), ("FFTS?", "ffts")):
            try:
                value = int(self.get(query))
            except Exception:
                self.recover()
                value = None
            if slot == "errs":
                errs = value
            else:
                ffts = value
        self._status = Status(errs=errs, ffts=ffts, at=time.perf_counter())
        if log is not None and self._status.overloaded:
            log("  *** OVERLOAD flagged. The front end saturated; content "
                "outside the span can do that without showing on the trace. ***")
        return self._status

    def status(self, refresh_if_stale=True):
        """The cached status, refreshing first if nothing has read since the
        last start()."""
        if self._status is None and refresh_if_stale:
            self.refresh_status()
        return self._status

    def invalidate_status(self):
        """Forget the cached flags. Called by start(); call it yourself after
        anything that makes the previous read irrelevant."""
        self._status = None

    def error_byte(self):
        """SR760 error status byte, from the cache. The manual's ERRS table
        names the individual bits; bit 7 is the input overload."""
        st = self.status()
        return 0 if st is None or st.errs is None else st.errs

    def overload(self):
        """(ERRS bit 7, FFTS bit 5) from the cache, refreshing if stale.

        Worth asking after every average, not just while ranging. The input
        stage sees the whole band the anti-alias filter passes, so content
        outside the span can overload it while the displayed trace looks
        perfectly clean - a servo bump at 150-300 kHz, or RF on an unterminated
        line, do exactly that. A trace taken through a saturated front end is
        not a measurement of anything, and nothing on the screen says so.
        """
        st = self.status()
        if st is None:
            return None, None
        return st.errs_bit(ERRS_OVERLOAD_BIT), st.ffts_bit(FFTS_OVERLOAD_BIT)

    def status_line(self):
        """One line about anything the analyser is complaining about, or "".

        Bit 7 is the input overload the ranging routine watches; it says nothing
        about the commands just sent, so it is called out separately rather than
        read as a rejection.
        """
        err = self.error_byte()
        if not err:
            return ""
        bits = [i for i in range(8) if err & (1 << i)]
        if bits == [ERRS_OVERLOAD_BIT]:
            return ("input overload flagged - ERRS bit 7. Not a rejected "
                    "command; check the input range.")
        return f"ERRS = {err}, bits {bits} set - see the ERRS table in the manual"

    # -- settings ----------------------------------------------------------

    def command(self, key, value):
        """The write for a setting, in the spelling its query turned out to use.
        Falls back to the likelier spelling for a setting that has never been
        read back successfully."""
        s = BY_KEY[key]
        return s.writes[self.qform.get(key, 0)].format(v=value)

    def query_for(self, key):
        """The read for a setting, in the spelling that answered last time."""
        s = BY_KEY[key]
        return s.queries[self.qform.get(key, 0)]

    def read_settings(self, *keys):
        """A few settings rather than all of them, each in the query form it
        turned out to answer.

        A query that fails falls back to the last value this session read
        successfully, so a decision made on the answer rests on a stale reading
        rather than on a default that happens to be wrong.
        """
        out = {}
        for key in keys:
            try:
                out[key] = self._remember(key, self.get(self.query_for(key)))
                continue
            except Exception:
                self.recover()
            if key in self.last_raw:
                out[key] = self.last_raw[key]
        return out

    def read_all_settings(self, retry_all=False, log=None):
        """Every setting the model knows, as {key: raw reply}.

        Each is asked with the query form that worked last time, or with each
        candidate form in turn the first time round. A setting nothing answers
        costs a whole VISA timeout per form, so one that fails twice is dropped
        until the next explicit retry_all - it stays editable and writable, it
        just cannot be read back.
        """
        say = log if log is not None else (lambda _msg: None)
        if retry_all:
            self.dead.clear()
            self.qform.clear()
        values = {}
        previous = self.inst.timeout
        self.inst.timeout = SETTINGS_TIMEOUT_MS
        try:
            for s in ALL_SETTINGS:
                if self.dead.get(s.key, 0) >= 2:
                    continue
                known = self.qform.get(s.key)
                tries = (s.queries[known],) if known is not None else s.queries
                failure = ""
                for query in tries:
                    try:
                        values[s.key] = self._remember(s.key, self.get(query))
                    except Exception as exc:
                        self.recover()
                        failure = f"{query} failed: {exc}"
                        continue
                    if known is None:
                        self.qform[s.key] = s.queries.index(query)
                        if query != s.queries[0]:
                            say(f"  ({s.label} answers {query}, "
                                f"not {s.queries[0]})")
                    self.dead[s.key] = 0
                    break
                else:
                    self.dead[s.key] = self.dead.get(s.key, 0) + 1
                    say(f"  {failure}")
                    if self.dead[s.key] >= 2:
                        say(f"  ({s.label} cannot be read back - leaving it "
                            f"write-only until the next Read)")
        finally:
            self.inst.timeout = previous
        return values

    def _remember(self, key, raw):
        self.last_raw[key] = raw
        return raw

    def write_settings(self, changes, log=None, hold_resets=True):
        """Write {key: code}, each in the spelling that key answers to.

        SPAN goes first and everything in SPAN_RESETS goes last, because a
        SPAN write silently reinstalls that span's default overlap. Passing
        {"OVLP": "0", "SPAN": "11"} in that order used to leave 98.44 % behind,
        and NAVG then counted records that were 98 % the same samples - an
        error bar eight times better than the data earned, with nothing on
        screen to say so. That ordering is not optional and applies either way.

        `hold_resets` decides only what happens where SPAN moves and OVLP is
        NOT alongside it. Holding reads the value the analyzer has and puts it
        back, so a span sweep cannot change what the averaging is worth. Letting
        it go leaves the span's default, which is a real choice and not a bug:
        at OVLP 0 every record is fresh samples and NAVG records cost NAVG
        record lengths, while at a span's default they advance 16 ms apiece and
        the same NAVG costs a fraction of the time for a fraction of the
        independent records. record_stats counts what actually arrived either
        way, so the trade is priced rather than hidden - which is what makes it
        safe to offer.

        A held value that cannot be read back is not guessed at: the re-assert
        is skipped and said so.

        Returns every command sent, re-asserts included. The caller is still
        expected to read back - the analyzer clamps a value it dislikes and
        says nothing.
        """
        say = log if log is not None else (lambda _msg: None)
        wanted = dict(changes)
        after = {}
        if "SPAN" in wanted:
            for key in SPAN_RESETS:
                if key in wanted:
                    after[key] = wanted.pop(key)
                    continue
                if not hold_resets:
                    say(f"  ({key} left to the span default, as asked)")
                    continue
                held = self.read_settings(key).get(key)
                if held is None:
                    say(f"  ({key} could not be read back, so the SPAN write "
                        f"leaves the span default in place - check it)")
                    continue
                after[key] = str(held).strip()
                say(f"  (holding {key}={after[key]} across the SPAN write)")

        ordered = {}
        if "SPAN" in wanted:
            ordered["SPAN"] = wanted.pop("SPAN")
        ordered.update(wanted)
        ordered.update(after)

        sent = []
        for key, value in ordered.items():
            cmd = self.command(key, value)
            self.put(cmd)
            sent.append(cmd)
            say(f"  {cmd}")
        return sent

    def apply(self, preset, log=None):
        """Write a whole preset. `PRESETS['protocol']` is the usual argument."""
        return self.write_settings(dict(preset), log=log)

    def average_finishes(self):
        """Whether an average started now will ever report itself finished, and
        a short phrase naming the averaging.

        Bit 0 of the poll byte sets when a linear average reaches its count. An
        exponential average never reaches one - it goes on re-weighting the
        newest record forever - and with averaging off nothing is counting at
        all, so in both cases the bit wait_done watches for is never coming and
        the wait can only end at the timeout or at Stop.

        Asked of the analyser rather than assumed, because the averaging mode is
        one knob turn away on the front panel and this is the difference between
        a wait that ends and one that does not.
        """
        snap = self.read_settings("AVGO", "AVGM", "NAVG")
        if code_of(snap, "AVGO", 0) != 1:
            return False, "no averaging"
        if code_of(snap, "AVGM", 0) != 0:
            return False, "exponential averaging"
        n = code_of(snap, "NAVG")
        return True, f"{n} linear averages" if n else "linear averaging"

    # -- timing ------------------------------------------------------------

    def dwell(self, seconds, stop=None, poll=0.25):
        """Sleep in short steps so a stop request is felt promptly.

        Answers in wait_done's vocabulary - 'done' or 'stopped' - so the two are
        interchangeable at the call sites.
        """
        deadline = time.time() + seconds
        while True:
            left = deadline - time.time()
            if left <= 0:
                return "done"
            if stop is not None and stop():
                return "stopped"
            time.sleep(min(poll, left))

    def settle(self, recs, span_code, stop=None, log=None):
        """Wait out the decimation chain after a change of span, start
        frequency, range or coupling, and say what was waited.

        In record lengths, because that is what the analyser's settling actually
        scales with: at the 191 mHz span a record is 35 minutes and at 100 kHz
        it is 4 ms, so a fixed number of seconds is either uselessly long at one
        end or no wait at all at the other.

        wait_ready() does not cover this. That waits for the analyser to answer
        a query again, which it does perfectly happily while its filters are
        still full of the previous span's data.

        Raises KeyboardInterrupt if `stop` fires, matching the capture path.
        """
        say = log if log is not None else (lambda _msg: None)
        t_rec = record_time(span_code)
        if not (recs > 0) or not np.isfinite(t_rec):
            return {"settle": "none"}
        seconds = recs * t_rec
        say(f"  settling {recs:g} record lengths = {seconds:.3g} s "
            f"(T_rec {t_rec:.4g} s)")
        if self.dwell(seconds, stop=stop) == "stopped":
            raise KeyboardInterrupt
        return {"settle": f"{recs:g} record lengths = {seconds:.4g} s "
                          f"(T_rec {t_rec:.4g} s)"}

    # -- readout -----------------------------------------------------------

    def trace(self, trace=TRACE, n_bins=N_BINS, snap=None, prefer_binary=True,
              progress=None, log=None):
        """The trace, by whichever readout is valid for the current settings.

        `snap` is a settings snapshot; without one it is read here. The display
        mode decides whether the binary dump is usable and the units decide how
        it has to be rebased, so both come from the same snapshot the caller
        will put in the metadata.

        Returns (freqs, amps, used_binary).
        """
        say = log if log is not None else (lambda _msg: None)
        if snap is None:
            snap = self.read_all_settings()
        binary = prefer_binary and binary_valid(snap)
        if prefer_binary and not binary:
            say(f"  ({binary_refusal(snap)})")
        if binary:
            try:
                freqs, amps = self.trace_binary(trace, n_bins, reads_in_db(snap))
                return freqs, amps, True
            except ValueError as exc:
                say(f"  ({exc} - reading bin by bin instead)")
        freqs, amps = self.trace_ascii(trace, n_bins, progress=progress)
        return freqs, amps, False

    # -- context manager ---------------------------------------------------

    def __enter__(self):
        if self.inst is None:
            self.connect(self.addr or None)
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# The class was called Analyzer while it lived in the panel. Anything that
# imports the old name keeps working.
Analyzer = SR760


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def write_csv(path, freqs, amps, ylabel):
    """Two columns, headed the way the trace was actually measured rather than
    with a fixed 'dbV' that goes stale as soon as the units change."""
    rows = np.column_stack([freqs, amps])
    np.savetxt(path, rows, delimiter=",", comments="",
               header=f"Frequency (Hz),{safe_name(ylabel)}")


def read_csv(path):
    """A capture back off disk as (freqs, amps, ylabel).

    The counterpart of write_csv, and in this file for the same reason it is:
    the header names the scale the trace was measured on, and reading it back is
    the only way anything downstream can know. The .npy matrices a sweep writes
    do not carry it, so a comparison across sessions has to come through here.

    A file with no header, or one naming a scale this module does not know, is
    still loaded - the label comes back as it was written, or empty, and the
    caller decides what that is worth.
    """
    with open(path, encoding="utf-8") as fh:
        first = fh.readline().strip()
    fields = first.split(",")
    try:                                  # a header is a line that is not data
        float(fields[0])
        ylabel, skip = "", 0
    except (ValueError, IndexError):
        ylabel = fields[1].strip() if len(fields) > 1 else ""
        skip = 1
    rows = np.loadtxt(path, delimiter=",", skiprows=skip, ndmin=2)
    if rows.shape[0] == 0 or rows.shape[1] < 2:
        raise ValueError(f"{os.path.basename(path)} has no two-column data")
    return rows[:, 0], rows[:, 1], ylabel


def metadata_text(an, snap, extra, command):
    """The capture described by the same settings snapshot the panel is showing,
    so the two can never disagree about what the analyzer was doing. Each
    setting is followed by the command that would put it back, in the spelling
    this analyzer turned out to use."""
    lines = [
        f"captured             : {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"instrument           : {an.idn}",
        f"visa address         : {an.addr}",
    ]
    for label, value in extra.items():
        lines.append(f"{label:<21}: {value}")
    lines.append("")
    lines.append("analyzer settings")
    for group, settings in SETTING_GROUPS:
        # The header only goes in if something under it was read. A group whose
        # settings all went dead used to leave a bare [Display] with nothing
        # beneath it, which reads as though the analyzer answered and had
        # nothing to say rather than as though it never answered.
        rows = [(s, snap[s.key]) for s in settings if snap.get(s.key) is not None]
        if not rows:
            continue
        lines.append(f"  [{group}]")
        for s, raw in rows:
            lines.append(f"    {s.label:<17}: {fmt_setting(s, raw):<14} "
                         f"({command(s.key, raw)})")
    return "\n".join(lines) + "\n"
