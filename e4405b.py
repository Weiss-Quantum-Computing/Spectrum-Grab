"""
Control for the Agilent / HP E4405B ESA-E spectrum analyser over GPIB.

    from e4405b import E4405B

    with E4405B() as an:                      # auto-finds the ESA on GPIB
        print(an.idn)
        an.write_settings({"FSTART": "1e6", "FSTOP": "1e9", "RBW": "1e5"})
        an.start()                            # one sweep, or one whole average
        an.wait_done(60)
        status = an.refresh_status()
        f, a = an.trace()

The sibling of sr760.py, in the same shape: the instrument layer AND the
scripting library, with the panel (esa_panel.py, reached through
spectrum_grab.py) importing it rather than carrying its own copy. The file
helpers - write_csv, read_csv, safe_name, unique_base - are sr760's, imported
here, so a capture from either analyser lands in the same format under the same
naming rules. sr760.py therefore has to sit beside this file.

Nothing here imports tkinter or matplotlib, and nothing here draws.

Things about this instrument that the code encodes and that are easy to "fix"
back into bugs:

* **Traces are read in ASCII, on purpose.** Measured 18 Nov 2026 on s/n
  MY41440490 at GPIB0::18: every binary format (REAL,32 / REAL,64 / INT,32)
  arrives with bit 7 of every byte cleared - -55.763 dBm comes back as
  +55.763, because 0xC2 lands as 0x42. ASCII is seven-bit and untouched. The
  SR760 on the same GPIB board transfers binary correctly, so the fault is in
  the DIO8 line of this analyser's cable or its own GPIB port, not in VISA.
  Nothing about a REAL,32 read fails - the numbers are simply wrong - and
  ASCII costs 0.05 s for 401 points, so there is no binary path here at all.
* **`*OPC` only works straight after `:INIT:IMM`** (or a calibration), so
  start() sends them as one message and wait_done() polls `*ESR?` for bit 0.
  In single sweep with averaging on, one :INIT:IMM takes AVER:COUNt sweeps
  before that bit sets (measured: 10 averages of a 33 ms sweep, 2.2 s).
* **The status registers that matter are condition registers three levels
  down**: Meas Uncal is QUES:INT:UNC bit 0, IF/ADC over range is QUES:INT bit
  4, Align Needed is QUES:CAL bit 14. See `Status`.
* `*OPT?` and `:SYST:KLOC` do not exist on an ESA - both time out and leave
  -113 in the error queue. Options are `:SYST:OPT?`; there is no keyboard lock.
* Writing a bandwidth, sweep time or attenuation switches its auto-coupling
  off; write_settings sends the auto switches first so a value sent alongside
  one wins, and the panel reads everything back afterwards.

Requires: pyvisa + a VISA runtime with GPIB (NI-488.2 on this machine), numpy.
"""

from __future__ import annotations

import datetime
import re
import time
from collections import namedtuple

import numpy as np
import pyvisa

# The file format and the naming rules are shared with the SR760 side, so a
# capture from either analyser reads back through the same code.
from sr760 import (BAD_NAME_CHARS, MAX_LIST_ITEMS, SPACE_OWNERS,  # noqa: F401
                   floor_fault, fmt_hms, read_csv, safe_name, unique_base,
                   write_csv)

__all__ = [
    "E4405B", "Status", "Setting", "MODELS", "DEFAULT_ADDRESS",
    "CONNECT_TIMEOUT_MS", "OP_TIMEOUT_MS", "SETTINGS_TIMEOUT_MS",
    "MIN_FREQ", "MAX_FREQ", "MAX_SEGMENTS", "SWEEP_OVERHEAD_S",
    "SETTING_GROUPS", "ALL_SETTINGS", "BY_KEY", "fmt_setting",
    "parse_setting", "value_of", "token_of", "label_of", "flag_of",
    "trace_units", "pretty_units", "canonical_units", "trace_yscale",
    "fmt_hz", "parse_hz", "parse_segments", "tile_segments", "sweep_count",
    "capture_time", "sweep_fault", "flat_fault", "band_fault",
    "readout_fault", "zero_span", "resolution_note", "plot_bits",
    "metadata_text", "floor_fault", "fmt_hms", "read_csv", "safe_name",
    "unique_base", "write_csv", "BAD_NAME_CHARS", "SPACE_OWNERS",
]

DEFAULT_ADDRESS = "GPIB0::18::INSTR"
CONNECT_TIMEOUT_MS = 5000
OP_TIMEOUT_MS = 20000
SETTINGS_TIMEOUT_MS = 3000

# The ESA-E family answers the same commands; a scan claims any of them.
MODELS = ("E4401B", "E4402B", "E4404B", "E4405B", "E4407B")

# The E4405B's band. DC coupling reaches 9 kHz, AC coupling 100 kHz; the
# analyser tunes to 0 Hz either way and simply is not specified down there.
MIN_FREQ = 0.0
MAX_FREQ = 13.2e9

# Most segments one sweep will take. Same reasoning as sr760.MAX_LIST_ITEMS: a
# pasted column of numbers is refused in one line rather than run blindly.
MAX_SEGMENTS = MAX_LIST_ITEMS

# What a sweep costs beyond its sweep time: retrace, band switching and the
# bus. Measured at full span (0 to 13.2 GHz, four bands): 0.42 s for a single
# 33 ms sweep and 2.2 s for ten averaged, so about 0.2 s a sweep. Only used to
# say roughly how long a run will take, never to decide when it has finished.
SWEEP_OVERHEAD_S = 0.2


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------

# `kind` and `choices` carry the same meaning as in sr760.Setting, which is
# what lets the panel's settings grid be built by the same code:
#   num  - free-form number, written as typed
#   enum - `choices` are the labels the panel shows, `tokens` the SCPI word to
#          write for each, and `aliases` any other spelling the analyser may
#          answer with ("1" for ON, LPOW for VID)
Setting = namedtuple("Setting", "label key scpi kind choices tokens aliases")


def num(label, key, scpi):
    return Setting(label, key, scpi, "num", None, None, None)


def enum(label, key, scpi, pairs, aliases=None):
    labels = tuple(p[0] for p in pairs)
    tokens = tuple(p[1] for p in pairs)
    return Setting(label, key, scpi, "enum", labels, tokens,
                   dict(aliases or {}))


OFF_ON = (("Off", "0"), ("On", "1"))
BOOL_ALIASES = {"OFF": "0", "ON": "1"}

SETTING_GROUPS = [
    ("Frequency", [
        num("Start (Hz)", "FSTART", "FREQ:STAR"),
        num("Stop (Hz)", "FSTOP", "FREQ:STOP"),
        num("Center (Hz)", "FCENT", "FREQ:CENT"),
        num("Span (Hz)", "FSPAN", "FREQ:SPAN"),
        # 101 to 8192. Changing it erases the trace on the screen.
        num("Points", "POINTS", "SWE:POIN"),
    ]),
    ("Bandwidth and sweep", [
        num("Res BW (Hz)", "RBW", "BAND"),
        enum("Res BW auto", "RBWAUTO", "BAND:AUTO", OFF_ON, BOOL_ALIASES),
        num("Video BW (Hz)", "VBW", "BAND:VID"),
        enum("Video BW auto", "VBWAUTO", "BAND:VID:AUTO", OFF_ON,
             BOOL_ALIASES),
        num("Sweep time (s)", "SWT", "SWE:TIME"),
        enum("Sweep time auto", "SWTAUTO", "SWE:TIME:AUTO", OFF_ON,
             BOOL_ALIASES),
        enum("Sweep", "CONT", "INIT:CONT",
             (("Single", "0"), ("Continuous", "1")), BOOL_ALIASES),
    ]),
    ("Amplitude", [
        num("Ref level", "RLEV", "DISP:WIND:TRAC:Y:RLEV"),
        num("Attenuation (dB)", "ATT", "POW:ATT"),
        enum("Atten auto", "ATTAUTO", "POW:ATT:AUTO", OFF_ON, BOOL_ALIASES),
        # UNIT:POW decides the scale the trace comes back on, whatever the
        # display is doing - the same rule as the SR760's UNIT.
        enum("Y units", "YUNIT", "UNIT:POW",
             (("dBm", "DBM"), ("dBmV", "DBMV"), ("dBuV", "DBUV"),
              ("dBuA", "DBUA"), ("V", "V"), ("W", "W"), ("A", "A"))),
        enum("Scale", "YSPAC", "DISP:WIND:TRAC:Y:SPAC",
             (("Log", "LOG"), ("Linear", "LIN"))),
        num("dB / div", "PDIV", "DISP:WIND:TRAC:Y:PDIV"),
        enum("Coupling", "COUP", "INP:COUP", (("AC", "AC"), ("DC", "DC"))),
    ]),
    ("Detector and averaging", [
        # No "Normal" on the bus: DET NORM is accepted and reads back POS.
        enum("Detector", "DET", "DET",
             (("Peak", "POS"), ("Neg peak", "NEG"), ("Sample", "SAMP"),
              ("Average", "AVER")), {"RMS": "AVER"}),
        enum("Detector auto", "DETAUTO", "DET:AUTO", OFF_ON, BOOL_ALIASES),
        enum("Averaging", "AVG", "AVER", OFF_ON, BOOL_ALIASES),
        num("Average count", "NAVG", "AVER:COUN"),
        # Video averages the log trace, which reads a noise floor 2.51 dB low;
        # RMS averages power, which is the one that means something on noise.
        enum("Average type", "AVGTYPE", "AVER:TYPE",
             (("Video (log)", "VID"), ("RMS (power)", "RMS")),
             {"LPOW": "VID", "LOG": "VID", "POW": "RMS", "LIN": "RMS"}),
        enum("Trace 1", "TRMODE", "TRAC1:MODE",
             (("Clear write", "WRIT"), ("Max hold", "MAXH"),
              ("Min hold", "MINH"), ("View", "VIEW"), ("Blank", "BLAN"))),
    ]),
]
ALL_SETTINGS = [s for _, group in SETTING_GROUPS for s in group]
BY_KEY = {s.key: s for s in ALL_SETTINGS}

# Written before everything else, so that a value sent in the same apply as its
# own auto switch ends up in force: BAND 1e3 turns BAND:AUTO off by itself, and
# BAND:AUTO ON sent after it would take the value straight back.
AUTO_KEYS = ("RBWAUTO", "VBWAUTO", "SWTAUTO", "ATTAUTO", "DETAUTO")

# The units whose numbers are decibels, and so go on a linear axis.
DB_UNITS = ("dBm", "dBmV", "dBuV", "dBuA")


def token_of(snap, key, default=None):
    """An enum's reply reduced to the token this module writes for it, so
    'ON', '1' and '+1' are one answer. None-safe; `default` when the snapshot
    has nothing recognisable under that key."""
    s = BY_KEY.get(key)
    raw = snap.get(key) if snap else None
    if s is None or s.kind != "enum" or raw is None:
        return default
    word = str(raw).strip().strip('"').upper()
    if word in s.tokens:
        return word
    if word in s.aliases:
        return s.aliases[word]
    try:                                   # "+1", "1.0"
        as_int = str(int(float(word)))
    except ValueError:
        return default
    return as_int if as_int in s.tokens else default


def label_of(snap, key, default=""):
    """The panel's wording for an enum reply, or `default`."""
    token = token_of(snap, key)
    if token is None:
        return default
    s = BY_KEY[key]
    return s.choices[s.tokens.index(token)]


def flag_of(snap, key, default=None):
    """An Off/On setting as a bool, or `default` if it was not read."""
    token = token_of(snap, key)
    return default if token is None else token == "1"


def value_of(snap, key, default=None):
    """One numeric setting out of a snapshot as a float."""
    try:
        return float(snap[key])
    except (KeyError, TypeError, ValueError):
        return default


def fmt_setting(s, raw):
    """An analyser reply as the panel displays it. An unexpected reply is
    shown as it arrived rather than swallowed."""
    raw = (raw or "").strip()
    if s.kind == "enum":
        token = token_of({s.key: raw}, s.key)
        return raw if token is None else s.choices[s.tokens.index(token)]
    try:
        # .12g: a 13.2 GHz stop frequency set to the hertz has eleven digits,
        # and %g would show 1.32e+10 for anything near it.
        return f"{float(raw):.12g}"
    except ValueError:
        return raw


def parse_setting(s, shown):
    """Panel text back to what the analyser is sent. Raises ValueError on
    anything that is not a listed choice or a number. A number may carry a
    k / M / G suffix, since nobody types 13200000000 correctly."""
    shown = shown.strip()
    if s.kind == "enum":
        return s.tokens[s.choices.index(shown)]
    return f"{parse_hz(shown):.12g}"


# ---------------------------------------------------------------------------
# Frequencies and segments
# ---------------------------------------------------------------------------

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_SUFFIX = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}


def parse_hz(text):
    """'1.5M', '100k', '2.4GHz', '1e6' or '915000000' as a float.

    The suffix is case-blind and M is always mega: nothing on this analyser is
    set in millihertz, and 'm' typed for MHz is the common slip."""
    m = re.fullmatch(rf"\s*({_NUMBER})\s*([kKmMgG]?)\s*(?:[hH][zZ])?\s*",
                     str(text))
    if not m:
        raise ValueError(f"'{str(text).strip()}' is not a number")
    return float(m.group(1)) * _SUFFIX[m.group(2).lower()]


def fmt_hz(hz):
    """A frequency for a title or a log line: 13.2 GHz, 100 kHz, 30 Hz."""
    if hz is None or not np.isfinite(hz):
        return "?"
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if abs(hz) >= scale:
            return f"{hz / scale:.6g} {unit}"
    return f"{hz:.6g} Hz"


def parse_segments(text):
    """'0-3G, 3G-6.7G; 6.7G:13.2G' as [(start, stop), ...] in Hz.

    Segments are separated by commas or semicolons; start and stop by '-',
    ':' or 'to'. An empty box gives [], which the panel reads as 'one capture
    where the analyser is'."""
    out = []
    for part in re.split(r"[;,]", text or ""):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(.*?[0-9.kKmMgGzZ])\s*(?:-|:|\bto\b)\s*(.+)", part)
        if not m:
            raise ValueError(f"'{part}' is not start-stop")
        start, stop = parse_hz(m.group(1)), parse_hz(m.group(2))
        if not stop > start:
            raise ValueError(f"'{part}': the stop has to be above the start")
        if start < MIN_FREQ or stop > MAX_FREQ:
            raise ValueError(f"'{part}' is outside {fmt_hz(MIN_FREQ)} to "
                             f"{fmt_hz(MAX_FREQ)}")
        out.append((start, stop))
        if len(out) > MAX_SEGMENTS:
            raise ValueError(f"more than {MAX_SEGMENTS} segments")
    return out


def tile_segments(start, stop, n):
    """`n` equal segments covering start to stop, end to end.

    A band cut into n segments is swept with n times the points, which is how
    a wide survey gets a point spacing down to the resolution bandwidth
    without asking one sweep for more than it holds. Neighbours share their
    edge frequency exactly, so the joined curve has no gap and one doubled
    point at each join."""
    n = int(n)
    if n < 1:
        raise ValueError("at least one segment")
    if n > MAX_SEGMENTS:
        raise ValueError(f"more than {MAX_SEGMENTS} segments")
    if not stop > start:
        raise ValueError("the stop has to be above the start")
    edges = np.linspace(start, stop, n + 1)
    return [(float(edges[i]), float(edges[i + 1])) for i in range(n)]


# ---------------------------------------------------------------------------
# What a snapshot means
# ---------------------------------------------------------------------------

def trace_units(snap):
    """Y-axis label the trace comes back in, in plain ASCII - it goes in the
    CSV header and the metadata as well as on the plot.

    UNIT:POW alone decides it: `:TRAC?` in ASCII answers in the current
    amplitude units whatever the display scale is. An unread unit is reported
    as dBm, the preset, and readout_fault says it was assumed."""
    return label_of(snap, "YUNIT", "dBm")


def pretty_units(label):
    return label


def canonical_units(label):
    """A units label read back off a CSV header, in trace_units' spelling."""
    label = (label or "").strip()
    for known in BY_KEY["YUNIT"].choices:
        if label.lower() == known.lower():
            return known
    return label


def trace_yscale(snap):
    """'linear' for a decibel trace, 'log' for volts, watts or amps - a
    linear-unit spectrum spans decades and is unreadable on a linear axis."""
    return "linear" if trace_units(snap) in DB_UNITS else "log"


def readout_fault(snap):
    """Why the label on the trace may be wrong, or ''."""
    if token_of(snap, "YUNIT") is None:
        return ("the amplitude units were not read back, so the trace is "
                "labelled dBm on assumption")
    return ""


def sweep_count(snap):
    """How many sweeps one :INIT:IMM takes: the average count with averaging
    on, otherwise one. Unreadable averaging counts as off."""
    if flag_of(snap, "AVG", False):
        n = value_of(snap, "NAVG")
        if n and n >= 1:
            return int(n)
    return 1


def capture_time(snap):
    """About how long one capture takes at these settings, in seconds, or nan
    when the sweep time was not read. An estimate for the log and the sweep
    ETA; wait_done is what decides when a run has actually finished."""
    swt = value_of(snap, "SWT")
    if swt is None:
        return float("nan")
    return sweep_count(snap) * (swt + SWEEP_OVERHEAD_S)


def sweep_fault(snap):
    """Why a triggered sweep would not have produced this trace, or ''.

    A trace in View or Blank is not being written by the sweep at all: the
    sweep runs, *OPC completes, and `:TRAC?` hands back whatever was frozen
    there - a capture that is perfectly well formed and measures nothing."""
    mode = token_of(snap, "TRMODE")
    if mode == "VIEW":
        return ("trace 1 is in View, so the sweep did not update it - this "
                "is the stored trace, not a measurement")
    if mode == "BLAN":
        return "trace 1 is blanked, so the sweep did not update it"
    return ""


def flat_fault(amps):
    """Why this trace is not a measurement of anything, or ''.

    A real sweep's points differ from one another, noise alone sees to that. A
    trace on which every point is the same number is one that was never
    written (a blanked or freshly cleared trace reads back as its initial
    fill) or one pinned against the edge of the amplitude range."""
    a = np.asarray(amps, dtype=float)
    if a.size > 1 and np.unique(a).size == 1:
        return (f"every point reads {a.flat[0]:.4g} - a flat trace is one "
                f"that was not written by a sweep, or is pinned at the edge "
                f"of the amplitude range")
    return ""


def band_fault(freqs, band):
    """Why the trace is not on the band a sweep asked for, or ''. The analyser
    clamps a start or stop it cannot reach and says nothing, and the files are
    named from the segment that was asked for."""
    if band is None or not len(freqs):
        return ""
    want_start, want_stop = band
    tol = 1e-6 * max(abs(want_stop - want_start), 1.0)
    if (abs(float(freqs[0]) - want_start) > tol
            or abs(float(freqs[-1]) - want_stop) > tol):
        return (f"asked for {fmt_hz(want_start)} to {fmt_hz(want_stop)}, the "
                f"analyser swept {fmt_hz(float(freqs[0]))} to "
                f"{fmt_hz(float(freqs[-1]))}")
    return ""


def resolution_note(snap):
    """What one trace point stands for, and whether the sweep can miss things.

    Each point is a bucket span/(points-1) wide. Wider than the resolution
    bandwidth, a peak detector still catches a narrow signal anywhere in the
    bucket, but Sample reads one instant per bucket and steps straight over
    it - a survey that looks clean because the carrier fell between points."""
    span, points = value_of(snap, "FSPAN"), value_of(snap, "POINTS")
    rbw = value_of(snap, "RBW")
    if not span or not points or points < 2 or not rbw:
        return ""
    bucket = span / (points - 1)
    text = f"{fmt_hz(bucket)} a point, RBW {fmt_hz(rbw)}"
    if bucket > rbw and token_of(snap, "DET") == "SAMP":
        text += (" - points are further apart than the RBW and the detector "
                 "is Sample, so a narrow signal between points is missed")
    return text


def plot_bits(snap, freqs=()):
    """The handful of settings that decide what the trace means, as short
    items for the line under a plot title."""
    bits = []
    if len(freqs):
        bits.append(f"{fmt_hz(float(freqs[0]))} to {fmt_hz(float(freqs[-1]))}")
    rbw, vbw = value_of(snap, "RBW"), value_of(snap, "VBW")
    if rbw:
        bits.append(f"RBW {fmt_hz(rbw)}")
    if vbw:
        bits.append(f"VBW {fmt_hz(vbw)}")
    det = label_of(snap, "DET")
    if det:
        bits.append(f"{det.lower()} detector")
    att = value_of(snap, "ATT")
    if att is not None:
        bits.append(f"{att:g} dB atten")
    if flag_of(snap, "AVG", False):
        n = value_of(snap, "NAVG")
        kind = {"VID": "video", "RMS": "RMS"}.get(token_of(snap, "AVGTYPE"), "")
        bits.append(" ".join(p for p in (f"{n:g}" if n else "", kind,
                                         "averages") if p))
    else:
        bits.append("no averaging")
    mode = token_of(snap, "TRMODE")
    if mode in ("MAXH", "MINH"):
        bits.append(label_of(snap, "TRMODE").lower())
    return bits


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

UNCAL_OVERSWEEP = 1 << 0        # QUES:INT:UNC  Oversweep (Meas Uncal)
INT_UNCAL_SUMMARY = 1 << 3      # QUES:INT      Data Uncalibrated summary
INT_ADC_OVER = 1 << 4           # QUES:INT      IF/ADC Over Range
INT_INVALID = 1 << 12           # QUES:INT      Invalid Data
FREQ_REF_UNLOCKED = 1 << 1      # QUES:FREQ     Freq Ref Unlocked
FREQ_SYNTH_UNLOCKED = 1 << 4    # QUES:FREQ     Synth Unlocked
POW_LO_UNLEVELED = 1 << 3       # QUES:POW      LO Unleveled
POW_50M_UNLEVELED = 1 << 4      # QUES:POW      50 MHz Osc Unleveled
CAL_FAILURES = 0x01FC           # QUES:CAL      bits 2-8, an alignment failed
CAL_RF_NEEDED = 1 << 12         # QUES:CAL      Align RF Now Needed
CAL_NEEDED = 1 << 14            # QUES:CAL      Align Needed

STATUS_QUERIES = (
    ("integ", "STAT:QUES:INT:COND?"),
    ("uncal", "STAT:QUES:INT:UNC:COND?"),
    ("freq", "STAT:QUES:FREQ:COND?"),
    ("power", "STAT:QUES:POW:COND?"),
    ("cal", "STAT:QUES:CAL:COND?"),
)


class Status(namedtuple("Status", "integ uncal freq power cal at")):
    """The questionable-status condition registers, read once after a sweep.

    These are condition registers: they describe the analyser as it stands,
    not something latched, so reading them clears nothing and they can be
    read as often as anyone likes. A register that did not answer is None,
    and `complete` says whether every one did - half a status is not a clean
    one, the same standard sr760.Status is held to.
    """
    __slots__ = ()

    @staticmethod
    def _has(value, mask):
        return None if value is None else bool(value & mask)

    @property
    def complete(self) -> bool:
        return None not in (self.integ, self.uncal, self.freq, self.power,
                            self.cal)

    @property
    def read(self) -> bool:
        return any(v is not None for v in (self.integ, self.uncal, self.freq,
                                           self.power, self.cal))

    @property
    def oversweep(self):
        """Meas Uncal: swept too fast for the span and bandwidths, so
        amplitudes read low and peaks are smeared."""
        direct = self._has(self.uncal, UNCAL_OVERSWEEP)
        if direct is not None:
            return direct
        return self._has(self.integ, INT_UNCAL_SUMMARY)

    @property
    def overloaded(self):
        """IF/ADC over range: the signal is past what the IF can pass."""
        return self._has(self.integ, INT_ADC_OVER)

    @property
    def unlocked(self):
        return self._has(self.freq, FREQ_REF_UNLOCKED | FREQ_SYNTH_UNLOCKED)

    @property
    def unleveled(self):
        return self._has(self.power, POW_LO_UNLEVELED | POW_50M_UNLEVELED)

    @property
    def invalid(self):
        return self._has(self.integ, INT_INVALID)

    @property
    def align_failed(self):
        return self._has(self.cal, CAL_FAILURES)

    @property
    def align_needed(self):
        return self._has(self.cal, CAL_NEEDED | CAL_RF_NEEDED)

    def faults(self):
        """Everything that makes the trace itself untrustworthy, as phrases.
        Alignment is not in here - see alignment()."""
        out = []
        if self.oversweep:
            out.append("Meas Uncal: swept too fast for this span and "
                       "bandwidth")
        if self.overloaded:
            out.append("IF/ADC over range: the input is overloading the "
                       "analyser")
        if self.unlocked:
            out.append("frequency reference or synthesizer unlocked")
        if self.unleveled:
            out.append("LO or 50 MHz oscillator unleveled")
        if self.invalid:
            out.append("the analyser flags the trace as not matching its "
                       "current state")
        if self.align_failed:
            out.append("an alignment has failed")
        if not self.complete:
            silent = [name for name in ("integ", "uncal", "freq", "power",
                                        "cal")
                      if getattr(self, name) is None]
            out.append("status unverified: " + ", ".join(silent)
                       + " did not answer")
        return out

    def alignment(self):
        """'' or a caution. Align Needed means the corrections in force were
        made at another temperature (auto align is off) or are defaults: the
        trace is real, its absolute amplitude is not to specification. That is
        a caution on every number rather than a fault in one capture, so it is
        reported on its own line instead of marking each trace SUSPECT."""
        if self.align_needed:
            return ("ALIGN NEEDED is flagged (auto align off, or default "
                    "data) - absolute amplitude is not to specification "
                    "until the analyser is aligned")
        return ""

    def describe(self) -> str:
        if not self.read:
            return "not read"
        faults = self.faults()
        return "; ".join(faults) if faults else "clean"


# ---------------------------------------------------------------------------
# Instrument layer
# ---------------------------------------------------------------------------

class E4405B:
    """The E4405B over GPIB. One VISA session, so every exchange the GUI makes
    is serialised through the worker thread."""

    def __init__(self, addr=None, resource_manager=None, connect=True,
                 log=None):
        """Same contract as sr760.SR760: connect=False builds the object
        without touching the bus, and a `resource_manager` that was handed in
        is never closed here."""
        self._given_rm = resource_manager
        self.rm = None
        self.inst = None
        self.idn = ""
        self.options = ""
        self.addr = addr or ""
        self.log = log if log is not None else (lambda _msg: None)
        self.dead = {}
        self.last_raw = {}
        self._status = None
        # Whether the analyser was sweeping continuously before the first
        # start() took it to single sweep, so resume() can put it back.
        self._was_continuous = None
        if connect:
            self.connect(addr)

    @property
    def connected(self) -> bool:
        return self.inst is not None

    def connect(self, addr=None):
        """Open the analyser. With no address the GPIB bus is scanned with
        `*IDN?` and nothing else - a query every instrument either answers or
        ignores, so the scan cannot change anything on the bus."""
        self.close()
        self.rm = self._given_rm or pyvisa.ResourceManager()
        if addr:
            candidates = [addr]
        else:
            candidates = [r for r in self.rm.list_resources()
                          if r.upper().startswith("GPIB")]
        if not candidates:
            raise RuntimeError("No GPIB resources found - check the cable and "
                               "that NI MAX sees the analyser.")
        problems = []
        for res in candidates:
            dev = None
            try:
                dev = self.rm.open_resource(res)
                dev.timeout = CONNECT_TIMEOUT_MS
                dev.write_termination = "\n"
                dev.read_termination = "\n"
                idn = dev.query("*IDN?").strip()
            except Exception as exc:
                problems.append(f"  {res}: {exc}")
                if dev is not None:
                    try:
                        dev.close()
                    except Exception:
                        pass
                continue
            known = any(m in idn.upper() for m in MODELS)
            if addr or known:
                if not known:
                    self.log(f"  ({res} answered '{idn}', which is not an "
                             f"ESA-E analyser - connected anyway because the "
                             f"address was given)")
                dev.timeout = OP_TIMEOUT_MS
                self.inst, self.idn, self.addr = dev, idn, res
                self.dead.clear()
                self._was_continuous = None
                try:
                    self.options = self.get("SYST:OPT?").strip('"')
                except Exception:
                    self.recover()
                    self.options = ""
                return idn
            problems.append(f"  {res}: not an ESA-E analyser ({idn})")
            dev.close()
        raise RuntimeError("No E4405B found.\n" + "\n".join(problems))

    def close(self):
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
        """Flush a half-finished exchange after a timeout."""
        try:
            self.inst.clear()
        except Exception:
            pass

    def errors(self, limit=20):
        """Drain the SCPI error queue; [] when it was empty. Each entry is the
        analyser's own text, e.g. '-222,"Data out of range"'."""
        out = []
        for _ in range(limit):
            try:
                reply = self.get("SYST:ERR?")
            except Exception:
                self.recover()
                break
            if reply.lstrip("+").startswith("0,") or not reply:
                break
            out.append(reply)
        return out

    def go_local(self):
        """Hand the front panel back. The analyser sits in remote, keys dead
        apart from Local, from the first command onwards."""
        try:
            self.inst.control_ren(pyvisa.constants.RENLineOperation
                                  .address_gtl)
        except Exception:
            pass

    # -- settings ----------------------------------------------------------

    def command(self, key, value):
        """The write that sets `key` to `value` (a raw reply or a token)."""
        s = BY_KEY[key]
        if s.kind == "enum":
            token = token_of({key: value}, key)
            value = token if token is not None else value
        else:
            try:
                value = f"{float(value):.12g}"
            except (TypeError, ValueError):
                pass
        return f"{s.scpi} {value}"

    def query_for(self, key):
        return BY_KEY[key].scpi + "?"

    def _remember(self, key, raw):
        self.last_raw[key] = raw
        return raw

    def read_settings(self, *keys):
        """A few settings rather than all of them. One that fails falls back
        to the last value this session read, as on the SR760."""
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
        """Every setting the model knows, as {key: raw reply}. A setting that
        fails twice is dropped until the next retry_all, so an option this
        unit lacks costs two timeouts a session rather than one a read."""
        say = log if log is not None else (lambda _msg: None)
        if retry_all:
            self.dead.clear()
        values = {}
        previous = self.inst.timeout
        self.inst.timeout = SETTINGS_TIMEOUT_MS
        try:
            for s in ALL_SETTINGS:
                if self.dead.get(s.key, 0) >= 2:
                    continue
                try:
                    values[s.key] = self._remember(
                        s.key, self.get(self.query_for(s.key)))
                    self.dead[s.key] = 0
                except Exception as exc:
                    self.recover()
                    self.dead[s.key] = self.dead.get(s.key, 0) + 1
                    say(f"  {self.query_for(s.key)} failed: {exc}")
                    if self.dead[s.key] >= 2:
                        say(f"  ({s.label} cannot be read back - leaving it "
                            f"write-only until the next Read)")
        finally:
            self.inst.timeout = previous
        # A failed query leaves -420 Query UNTERMINATED behind; clear it so
        # the next write's error check reports only its own.
        if len(values) < len(ALL_SETTINGS):
            self.errors()
        return values

    def _ordered(self, changes):
        """`changes` in the order they have to be sent.

        Auto switches first (see AUTO_KEYS). Start and stop in the order that
        never crosses: the analyser will not hold a start above its stop - it
        drags the other edge along - so moving a band upward has to send the
        stop first and moving it downward the start first. Points before the
        frequencies changes nothing, so it keeps its place."""
        wanted = dict(changes)
        ordered = {k: wanted.pop(k) for k in AUTO_KEYS if k in wanted}
        if "FSTART" in wanted and "FSTOP" in wanted:
            try:
                new_start = float(wanted["FSTART"])
                now = self.read_settings("FSTOP")
                stop_now = float(now.get("FSTOP", "nan"))
            except (TypeError, ValueError):
                new_start, stop_now = 0.0, float("inf")
            pair = (("FSTOP", "FSTART") if new_start >= stop_now
                    else ("FSTART", "FSTOP"))
            rest = {k: v for k, v in wanted.items() if k not in pair}
            for key in pair:
                ordered[key] = wanted[key]
            ordered.update(rest)
        else:
            ordered.update(wanted)
        return ordered

    def write_settings(self, changes, log=None):
        """Write {key: value} and report what the analyser said about it.

        Returns (sent, errors): every command sent, and whatever the writes
        left in the error queue. The queue is cleared first so the errors are
        these writes' own. The caller is still expected to read back - a value
        out of range is clamped, with an error, and a coupled setting moves
        without one."""
        say = log if log is not None else (lambda _msg: None)
        self.put("*CLS")
        sent = []
        for key, value in self._ordered(changes).items():
            cmd = self.command(key, value)
            self.put(cmd)
            sent.append(cmd)
            say(f"  {cmd}")
        errors = self.errors()
        for err in errors:
            say(f"  (the analyser reports {err})")
        self.invalidate_status()
        return sent, errors

    def apply(self, preset, log=None):
        return self.write_settings(dict(preset), log=log)

    def set_band(self, start, stop, log=None):
        """Move the sweep to start..stop, in Hz."""
        return self.write_settings({"FSTART": f"{start:.12g}",
                                    "FSTOP": f"{stop:.12g}"}, log=log)

    # -- acquisition ------------------------------------------------------

    def start(self, log=None):
        """Take the analyser to single sweep and trigger one measurement: one
        sweep, or with averaging on the whole average.

        `*CLS` first, so the operation-complete bit wait_done polls for cannot
        be one left over from before. `:INIT:IMM;*OPC` goes as a single
        message because *OPC is only honoured immediately after :INIT:IMM.
        What the sweep mode was is remembered for resume()."""
        if self._was_continuous is None:
            try:
                self._was_continuous = self.get("INIT:CONT?").lstrip("+") \
                    in ("1", "ON")
            except Exception:
                self.recover()
                self._was_continuous = None
        self.invalidate_status()
        self.put("*CLS")
        self.put("INIT:CONT OFF")
        self.put("INIT:IMM;*OPC")

    def wait_done(self, timeout, stop=None, poll=0.1):
        """Wait for the triggered measurement to finish. Returns 'done',
        'timeout', 'stopped' or 'error'.

        Polls `*ESR?` for bit 0 rather than blocking on `*OPC?`, so Stop is
        felt within a poll and a long average cannot outrun the VISA timeout.
        Reading ESR clears it, which is fine: nothing else here uses it."""
        deadline = time.time() + timeout
        misses = 0
        while time.time() < deadline:
            if stop is not None and stop():
                return "stopped"
            try:
                if int(float(self.get("*ESR?"))) & 1:
                    return "done"
                misses = 0
            except Exception:
                self.recover()
                misses += 1
                if misses >= 3:
                    return "error"
            time.sleep(poll)
        return "timeout"

    def abort(self):
        """Stop the sweep in progress. Used when a wait is abandoned, so a
        thousand-sweep average does not go on holding the analyser."""
        try:
            self.put("ABOR")
        except Exception:
            pass

    def resume(self):
        """Put continuous sweep back if start() took it away. Returns whether
        it did. With averaging on this restarts the average on the screen;
        the trace already read is not affected."""
        if self._was_continuous:
            self.put("INIT:CONT ON")
            self._was_continuous = None
            return True
        self._was_continuous = None
        return False

    def dwell(self, seconds, stop=None, poll=0.25):
        """Sleep in short steps so a stop request is felt promptly."""
        deadline = time.time() + seconds
        while True:
            left = deadline - time.time()
            if left <= 0:
                return "done"
            if stop is not None and stop():
                return "stopped"
            time.sleep(min(poll, left))

    def trace(self, snap=None, trace=1):
        """Trace `trace` as it stands: (x, amps), amps in the current
        amplitude units.

        ASCII, always - see the module docstring for why there is no binary
        path. The frequency axis is laid from the start and stop the analyser
        reports, read here unless a snapshot is handed in, across however many
        points came back. In zero span x is time in seconds, 0 to the sweep
        time, and the caller should say so (see `zero_span`)."""
        self.put("FORM ASC")
        amps = np.array(self.inst.query_ascii_values(f"TRAC? TRACE{trace}"),
                        dtype=float)
        if snap is None or "FSTART" not in snap or "FSTOP" not in snap:
            snap = dict(snap or {})
            snap.update(self.read_settings("FSTART", "FSTOP", "SWT"))
        start, stop = value_of(snap, "FSTART"), value_of(snap, "FSTOP")
        if start is None or stop is None:
            raise RuntimeError("the analyser would not report its start and "
                               "stop frequency, so the trace has no x axis")
        if zero_span(snap):
            return np.linspace(0.0, value_of(snap, "SWT", 0.0) or 0.0,
                               len(amps)), amps
        return np.linspace(start, stop, len(amps)), amps

    # -- status ------------------------------------------------------------

    def refresh_status(self, log=None):
        """Read the questionable-status condition registers and cache them."""
        values = {}
        for name, query in STATUS_QUERIES:
            try:
                values[name] = int(float(self.get(query)))
            except Exception:
                self.recover()
                values[name] = None
        self._status = Status(at=time.perf_counter(), **values)
        if log is not None:
            for fault in self._status.faults():
                log(f"  *** {fault} ***")
        return self._status

    def status(self, refresh_if_stale=True):
        if self._status is None and refresh_if_stale:
            self.refresh_status()
        return self._status

    def invalidate_status(self):
        self._status = None

    def align(self, timeout=300.0, stop=None):
        """Run a full alignment (`:CAL?`), which takes a minute or two and
        needs nothing connected that would disturb the 50 MHz reference.
        Returns True if the analyser reports it passed."""
        previous = self.inst.timeout
        self.inst.timeout = int(timeout * 1000)
        try:
            return int(float(self.get("CAL?"))) == 0
        finally:
            self.inst.timeout = previous
            self.invalidate_status()

    # -- context manager ---------------------------------------------------

    def __enter__(self):
        if self.inst is None:
            self.connect(self.addr or None)
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def zero_span(snap):
    """True when the analyser is in zero span, where the trace is amplitude
    against time at one frequency rather than a spectrum."""
    span = value_of(snap, "FSPAN")
    if span is not None:
        return span == 0
    start, stop = value_of(snap, "FSTART"), value_of(snap, "FSTOP")
    return start is not None and start == stop


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def metadata_text(an, snap, extra, command):
    """The capture described by the same settings snapshot the panel is
    showing, each setting followed by the command that would put it back. The
    same layout as sr760.metadata_text, so the two analysers' .txt files read
    alike."""
    lines = [
        f"captured             : {datetime.datetime.now().isoformat(timespec='seconds')}",
        f"instrument           : {an.idn}",
        f"visa address         : {an.addr}",
    ]
    options = getattr(an, "options", "")
    if options:
        lines.append(f"options              : {options}")
    for label, value in extra.items():
        lines.append(f"{label:<21}: {value}")
    lines.append("")
    lines.append("analyzer settings")
    for group, settings in SETTING_GROUPS:
        rows = [(s, snap[s.key]) for s in settings if snap.get(s.key) is not None]
        if not rows:
            continue
        lines.append(f"  [{group}]")
        for s, raw in rows:
            lines.append(f"    {s.label:<17}: {fmt_setting(s, raw):<14} "
                         f"({command(s.key, raw)})")
    return "\n".join(lines) + "\n"
