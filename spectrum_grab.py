#!/usr/bin/env python3
"""
Spectrum Grab - one-click capture from an SRS SR760 FFT spectrum analyzer.

Click a button, get a CSV of the trace, a PNG of the plot and a metadata text
file in your chosen folder. This is the read_sr760fft_data*.py bench scripts
rolled into one program: single grabs, span sweeps, stitched start-frequency
sweeps, the fast SPEB? binary transfer and the autorange-then-freeze routine.

The instrument itself lives in sr760.py beside this file, which is also the
scripting library: one copy of the command ordering, the settings model and the
file format, shared with the headless protocol runner. This file is the panel -
Tk, matplotlib and the config file, and nothing else.

Requires: NI-488.2 (or any VISA with GPIB support) + `pip install pyvisa numpy
          matplotlib pillow`
          (matplotlib draws the plots, pillow only sharpens the preview - the
          CSV and the metadata file are written without either)
Run with:  pythonw spectrum_grab.py      (pythonw = no console window)
"""

import base64
import datetime
import io
import json
import os
import queue
import re
import textwrap
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

# The instrument layer lives in sr760.py, beside this file - the command
# spellings, the settings model, the status handling and the file format are
# shared with anything that scripts the analyzer rather than driving it from
# the panel, so a fix lands in both at once. It has to sit beside this file for
# the panel to run.
from sr760 import (SR760, ALL_SETTINGS, BAD_NAME_CHARS, BY_KEY,
                   CONNECT_TIMEOUT_MS, DEFAULT_ADDRESS, DEFAULT_EXP_WAIT_S,
                   DEFAULT_SETTLE_RECS, MAX_FREQ, MAX_LIST_ITEMS, N_BINS,
                   READY_TIMEOUT_S, SETTING_GROUPS, SETTLE_KEYS,
                   SPACE_OWNERS, SPAN_CHOICES, SPANS, TRACE, TRANSFER_ASCII_S,
                   TRANSFER_BINARY_S, averaging_fault, binary_refusal,
                   binary_valid, canonical_units, capture_time, code_of,
                   convert_amplitude, fmt_hms, fmt_setting, hold_notes,
                   independent_records,
                   label_of, metadata_text, parse_list, parse_setting,
                   overlap_fault, pretty_units, read_csv, readout_fault,
                   reads_in_db,
                   record_stats, record_time, safe_name, span_hz, stats_notes,
                   trace_units, trace_yscale, unique_base, unit_parts,
                   value_of,
                   write_csv)

try:
    from PIL import Image, ImageTk        # smooth (Lanczos) preview rescale
except ImportError:                       # without pillow: Tk's integer subsample
    Image = ImageTk = None

try:
    import matplotlib
    matplotlib.use("Agg")                  # plots are files, not pop-up windows, so
    matplotlib.rcParams["font.size"] = 14  # they can be drawn off the main thread
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
except ImportError:                       # everything but the plots still works
    Figure = None

# The zoom window is the one plot that is not a file: a live Tk canvas with
# matplotlib's own navigation toolbar behind it. Imported separately because the
# Agg backend above is what lets the saved plots be drawn off the main thread,
# and that has to go on being true - this canvas is touched by the main thread
# only. A Python with matplotlib but no Tk backend still saves plots; it just
# cannot zoom.
try:
    from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,
                                                   NavigationToolbar2Tk)
except ImportError:
    FigureCanvasTkAgg = NavigationToolbar2Tk = None

# Remembered between sessions: folder, title, sweep lists, acquisition options.
# Kept out of the program folder so a git pull cannot clobber it.
CONFIG_PATH = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                           "SpectrumGrab", "config.json")

PLOT_DPI = 300
PEEK_DPI = 110                # a peek is only ever looked at in the preview box
PREVIEW_W, PREVIEW_H = 440, 330

# How often the combined plot may be redrawn while a sweep builds it up. At the
# span a segmented measurement is actually taken on a run is a minute or more,
# so every run gets its own redraw; this only bites on a long sweep of short
# runs, where the drawing would otherwise cost more than the measuring - each
# redraw carries every trace so far, so redrawing every run is quadratic.
PROGRESS_MIN_S = 2.0

# Most runs a sweep will set matrices aside for. The [case][start][span][bin]
# pair is allocated whole before the first trace, at 6.4 kB a run, so this is
# about 128 MB - far past any bench session, and the point is only that an
# accidental sweep is refused in one line rather than allocated blindly.
MAX_SWEEP_RUNS = 20000

# Plot window used unless the trace falls outside it, and only for dB traces -
# a linear trace is autoscaled instead.
DEFAULT_YMIN, DEFAULT_YMAX = -160.0, -20.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Instrument layer
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

# Text that changes as the panel runs has to be kept to a length, because Tk
# sizes a widget to fit its label and passes the request up to the window. A
# capture's file name is sixty characters and used to move the whole panel 91 px
# wider every time a grab finished, then back again on the next peek.
CAPTION_CHARS = 58            # under the preview
STATUS_CHARS = 64             # the connection, hold and compare lines
MIN_W, MIN_H = 900, 700       # a floor for fit_window on a small screen
AVG_BOX_W, AVG_BOX_H = 215, 34   # the what-the-averaging-is-worth read-out,
#                                  sized to the cell the Averaging group leaves
#                                  empty rather than to a strip of its own


def elide(text, width):
    """Long text shortened from the middle, so both ends survive - which is
    where a capture's name carries what it is and when it was taken."""
    text = str(text)
    if len(text) <= width:
        return text
    keep = (width - 3) // 2
    return f"{text[:keep]}...{text[-keep:]}"


SUBTITLE_SEP = "   ·   "      # between items of the notes line under the title
TITLE_WRAP = 58               # characters that fit across the figure at 15 pt
SUBTITLE_WRAP = 74            # ... and at 9 pt


def wrap_notes(bits, width=SUBTITLE_WRAP):
    """Lay the settings items out over as few lines as they fit on, breaking
    only between whole items - a line that fell off the edge of the figure was
    the reason the settings were kept out of the title in the first place."""
    lines, line = [], ""
    for bit in bits:
        joined = bit if not line else line + SUBTITLE_SEP + bit
        if line and len(joined) > width:
            lines.append(line)
            line = bit
        else:
            line = joined
    if line:
        lines.append(line)
    return "\n".join(lines)


# Greys for the reference sequences a comparison draws underneath. Grey rather
# than colours: while a sweep builds, the colours belong to the segments being
# measured, and a reference that competes with them for attention is worse than
# no reference. Several levels so more than one reference stays tellable apart.
REF_GREYS = ("#8c8c8c", "#5a5a5a", "#b4b4b4", "#3c3c3c")


def sequence_label(path):
    """The name a saved capture belongs under: everything before the _spanNN
    part of the file name, plus the folder it sits in.

    The file names a run writes are <title>[_case]_spanNN[_strfF Hz]_<top>Hz_
    <date>, so the part before _span is what a set of segments has in common and
    the part after is what tells them apart. A whole sweep writes
    <title>_sweep_<date> instead, and that is cut at the same place - so one
    sweep loaded from its combined CSV and the same sweep loaded from the
    per-segment files it used to leave behind come out under one name, which is
    what they are.

    The _N that unique_base adds to a second sweep of the same title on the same
    day is kept, as `(run 2)`. There it means a different acquisition - the
    aborted fourteen-segment run of 2026-08-30 and the fifty-two that replaced
    it are both `50 ohm full span grounded`, and drawing them as one curve would
    show that band measured twice. On a per-segment file the same suffix only
    means two files wanted one name, so it is cut with the rest.

    The folder joins the key because the same title measured on two days is two
    sequences, not one - which is the usual comparison.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    sweep = re.match(r"^(?P<head>.+?)_sweep_\d{8}(?:_(?P<run>\d+))?$", stem)
    if sweep:
        head = sweep.group("head")
        if sweep.group("run"):
            head += f" (run {int(sweep.group('run')) + 1})"
    else:
        head = re.split(r"_span\d", stem, maxsplit=1)[0]
    if not head:
        head = stem
    day = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return f"{head}  {day}" if day else head


def load_sequences(paths, log=None):
    """Saved CSVs grouped into sequences, ready to draw.

    Returns [{name, freqs, amps, ylabel, segments, paths}], one entry per group,
    with each group's segments concatenated and sorted into one curve - a stitch
    is one measurement of one band, drawn as one line, however many captures it
    took. Points are not merged where segments overlap; both are kept, which is
    what makes a bad join visible rather than averaged away.

    A segment whose units differ from the rest of its own group is dropped and
    named, because there is no reading of that which is a single measurement.
    """
    say = log if log is not None else (lambda _msg: None)
    groups = {}
    for path in paths:
        try:
            freqs, amps, ylabel = read_csv(path)
        except Exception as exc:
            say(f"  ({os.path.basename(path)}: {exc})")
            continue
        # Back into the spelling trace_units() uses, so a sequence loaded off
        # disk compares equal to a live capture in the same units instead of
        # being "converted" from a scale to itself.
        ylabel = canonical_units(ylabel)
        groups.setdefault(sequence_label(path),
                          []).append((path, freqs, amps, ylabel))
    out = []
    for name, parts in groups.items():
        ylabel = parts[0][3]
        kept = [p for p in parts if p[3] == ylabel]
        if len(kept) != len(parts):
            odd = sorted({p[3] or "(no header)" for p in parts if p[3] != ylabel})
            say(f"  ({name}: dropped {len(parts) - len(kept)} segment(s) "
                f"measured in {', '.join(odd)} rather than {ylabel})")
        freqs = np.concatenate([p[1] for p in kept])
        amps = np.concatenate([p[2] for p in kept])
        order = np.argsort(freqs, kind="stable")
        out.append({"name": name, "freqs": freqs[order], "amps": amps[order],
                    "ylabel": ylabel, "segments": len(kept),
                    "paths": [p[0] for p in kept]})
    return sorted(out, key=lambda s: s["name"])


def draw_traces(ax, traces, title, subtitle, ylabel, ymin, ymax, legend=True,
                yscale="linear", refs=()):
    """Draw one or more traces onto an existing axes.

    Titling is two lines: what the capture is, then the handful of settings that
    decide what the trace means. The default y window is kept unless the data
    falls outside it, and the second line says so when it had to be widened - so
    a plot that looks unlike the others is flagged rather than silently
    rescaled. Linear traces are left to autoscale.

    `refs` are sequences loaded off disk to compare against. They go on first,
    in grey and behind, so the colours go on meaning the thing being measured -
    a reference that competes with the live traces for attention is worse than
    no reference. They count toward the y window like anything else, because a
    comparison whose reference falls off the top of the plot is not one.

    Split out from save_plot so the zoom window draws through the same code: a
    plot you have zoomed into and the PNG on disk should differ in nothing but
    the axis limits."""
    for i, (freqs, amps, label) in enumerate(refs):
        ax.plot(freqs, amps, lw=1.0, label=label, zorder=1,
                color=REF_GREYS[i % len(REF_GREYS)])
    for freqs, amps, label in traces:
        ax.plot(freqs, amps, lw=1.2, label=label, zorder=2,
                color="blue" if len(traces) == 1 and not refs else None)

    everything = list(traces) + list(refs)
    rescaled = False
    if ymin is not None and ymax is not None and everything:
        low = min(float(np.nanmin(a)) for _, a, _ in everything)
        high = max(float(np.nanmax(a)) for _, a, _ in everything)
        if high > ymax:
            ymax, rescaled = high, True
        if low < ymin:
            ymin, rescaled = low, True
        ax.set_ylim([ymin, ymax])

    notes = subtitle.split(SUBTITLE_SEP) if subtitle else []
    if rescaled:
        notes.append("y-scale widened to fit the trace")
    if yscale == "log":
        # A log axis cannot draw a zero or a negative bin, so a trace that
        # reaches one is drawn linear instead and the notes say why rather than
        # leaving a plot that quietly disagrees with the analyzer's screen.
        if everything and min(float(np.nanmin(a))
                              for _, a, _ in everything) > 0:
            ax.set_yscale("log")
            ax.grid(True, which="minor", alpha=0.3)
        else:
            notes.append("linear y-scale: the trace reaches zero")
    notes = wrap_notes(notes)

    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel(pretty_units(ylabel))
    # The title is the name of the capture and nothing else, so a folder of
    # plots can be told apart at a glance; the settings that used to swamp it -
    # and the mangled file name it used to be - go in a smaller line beneath.
    # The room the notes need is reserved with the title's pad, because
    # tight_layout only measures the title itself.
    ax.set_title(textwrap.fill(title, TITLE_WRAP), fontsize=15,
                 pad=(8 + 13 * (notes.count("\n") + 1)) if notes else 8)
    if notes:
        ax.text(0.5, 1.008, notes, transform=ax.transAxes, ha="center",
                va="bottom", fontsize=9, color="#444", linespacing=1.4)
    ax.grid(True)
    # A reference always earns a legend entry - unlabelled grey is just clutter
    # - so one trace against one reference is a legend where one trace alone
    # is not.
    if legend and 1 < len(everything) <= 12:
        ax.legend(fontsize=9)


def save_plot(path, traces, title, subtitle, ylabel, ymin, ymax, legend=True,
              dpi=PLOT_DPI, yscale="linear", refs=()):
    """The same plot as a PNG. `path` may be a file object, which is how the
    peek keeps its picture out of the file system."""
    fig = Figure(figsize=(8, 6))
    FigureCanvasAgg(fig)
    draw_traces(fig.add_subplot(111), traces, title, subtitle, ylabel,
                ymin, ymax, legend, yscale, refs=refs)
    fig.tight_layout()
    fig.savefig(path, format="png", dpi=dpi)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:
    last_ylabel = "dBV"       # unit of the most recent snapshot, for sweep plots
    last_yscale = "linear"    # ... and the axis the analyzer draws it on

    def __init__(self, root):
        self.root = root
        self.an = SR760(connect=False, log=self.log)
        self.msgs = queue.Queue()
        self.busy = False
        self.abort = threading.Event()
        self.auto_job = None
        # What was drawn last, kept so the zoom window can redraw it from the
        # data rather than from the PNG: (traces, title, subtitle, ylabel,
        # yscale). None until the first grab or peek of the session.
        self.last_plot = None
        # When the building sweep was last redrawn, so a long sweep of short
        # runs does not spend its time drawing instead of measuring.
        self.last_progress = 0.0
        # Sequences loaded off disk to draw underneath what is being measured.
        # They outlive a grab and a sweep on purpose: the reason to load last
        # week's floor is to take this week's against it, and that is several
        # captures, not one. `said` keeps a warning about one of them from
        # repeating on every redraw.
        self.compare = []
        self.said = set()
        self.zoom_win = self.zoom_fig = self.zoom_ax = None
        self.zoom_canvas = self.zoom_tb = None
        # Range hold: the input range a measurement set is pinned to, and when
        # it was armed. None means no set is being held. It deliberately
        # outlives one press of GRAB - dark against light, or resistor against
        # resistor, are separate acquisitions that only compare if they were
        # taken on the same range.
        self.pinned_range = None
        self.pinned_at = ""
        self.settle_due = threading.Event()

        root.title("Spectrum Grab - SR760 FFT")

        pad = dict(padx=8, pady=4)
        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(body)
        right.pack(side="left", fill="y")

        self.build_connection(left, pad)
        self.build_output(left, pad)
        self.build_grab(left, pad)
        self.build_sweep(left, pad)
        self.build_options(left, pad)
        self.build_hold(left, pad)
        self.build_compare(left, pad)
        self.build_log(left, pad)
        self.build_preview(right, pad)
        self.build_settings(right, pad)

        root.bind("<space>", self.on_space)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.fit_window()
        self.saved_cfg = None
        if Figure is None:
            self.log("matplotlib is not installed for this Python, so there are "
                     "no plots and no preview.")
            self.log("  CSV and metadata are unaffected. To get plots: "
                     "pip install matplotlib")
        self.load_config()
        self.root.after(100, self.pump)
        self.root.after(300, self.do_connect)
        self.load_latest_preview()

    def fit_window(self):
        """Size the window to what the panel actually asks for, capped by the
        screen.

        It used to be a flat 1180x950, which the settings column overran by 40
        px - Read and Apply sat 7 px inside the bottom edge and anything that
        grew the column pushed them out of reach. Asking the panel how tall it
        is means a line added to a read-out costs a taller window rather than a
        button, and MIN_H keeps a screen that cannot fit it from collapsing the
        thing to nothing.
        """
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth() - 60
        screen_h = self.root.winfo_screenheight() - 80
        wide = min(max(self.root.winfo_reqwidth(), MIN_W), screen_w)
        high = min(max(self.root.winfo_reqheight(), MIN_H), screen_h)
        self.root.geometry(f"{wide}x{high}+30+15")
        return wide, high

    # -- layout -----------------------------------------------------------

    def build_connection(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Analyzer")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Address:").pack(side="left")
        self.addr = tk.StringVar(value=DEFAULT_ADDRESS)
        ttk.Entry(row, textvariable=self.addr, width=22).pack(side="left", padx=6)
        self.connect_btn = ttk.Button(row, text="Connect", command=self.do_connect)
        self.connect_btn.pack(side="right")
        self.status = ttk.Label(f, text="Not connected", foreground="#a00")
        self.status.pack(anchor="w", padx=8, pady=(0, 6))

    def build_output(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Save to")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(6, 2))
        default_dir = os.path.join(os.path.expanduser("~"), "Desktop",
                                   "spectrum_data")
        self.outdir = tk.StringVar(value=default_dir)
        ttk.Entry(row, textvariable=self.outdir).pack(side="left", fill="x",
                                                      expand=True)
        ttk.Button(row, text="...", width=3,
                   command=self.pick_dir).pack(side="left", padx=6)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="Title:").pack(side="left")
        self.title = tk.StringVar(value="sr760")
        ttk.Entry(row, textvariable=self.title).pack(side="left", fill="x",
                                                     expand=True, padx=6)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(2, 6))
        self.dated = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="dated subfolder", variable=self.dated,
                        command=self.load_latest_preview).pack(side="left")
        self.save_csv = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="csv", variable=self.save_csv).pack(side="left",
                                                                     padx=8)
        self.save_png = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="png", variable=self.save_png).pack(side="left")
        self.save_txt = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="metadata", variable=self.save_txt).pack(
            side="left", padx=8)

    def build_grab(self, parent, pad):
        f = ttk.Frame(parent)
        f.pack(fill="x", **pad)
        self.grab_btn = ttk.Button(f, text="GRAB one  (or press Space)",
                                   command=self.do_grab, state="disabled")
        self.grab_btn.pack(side="left", fill="x", expand=True, ipady=8)
        self.avg_btn = ttk.Button(f, text="Start average", width=14,
                                  command=self.do_average, state="disabled")
        self.avg_btn.pack(side="left", padx=(6, 0), ipady=8)
        self.peek_btn = ttk.Button(f, text="Peek (saves nothing)", width=19,
                                   command=self.do_peek, state="disabled")
        self.peek_btn.pack(side="left", padx=(6, 0), ipady=8)
        self.stop_btn = ttk.Button(f, text="Stop", width=6,
                                   command=self.do_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6, ipady=8)

        f = ttk.Frame(parent)
        f.pack(fill="x", **pad)
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Auto-grab every", variable=self.auto,
                        command=self.toggle_auto).pack(side="left")
        self.interval = tk.StringVar(value="60")
        ttk.Entry(f, textvariable=self.interval, width=6).pack(side="left", padx=4)
        ttk.Label(f, text="seconds").pack(side="left")

    def build_sweep(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Sweep  (blank = one grab at the current "
                                        "settings)")
        f.pack(fill="x", **pad)
        grid = ttk.Frame(f)
        grid.pack(fill="x", padx=6, pady=(6, 2))
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Span codes:").grid(row=0, column=0, sticky="e",
                                                 padx=(0, 4), pady=1)
        self.spans_txt = tk.StringVar(value="")
        ttk.Entry(grid, textvariable=self.spans_txt).grid(row=0, column=1,
                                                          sticky="ew", pady=1)
        ttk.Label(grid, text="Start freqs (Hz):").grid(row=1, column=0, sticky="e",
                                                       padx=(0, 4), pady=1)
        self.starts_txt = tk.StringVar(value="")
        ttk.Entry(grid, textvariable=self.starts_txt).grid(row=1, column=1,
                                                           sticky="ew", pady=1)
        ttk.Label(grid, text="Cases:").grid(row=2, column=0, sticky="e",
                                            padx=(0, 4), pady=1)
        self.cases_txt = tk.StringVar(value="")
        ttk.Entry(grid, textvariable=self.cases_txt).grid(row=2, column=1,
                                                          sticky="ew", pady=1)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="Stitch to").pack(side="left")
        self.stitch_stop = tk.StringVar(value="1000")
        ttk.Entry(row, textvariable=self.stitch_stop, width=8).pack(side="left",
                                                                    padx=4)
        ttk.Label(row, text="Hz, overlap").pack(side="left")
        self.stitch_overlap = tk.StringVar(value="10")
        ttk.Entry(row, textvariable=self.stitch_overlap, width=5).pack(side="left",
                                                                       padx=4)
        ttk.Label(row, text="points").pack(side="left")
        ttk.Button(row, text="Fill start freqs",
                   command=self.do_stitch).pack(side="left", padx=8)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(4, 2))
        self.sweep_btn = ttk.Button(row, text="RUN SWEEP",
                                    command=self.do_sweep, state="disabled")
        self.sweep_btn.pack(side="left", fill="x", expand=True, ipady=6)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(2, 6))
        self.save_npy = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="save .npy matrices",
                        variable=self.save_npy).pack(side="left")
        self.combined = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="combined plot",
                        variable=self.combined).pack(side="left", padx=10)
        self.pause_cases = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="pause before each case",
                        variable=self.pause_cases).pack(side="left")
        # A sweep writes its segments as one set of files, not as one set per
        # segment - 65 segments used to leave 195 files behind. This puts the
        # old behaviour back for anyone who wants a segment on its own.
        self.keep_segments = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="keep per-segment files",
                        variable=self.keep_segments).pack(side="left", padx=10)

    def build_options(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Acquisition")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(6, 2))
        self.binary = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="fast binary transfer (SPEB?)",
                        variable=self.binary).pack(side="left")
        self.lock = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="lock front panel while measuring",
                        variable=self.lock).pack(side="left", padx=10)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=2)
        self.do_arng = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="before each grab, auto-range then freeze for",
                        variable=self.do_arng).pack(side="left")
        self.arng_s = tk.StringVar(value="15")
        ttk.Entry(row, textvariable=self.arng_s, width=5).pack(side="left", padx=4)
        ttk.Label(row, text="s").pack(side="left")

        # The analyzer's own auto range, left switched on rather than frozen.
        # It is in the settings panel too, but there it is an edit waiting for
        # Apply; here it is the switch you reach for while watching a trace, so
        # it goes to the analyzer the moment it is clicked.
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=2)
        self.arng_live = tk.BooleanVar(value=False)
        self.arng_chk = ttk.Checkbutton(
            row, text="auto range (ARNG) - sent as soon as it is ticked",
            variable=self.arng_live, command=self.toggle_autorange,
            state="disabled")
        self.arng_chk.pack(side="left")

        grid = ttk.Frame(f)
        grid.pack(fill="x", padx=6, pady=(2, 6))
        for i, item in enumerate(
                (("settle before start (s)", "settle_s", "0"),
                 ("settle (record lengths)", "settle_recs",
                  f"{DEFAULT_SETTLE_RECS:g}"),
                 ("measurement timeout (s)", "timeout_s", "600"),
                 ("exponential wait (s)", "exp_wait_s", f"{DEFAULT_EXP_WAIT_S:g}"),
                 ("plot y min (dB)", "ymin", f"{DEFAULT_YMIN:g}"),
                 ("plot y max (dB)", "ymax", f"{DEFAULT_YMAX:g}"))):
            label, attr, default = item
            r, c = divmod(i, 2)
            ttk.Label(grid, text=label + ":").grid(row=r, column=c * 2,
                                                   sticky="e", padx=(0, 4), pady=1)
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            ttk.Entry(grid, textvariable=var, width=8).grid(
                row=r, column=c * 2 + 1, sticky="w", padx=(0, 12), pady=1)

    def build_hold(self, parent, pad):
        """Range hold: one autorange for a whole measurement set, then the range
        pinned for every trace in it.

        A set here is not one press of GRAB. Dark against light, resistor
        against resistor, range-step A against range-step B are separate
        acquisitions whose whole content is the difference between them, and
        that difference only means anything if the input range did not move
        underneath it. So the hold is armed once, by hand, and stays armed
        across as many grabs as the set takes."""
        f = ttk.LabelFrame(parent, text="Range hold  (one range for a whole "
                                        "measurement set)")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(row, text="Set:").pack(side="left")
        self.set_name = tk.StringVar(value="")
        ttk.Entry(row, textvariable=self.set_name, width=18).pack(side="left",
                                                                  padx=6)
        self.arm_btn = ttk.Button(row, text="Auto-range and pin",
                                  command=self.do_arm_hold, state="disabled")
        self.arm_btn.pack(side="left", padx=4)
        self.pin_btn = ttk.Button(row, text="Pin as-is",
                                  command=lambda: self.do_arm_hold(rerange=False),
                                  state="disabled")
        self.pin_btn.pack(side="left", padx=2)
        self.release_btn = ttk.Button(row, text="Release",
                                      command=self.do_release_hold,
                                      state="disabled")
        self.release_btn.pack(side="left", padx=4)
        self.hold_status = ttk.Label(f, text="not held - the analyzer is free "
                                             "to move its own range",
                                     foreground="#c60")
        self.hold_status.pack(anchor="w", padx=10, pady=(0, 6))

    def build_compare(self, parent, pad):
        """Other sequences, loaded off disk and drawn underneath.

        A sequence here is what a stitch leaves behind: the CSVs a run wrote,
        grouped by the name they share and the day they were taken. Picked with
        a file dialog rather than typed, because the titles have spaces in them
        and the comparison that matters usually crosses dated folders - last
        week's floor against this week's.
        """
        f = ttk.LabelFrame(parent, text="Compare  (saved sequences, drawn "
                                        "underneath)")
        f.pack(fill="x", **pad)
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Button(row, text="Add sequences...",
                   command=self.do_compare_add).pack(side="left")
        self.compare_btn = ttk.Button(row, text="Plot comparison",
                                      command=self.do_compare_plot,
                                      state="disabled")
        self.compare_btn.pack(side="left", padx=6)
        self.compare_clear_btn = ttk.Button(row, text="Clear",
                                            command=self.do_compare_clear,
                                            state="disabled")
        self.compare_clear_btn.pack(side="left")
        self.compare_status = ttk.Label(f, text="nothing loaded", foreground="#666")
        self.compare_status.pack(anchor="w", padx=10, pady=(0, 6))

    def build_log(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Log")
        f.pack(fill="both", expand=True, **pad)
        self.logbox = tk.Text(f, height=8, wrap="none", font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True, padx=4, pady=4)

    def build_preview(self, parent, pad):
        # The frame's own label never changes. A LabelFrame asks for room to
        # draw its label, and that request goes all the way up to the window, so
        # a caption there is a caption that resizes the panel: "Last plot -
        # <name>.png (double-click...)" is a hundred characters and moved
        # everything 91 px sideways at the end of every grab.
        self.shot_frame = ttk.LabelFrame(parent, text="Last plot")
        self.shot_frame.pack(fill="x", **pad)
        box = tk.Frame(self.shot_frame, width=PREVIEW_W, height=PREVIEW_H)
        box.pack(padx=4, pady=4)
        box.pack_propagate(False)     # keep the box from shrinking to the label
        self.preview = ttk.Label(box, text="(no plot yet)", anchor="center")
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Double-Button-1>", self.open_preview)
        self.preview_img = None
        self.preview_path = None

        # The caption lives here instead, in a box of its own that cannot grow:
        # pack_propagate(False) means whatever is written in it asks for nothing.
        cap = tk.Frame(self.shot_frame, width=PREVIEW_W, height=20)
        cap.pack(fill="x", padx=6)
        cap.pack_propagate(False)
        self.shot_caption = ttk.Label(cap, text="(no plot yet)", anchor="w",
                                      foreground="#444")
        self.shot_caption.pack(fill="both", expand=True)

        row = ttk.Frame(self.shot_frame)
        row.pack(fill="x", padx=6, pady=(0, 6))
        self.zoom_btn = ttk.Button(row, text="Zoom / pan...",
                                   command=self.open_zoom, state="disabled")
        self.zoom_btn.pack(side="left")
        self.zoom_follow = tk.BooleanVar(value=True)
        ttk.Label(row, text="double-click the plot for the saved PNG",
                  foreground="#666").pack(side="left", padx=8)

    def build_settings(self, parent, pad):
        self.set_vars = {}        # key -> StringVar shown in the panel
        self.set_marks = {}       # key -> "edited" marker label
        self.set_inst = {}        # key -> value the analyzer last reported
        self.read_stamp = ""      # when the panel last matched the instrument

        for group, settings in SETTING_GROUPS:
            frame = ttk.LabelFrame(parent, text=group)
            frame.pack(fill="x", padx=8, pady=2)
            grid = ttk.Frame(frame)
            grid.pack(fill="x", padx=6, pady=4)
            for i, s in enumerate(settings):
                row, col = divmod(i, 2)
                ttk.Label(grid, text=s.label + ":").grid(
                    row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
                self.setting_widget(grid, s, row, col * 2 + 1)
            if group == "Averaging":
                # What the boxes above are actually worth, in the cell the group
                # leaves empty: five settings laid out two to a row fill five of
                # six places, so this costs no height at all. It used to sit in
                # a strip underneath, which pushed Read and Apply off the bottom
                # of the panel.
                #
                # NAVG counts records the analyzer averaged, not independent
                # ones, and SPAN reinstalls its own default overlap - 98.4375%
                # at the narrow end - so NAVG can be honoured to the letter
                # while the trace is worth a fifty-fifth of it. Nothing to fill
                # in: it reads the boxes as they are typed, before Apply.
                row = (len(settings) - 1) // 2
                box = tk.Frame(grid, width=AVG_BOX_W, height=AVG_BOX_H)
                box.grid(row=row, column=2, columnspan=2, sticky="w",
                         padx=(2, 0))
                # pack_propagate, not grid_propagate: the frame is PLACED with
                # grid but its label PACKS inside it, and it is the inner
                # manager that has to be told to stop asking. With the wrong one
                # the box grew from 18 px to 46 as the read-out gained a line,
                # which is the whole thing this is here to avoid.
                box.pack_propagate(False)
                self.avg_worth = ttk.Label(box, text="", anchor="nw",
                                           justify="left", font=("", 8),
                                           wraplength=AVG_BOX_W - 4)
                self.avg_worth.pack(fill="both", expand=True)

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=(2, 6))
        self.read_btn = ttk.Button(bar, text="Read", command=self.do_read_settings,
                                   state="disabled")
        self.read_btn.pack(side="left")
        self.apply_btn = ttk.Button(bar, text="Apply changes",
                                    command=self.do_apply_settings,
                                    state="disabled")
        self.apply_btn.pack(side="left", padx=4)
        self.aoff_btn = ttk.Button(bar, text="Auto-offset",
                                   command=lambda: self.do_action(
                                       "auto offset", lambda: self.an.put("AOFF"),
                                       wait_ready=True),
                                   state="disabled")
        self.aoff_btn.pack(side="left", padx=4)
        self.auts_btn = ttk.Button(bar, text="Auto-scale",
                                   command=lambda: self.do_action(
                                       "auto scale", self.an.autoscale),
                                   state="disabled")
        self.auts_btn.pack(side="left", padx=4)
        self.set_status = ttk.Label(parent, text="not read yet", foreground="#666")
        self.set_status.pack(anchor="w", padx=10)

    def setting_widget(self, parent, s, row, col):
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=col, sticky="w", padx=2, pady=1)
        var = tk.StringVar()
        if s.kind == "enum":
            ttk.Combobox(cell, textvariable=var, values=list(s.choices),
                         width=13, state="readonly").pack(side="left")
        else:
            ttk.Entry(cell, textvariable=var, width=15).pack(side="left")
        mark = ttk.Label(cell, text=" ", width=1, foreground="#c60")
        mark.pack(side="left")
        self.set_vars[s.key] = var
        self.set_marks[s.key] = mark
        self.set_inst[s.key] = ""
        var.trace_add("write", lambda *_: self.refresh_marks())

    # -- helpers ----------------------------------------------------------

    def log(self, text):
        self.msgs.put(text)

    def pump(self):
        while not self.msgs.empty():
            self.logbox.insert("end", self.msgs.get() + "\n")
            self.logbox.see("end")
        self.root.after(100, self.pump)

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or ".")
        if d:
            self.outdir.set(d)
            self.load_latest_preview()
            self.save_config()

    def safe_title(self):
        t = "".join("_" if c in BAD_NAME_CHARS else c
                    for c in self.title.get()).strip()
        return t or "sr760"

    def target_dir(self):
        r"""Where this session writes. The dated subfolder reproduces the
        ...\YYYYMMDD\... layout the scripts built into their PATHNAME."""
        outdir = self.outdir.get()
        if self.dated.get():
            outdir = os.path.join(outdir,
                                  datetime.datetime.now().strftime("%Y%m%d"))
        return outdir

    def float_of(self, var, default, name):
        try:
            return float(var.get())
        except ValueError:
            self.log(f"  ({name} is not a number, using {default})")
            return default

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy or not self.an.inst else "normal"
        for btn in (self.grab_btn, self.sweep_btn, self.avg_btn, self.peek_btn,
                    self.read_btn, self.apply_btn, self.aoff_btn,
                    self.auts_btn, self.arng_chk, self.arm_btn, self.pin_btn):
            btn.configure(state=state)
        self.refresh_hold()
        self.refresh_compare()
        # Connect is the one button that means something while disconnected.
        self.connect_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def confirm(self, message):
        """Ask on the main thread and block the worker until it is answered -
        the bench scripts' input('Press Enter to continue') between cases."""
        answered = threading.Event()
        box = {}

        def ask():
            box["go"] = messagebox.askokcancel("Spectrum Grab", message)
            answered.set()

        self.root.after(0, ask)
        answered.wait()
        return box.get("go", False)

    # -- preview ----------------------------------------------------------

    def render_preview(self, source):
        """Put a PNG in the preview box: `source` is a file path, or PNG bytes
        for a plot that was never written to disk. Main thread only (Tk images
        are not thread safe)."""
        try:
            if Image is not None:
                im = Image.open(source if isinstance(source, str)
                                else io.BytesIO(source))
                im.load()
                k = min(PREVIEW_W / im.width, PREVIEW_H / im.height, 1.0)
                if k < 1.0:
                    im = im.resize((max(1, round(im.width * k)),
                                    max(1, round(im.height * k))),
                                   Image.LANCZOS)
                img = ImageTk.PhotoImage(im)
            else:
                # Tk 8.6 reads PNG natively, from a file or from base64 data.
                img = (tk.PhotoImage(file=source) if isinstance(source, str)
                       else tk.PhotoImage(data=base64.b64encode(source)))
                k = 1
                while img.width() // k > PREVIEW_W or img.height() // k > PREVIEW_H:
                    k += 1
                if k > 1:
                    img = img.subsample(k)         # integer factors only
        except Exception as exc:
            self.log(f"  (preview failed: {exc})")
            return False
        self.preview_img = img            # keep a reference or Tk drops it
        self.preview.configure(image=img, text="")
        return True

    def set_caption(self, text):
        """Main thread. What the preview is showing, in a label that cannot
        resize the panel around it."""
        self.shot_caption.configure(text=elide(text, CAPTION_CHARS))

    def show_preview(self, path):
        if not self.render_preview(path):
            return
        self.preview_path = path
        self.set_caption(os.path.basename(path))

    def show_peek(self, data):
        """A plot held only in the window. preview_path goes to None: there is
        no file to open, so a double-click must not reopen the last saved one
        and pass it off as what is on screen."""
        if not self.render_preview(data):
            return
        self.preview_path = None
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.set_caption(f"Peek at {stamp} - not saved")

    def show_building(self, data, done, planned):
        """The sweep as it stands, part way through. Same reasoning as the peek
        about preview_path: the combined PNG is not written until the sweep
        ends, so there is nothing for a double-click to open yet and it must not
        fall back to whichever file happened to be shown before."""
        if not self.render_preview(data):
            return
        self.preview_path = None
        self.set_caption(f"Sweep building - {done} of {planned} runs")

    # -- zoom window ------------------------------------------------------

    def remember_plot(self, traces, title, subtitle, ylabel, yscale, refs=()):
        """Keep what was just drawn so the zoom window can rebuild it from the
        data. Called from the worker threads, so the window itself is only
        touched by way of the main loop."""
        self.last_plot = (traces, title, subtitle, ylabel, yscale, refs)
        self.root.after(0, self.plot_arrived)

    def plot_arrived(self):
        """Main thread. A new plot exists, so zooming is now possible; redraw an
        open window if it is following."""
        self.zoom_btn.configure(state="normal")
        if self.zoom_follow.get():
            self.draw_zoom()

    def zoom_open(self):
        return self.zoom_win is not None and self.zoom_win.winfo_exists()

    def open_zoom(self):
        """An interactive copy of the last plot in a window of its own, with
        matplotlib's navigation toolbar: rectangle zoom, pan, back, forward,
        Home and Save, the same set the ILC panel has.

        A window rather than the preview box, for two reasons. A spectrum is
        worth looking at large, and the preview has to go on showing PNGs this
        session did not draw - the newest file in the folder at startup has no
        trace data behind it to redraw from."""
        if Figure is None or FigureCanvasTkAgg is None:
            self.log("Zoom needs matplotlib with its Tk backend, which this "
                     "Python does not have.")
            return
        if self.last_plot is None:
            self.log("Nothing plotted yet - grab or peek first.")
            return
        if self.zoom_open():
            self.zoom_win.deiconify()
            self.zoom_win.lift()
            self.draw_zoom()
            return

        win = tk.Toplevel(self.root)
        win.title("Spectrum Grab - zoom")
        win.geometry("980x720")
        fig = Figure(figsize=(9, 6.4), dpi=100)
        canvas = FigureCanvasTkAgg(fig, master=win)

        # Anything of a fixed height has to be packed before the canvas: the
        # canvas expands into whatever cavity is left when it is packed, and
        # packing it first leaves the toolbar nothing to sit in.
        bar = ttk.Frame(win)
        bar.pack(side="bottom", fill="x")
        ttk.Checkbutton(bar, text="follow new captures",
                        variable=self.zoom_follow).pack(side="left", padx=6,
                                                        pady=3)
        ttk.Label(bar, text="untick to hold this view while a sweep or an "
                            "auto-grab runs", foreground="#666").pack(
            side="left", padx=4)
        self.zoom_tb = NavigationToolbar2Tk(canvas, win)
        canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

        self.zoom_win, self.zoom_fig, self.zoom_canvas = win, fig, canvas
        self.zoom_ax = fig.add_subplot(111)
        win.protocol("WM_DELETE_WINDOW", self.close_zoom)
        self.draw_zoom()

    def close_zoom(self):
        win, self.zoom_win = self.zoom_win, None
        self.zoom_fig = self.zoom_ax = self.zoom_canvas = self.zoom_tb = None
        if win is not None:
            win.destroy()

    def draw_zoom(self):
        """Main thread. Redraw the zoom window from the last plot, through the
        same drawing code the PNG goes through."""
        if not self.zoom_open() or self.last_plot is None:
            return
        traces, title, subtitle, ylabel, yscale, refs = self.last_plot
        self.zoom_ax.clear()
        try:
            draw_traces(self.zoom_ax, traces, title, subtitle, ylabel,
                        *self.y_window(ylabel), yscale=yscale, refs=refs)
            self.zoom_fig.tight_layout()
            self.zoom_canvas.draw_idle()
        except Exception as exc:
            self.log(f"  (zoom redraw failed: {exc})")
            return
        # The view stack belonged to the trace that has just been replaced, so
        # Home would otherwise go back to the previous capture's limits.
        self.zoom_tb.update()

    def load_latest_preview(self):
        outdir = self.target_dir()
        try:
            shots = [os.path.join(outdir, n) for n in os.listdir(outdir)
                     if n.lower().endswith(".png")]
        except OSError:
            return
        if shots:
            self.show_preview(max(shots, key=os.path.getmtime))

    def open_preview(self, _event=None):
        if self.preview_path:
            try:
                os.startfile(self.preview_path)
            except Exception as exc:
                self.log(f"ERROR: {exc}")

    # -- config -----------------------------------------------------------

    def config_vars(self):
        return {
            "addr": self.addr, "outdir": self.outdir, "title": self.title,
            "dated": self.dated, "save_csv": self.save_csv,
            "save_png": self.save_png, "save_txt": self.save_txt,
            "spans": self.spans_txt, "starts": self.starts_txt,
            "cases": self.cases_txt, "stitch_stop": self.stitch_stop,
            "stitch_overlap": self.stitch_overlap, "save_npy": self.save_npy,
            "combined": self.combined, "pause_cases": self.pause_cases,
            "binary": self.binary, "lock": self.lock, "autorange": self.do_arng,
            "autorange_s": self.arng_s, "settle_s": self.settle_s,
            "timeout_s": self.timeout_s, "exp_wait_s": self.exp_wait_s,
            "settle_recs": self.settle_recs, "set_name": self.set_name,
            "ymin": self.ymin, "ymax": self.ymax,
            "interval": self.interval,
        }

    def current_cfg(self):
        return {k: v.get() for k, v in self.config_vars().items()}

    def load_config(self):
        """Restore what the last session was using. Anything missing, malformed
        or of the wrong type is ignored and leaves the default in place."""
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
            if not isinstance(cfg, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            return
        except Exception as exc:
            self.log(f"Ignoring unreadable {CONFIG_PATH}: {exc}")
            return

        for key, var in self.config_vars().items():
            value = cfg.get(key)
            if isinstance(var, tk.BooleanVar):
                if isinstance(value, (bool, int)):
                    var.set(bool(value))
            elif isinstance(value, str):
                var.set(value)
        self.saved_cfg = self.current_cfg()
        self.log(f"Restored last session from {CONFIG_PATH}")

    def save_config(self):
        """Called after a grab, when the folder is picked, and on close. Writes
        only when something actually changed."""
        cfg = self.current_cfg()
        if cfg == self.saved_cfg:
            return
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, indent=2)
            self.saved_cfg = cfg
        except Exception as exc:
            self.log(f"Could not save {CONFIG_PATH}: {exc}")

    # -- actions ----------------------------------------------------------

    def on_space(self, event):
        try:
            cls = event.widget.winfo_class()
        except AttributeError:
            cls = ""
        if cls in SPACE_OWNERS:
            return
        self.do_grab()

    def do_connect(self):
        # Connecting drops the VISA session and opens a new one, so it has to
        # wait its turn like everything else - otherwise the startup auto-connect
        # or an impatient click would pull the instrument out from under a sweep.
        if self.busy:
            self.log("Busy - stop the current job before reconnecting.")
            return
        self.set_busy(True)

        def work():
            try:
                idn = self.an.connect(self.addr.get().strip() or None)
                self.root.after(0, lambda: self.status.configure(
                    text=elide(idn, STATUS_CHARS), foreground="#060"))
                self.log(f"Connected: {idn}")
                self.log(f"Address:   {self.an.addr}")
                self.root.after(0, lambda: self.addr.set(self.an.addr))
                values = self.read_all_settings(retry_all=True)
                self.root.after(0, lambda v=values: self.show_settings(v))
            except Exception as exc:
                self.root.after(0, lambda: self.status.configure(
                    text="Not connected", foreground="#a00"))
                self.log(f"ERROR: {exc}")
            finally:
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def do_stop(self):
        self.abort.set()
        self.log("Stopping after the current step...")

    def do_stitch(self):
        """Fill the start-frequency list with segments that tile 0 Hz up to the
        stop frequency, each repeating a fixed number of the previous segment's
        frequency points.

        Overlapping by points rather than by hertz is what makes the pieces line
        up: stepping by (bins - overlap) spacings puts segment n+1's first point
        exactly on segment n's (400 - overlap)th, so the shared points are the
        same frequencies in both runs and can be matched or averaged directly.
        Zero overlap still leaves no gap - the next segment starts one spacing
        past the last point of the one before."""
        if self.busy:
            return
        try:
            stop = float(self.stitch_stop.get())
            overlap = int(float(self.stitch_overlap.get()))
        except ValueError:
            self.log("Stitch stop and overlap must be numbers.")
            return
        if not 0 <= overlap < N_BINS:
            self.log(f"Overlap must be from 0 to {N_BINS - 1} points.")
            return
        if self.edited("SPAN"):
            self.log("Apply the span change first - the point spacing is read "
                     "from the analyzer, so it would use the old span.")
            return

        if not self.an.inst:
            # Offline: the span table is all there is, and its printed values
            # are rounded, so say so rather than quietly stepping slightly wrong.
            try:
                code = SPAN_CHOICES.index(self.set_vars["SPAN"].get())
            except ValueError:
                self.log("Connect first, or pick a span, to fill the stitch.")
                return
            self.fill_stitch(span_hz(code) / N_BINS, stop, overlap,
                             "estimated from the rounded span table")
            return

        self.set_busy(True)

        def work():
            try:
                spacing = self.an.bin_spacing(TRACE, N_BINS)
                self.root.after(0, lambda: self.fill_stitch(
                    spacing, stop, overlap, "measured on the analyzer"))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.an.recover()
            finally:
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def fill_stitch(self, spacing, stop, overlap, source):
        """Main thread. Lay the segments out at the given point spacing.

        No segment may start so high that its span runs off the top of the
        band. The analyzer clamps a start frequency it cannot honour and says
        nothing, so a stitch that walked past 100 kHz used to hand it starts it
        quietly moved - two of them landing on the same place and measuring the
        same band twice. The 259-segment sweep of 2026-08-26 asked for a last
        start of 99773 Hz at a span 387 Hz wide; the analyzer put it at 99613
        and the run cost the same either way.

        Where the band runs out before the stop frequency does, the last start
        is pulled down to the highest one the analyzer will take rather than
        dropped. The top of the band still gets measured; the last two segments
        simply share more points than the overlap asked for, which is the same
        kind of overlap the stitch is built on and costs nothing but a little
        repeated frequency.
        """
        step = (N_BINS - overlap) * spacing
        if step <= 0 or not np.isfinite(step):
            self.log(f"A point spacing of {spacing:g} Hz gives no usable step.")
            return
        span = N_BINS * spacing              # what one segment covers
        highest = MAX_FREQ - span            # a segment here ends on the top
        if highest < 0:
            self.log(f"One segment is {span:.6g} Hz wide, more than the "
                     f"analyzer's {MAX_FREQ:g} Hz band - nothing to stitch.")
            return
        asked, stop = stop, min(stop, MAX_FREQ)

        starts, f, too_many = [], 0.0, False
        while f <= stop + 1e-9:
            if f > highest + 1e-9:
                break
            if len(starts) >= MAX_LIST_ITEMS:
                too_many = True
                break
            starts.append(f)
            f += step
        if not starts:
            self.log(f"A stop frequency of {asked:g} Hz leaves nothing to "
                     f"measure.")
            return

        # The last segment measures up to its start plus (bins - 1) spacings.
        # Only pulled down when it was the band that ran out: stopping because
        # the list is full is a different thing, and moving the last start to
        # the top of the band would then leap over everything in between and
        # call the gap an overlap.
        shared = 0
        if (not too_many
                and starts[-1] + (N_BINS - 1) * spacing < stop - 1e-6
                and starts[-1] < highest - 1e-9):
            starts.append(highest)
            shared = N_BINS - int(round((starts[-1] - starts[-2]) / spacing))

        self.starts_txt.set(", ".join(f"{v:.10g}" for v in starts))
        top = starts[-1] + (N_BINS - 1) * spacing
        self.log(f"{len(starts)} segments of {N_BINS} points at "
                 f"{spacing:.6g} Hz per point ({source}), stepping "
                 f"{N_BINS - overlap} points ({step:.6g} Hz) to {top:.6g} Hz - "
                 f"neighbours share {overlap} point(s).")
        if asked > MAX_FREQ:
            self.log(f"  ({asked:g} Hz asked for; {MAX_FREQ:g} Hz is the top of "
                     f"the analyzer's band)")
        if too_many:
            self.log(f"  (stopped at {MAX_LIST_ITEMS} segments, which is as "
                     f"long a sweep as this will take - nothing above "
                     f"{top:.6g} Hz is covered. A wider span would reach the "
                     f"top in fewer runs.)")
        if shared:
            self.log(f"  (the last segment starts at {starts[-1]:.10g} Hz "
                     f"rather than {starts[-2] + step:.10g} Hz, which would "
                     f"have run past {MAX_FREQ:g} Hz. The band is still covered "
                     f"to the top; that segment shares {shared} points with the "
                     f"one before it instead of {overlap}.)")

    def do_action(self, name, fn, wait_ready=False):
        """One-shot instrument command from a button, then re-read the panel.

        `wait_ready` is for a command the analyzer goes away to work on. Reading
        the panel back straight away would spend a VISA timeout on every query
        it sends while the analyzer is busy, and drop those settings for the
        rest of the session - so the read waits until the analyzer is answering
        again."""
        if self.busy or not self.an.inst:
            return
        self.set_busy(True)

        def work():
            try:
                fn()
                self.log(f"{name} sent")
                if wait_ready:
                    waited = self.an.wait_ready(self.probe_query())
                    if waited is None:
                        self.log(f"  (still no answer after {READY_TIMEOUT_S:g} s"
                                 f" - reading the panel back anyway)")
                    else:
                        self.log(f"  analyzer answering again after "
                                 f"{waited:.1f} s")
                values = self.read_all_settings()
                self.root.after(0, lambda v=values: self.show_settings(v))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
            finally:
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def probe_query(self):
        """The query wait_ready polls with: the SPAN spelling this analyzer
        turned out to answer, so a wait cannot sit out its whole timeout on a
        query that was never going to be answered anyway."""
        return self.an.query_for("SPAN")

    def toggle_autorange(self):
        """Switch the analyzer's auto range on or off there and then.

        Only the checkbox fires this - setting the variable from a read-back
        does not invoke a Checkbutton's command - so a panel refresh cannot loop
        back round into another write."""
        if self.busy or not self.an.inst:
            # Put the box back where the analyzer last had it rather than
            # leaving it showing a state that was never sent.
            self.arng_live.set(self.set_inst.get("ARNG") == "Auto")
            self.log("Auto range needs a free connection - nothing sent.")
            return
        on = self.arng_live.get()
        self.do_action(f"auto range {'on' if on else 'off'}",
                       lambda: self.an.put(
                           self.an.command("ARNG", 1 if on else 0)))

    # -- range hold -------------------------------------------------------

    def do_arm_hold(self, rerange=True):
        """Arm the hold: optionally auto-range first, then pin whatever range
        the analyzer ends up on.

        `rerange=False` pins the range that is already set, which is what you
        want when the range was chosen by hand at the front panel - the usual
        case for a segmented measurement where each band has its own range
        picked to sit just under overload."""
        if self.busy or not self.an.inst:
            return
        self.abort.clear()
        self.set_busy(True)
        threading.Thread(target=self._arm_worker, args=(rerange,),
                         daemon=True).start()

    def _arm_worker(self, rerange):
        try:
            if rerange:
                seconds = self.float_of(self.arng_s, 15.0, "auto-range time")
                self.log(f"Range hold: auto-ranging for {seconds:g} s...")
                rng, overloads, polls = self.an.autorange(
                    seconds, stop=self.abort.is_set)
                self.log(f"  settled at {rng} dBV (overload on "
                         f"{overloads}/{polls} polls)")
            found = self.an.input_range()
            if found is None:
                self.log("ERROR: the analyzer would not report IRNG, so there "
                         "is nothing to pin to.")
                return
            self.an.pin_range(found)
            # Read it back rather than trusting the write: the analyzer clamps
            # a range it dislikes and says nothing about having done so.
            got = self.an.input_range()
            if got is not None and got != found:
                self.log(f"  (asked to pin {found:g} dBV, the analyzer reports "
                         f"{got:g} dBV - pinning that instead)")
                found = got
            self.pinned_range = found
            self.pinned_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.settle_due.set()
            name = self.set_name.get().strip()
            self.log(f"Range held at {found:g} dBV"
                     + (f" for set '{name}'" if name else "")
                     + " - ARNG is now manual and every trace will be checked "
                       "against it.")
            values = self.read_all_settings()
            self.root.after(0, lambda v=values: self.show_settings(v))
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.an.recover()
        finally:
            self.root.after(0, self.refresh_hold)
            self.root.after(0, lambda: self.set_busy(False))

    def do_release_hold(self):
        """Stop holding. The analyzer is left exactly where it is - releasing
        the hold is not a reason to move the range, only to stop checking it."""
        self.pinned_range = None
        self.pinned_at = ""
        self.log("Range hold released. The range is unchanged; it is simply no "
                 "longer pinned or checked.")
        self.refresh_hold()

    def refresh_hold(self):
        """Main thread. Keep the hold line and its buttons telling the truth."""
        held = self.pinned_range is not None
        name = self.set_name.get().strip()
        if held:
            self.hold_status.configure(
                text=elide(f"held at {self.pinned_range:g} dBV"
                           + (f" for '{name}'" if name else "")
                           + f", armed {self.pinned_at}", STATUS_CHARS),
                foreground="#060")
        else:
            self.hold_status.configure(
                text="not held - the analyzer is free to move its own range",
                foreground="#c60")
        self.release_btn.configure(state="normal" if held and not self.busy
                                   else "disabled")

    def check_hold(self, snap):
        return hold_notes(self.pinned_range, snap,
                          set_name=self.set_name.get().strip(),
                          armed_at=self.pinned_at)

    # -- compare ----------------------------------------------------------

    def log_once(self, text):
        """A warning about a loaded sequence, said once. refs_for() runs on
        every redraw, and a sweep redraws once a run."""
        if text not in self.said:
            self.said.add(text)
            self.log(text)

    def do_compare_add(self):
        """Pick saved captures and group them into sequences to draw against."""
        paths = filedialog.askopenfilenames(
            title="Pick the CSVs of the sequences to compare",
            initialdir=self.outdir.get() or ".",
            filetypes=[("Capture CSVs", "*.csv"), ("All files", "*.*")])
        if not paths:
            return
        found = load_sequences(paths, log=self.log)
        if not found:
            self.log("Nothing loadable in that selection.")
            return
        by_name = {s["name"]: s for s in self.compare}
        for seq in found:
            if seq["name"] in by_name:
                self.log(f"  (reloaded {seq['name']})")
            by_name[seq["name"]] = seq
        self.compare = sorted(by_name.values(), key=lambda s: s["name"])
        self.said.clear()
        self.log(f"Compare: {len(found)} sequence(s) loaded, "
                 f"{len(self.compare)} in all")
        for seq in self.compare:
            self.log(f"  {seq['name']}   {seq['segments']} seg   "
                     f"{pretty_units(seq['ylabel'])}   "
                     f"{seq['freqs'][0]:.6g} to {seq['freqs'][-1]:.6g} Hz")
        self.refresh_compare()
        if not self.busy:
            self.do_compare_plot()

    def do_compare_clear(self):
        self.compare = []
        self.said.clear()
        self.log("Compare: cleared. Nothing is drawn underneath any more.")
        self.refresh_compare()

    def refresh_compare(self):
        """Main thread. Keep the compare line and its buttons telling the
        truth."""
        n = len(self.compare)
        if n:
            self.compare_status.configure(
                text=elide(f"{n} sequence(s): "
                           + "; ".join(s["name"] for s in self.compare),
                           STATUS_CHARS),
                foreground="#060")
        else:
            self.compare_status.configure(text="nothing loaded",
                                          foreground="#666")
        state = "normal" if n and not self.busy else "disabled"
        self.compare_btn.configure(state=state)
        self.compare_clear_btn.configure(state="normal" if n else "disabled")

    def compare_scale(self):
        """The scale a comparison is drawn on: the first loaded sequence whose
        units this module recognises.

        Not simply the first one. A capture with no header, or one naming
        something the unit model does not know, would otherwise become the
        target that everything else has to convert to - and nothing can convert
        to an unknown scale, so every sequence that WAS on a known one would be
        dropped and the unreadable one left holding the plot on its own.
        """
        for seq in self.compare:
            if unit_parts(seq["ylabel"]) is not None:
                return seq["ylabel"]
        return self.compare[0]["ylabel"] if self.compare else ""

    def ref_label(self, seq, ylabel):
        name = f"{seq['name']}  ({seq['segments']} seg)"
        if seq["ylabel"] != ylabel:
            name += f", was {pretty_units(seq['ylabel'])}"
        return name

    def refs_for(self, ylabel):
        """The loaded sequences as traces on `ylabel`'s scale, or [].

        Converted here rather than when they were loaded, because what they have
        to match is whatever the plot they are going under turns out to be
        measured in - and that is decided by UNIT on the analyzer, which can
        move between one capture and the next. A sequence that cannot be put on
        this scale is left out and said once, rather than drawn on an axis that
        does not describe it: a dBVrms/sqrtHz trace and a Vpk/sqrtHz one differ
        by 160 dB, and stacking them would draw a straight line at zero and a
        floor off the bottom of the plot.
        """
        out = []
        for seq in self.compare:
            if seq["ylabel"] == ylabel:
                amps = seq["amps"]
            else:
                try:
                    amps = convert_amplitude(seq["amps"], seq["ylabel"], ylabel)
                except ValueError as exc:
                    self.log_once(f"  (compare: {seq['name']} left out - {exc})")
                    continue
                self.log_once(f"  (compare: {seq['name']} converted from "
                              f"{pretty_units(seq['ylabel'])} to "
                              f"{pretty_units(ylabel)})")
            out.append((seq["freqs"], amps, self.ref_label(seq, ylabel)))
        return out

    def do_compare_plot(self):
        """Draw the loaded sequences against each other, in colour.

        The one plot here where they are the subject rather than the backdrop,
        so they get the colours. Everything goes on the first sequence's scale,
        which keeps whichever was loaded first as the one that is not being
        converted, and the subtitle says what was.
        """
        if self.busy or not self.compare:
            return
        if Figure is None:
            self.log("matplotlib is not installed, so there is nothing to draw "
                     "the comparison with.")
            return
        ylabel = self.compare_scale()
        traces, converted = [], []
        for seq in self.compare:
            if seq["ylabel"] == ylabel:
                amps = seq["amps"]
            else:
                try:
                    amps = convert_amplitude(seq["amps"], seq["ylabel"], ylabel)
                except ValueError as exc:
                    self.log(f"  ({seq['name']} left out - {exc})")
                    continue
                converted.append(seq["name"])
            traces.append((seq["freqs"], amps, self.ref_label(seq, ylabel)))
        if not traces:
            self.log("Nothing left to plot once the units were checked.")
            return
        bits = [f"{len(traces)} sequence(s)"]
        if converted:
            bits.append(f"converted to {pretty_units(ylabel)}: "
                        + ", ".join(converted))
        bits.append(datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        outdir = self.target_dir()
        try:
            os.makedirs(outdir, exist_ok=True)
            base = unique_base(outdir,
                               f"{self.safe_title()}_compare_"
                               f"{datetime.datetime.now():%Y%m%d}", (".png",))
            # refs=() and not the default: here the loaded sequences ARE the
            # traces, and letting refs_for() add them again would draw every
            # one of them twice, once in colour and once in grey underneath.
            self.write_plot(base + ".png", traces,
                            self.plot_title(note="comparison"),
                            SUBTITLE_SEP.join(bits), ylabel,
                            "linear" if ylabel.startswith("dB") else "log",
                            refs=())
        except Exception as exc:
            self.log(f"ERROR: the comparison could not be written: {exc}")

    # -- settling ---------------------------------------------------------

    def settle_for(self, span_code):
        """The settle wait, with the panel's record-length figure.

        The flag is cleared once the wait has been served, and not before. It
        used to be cleared in a finally, which meant a settle cut short by Stop
        left the analyzer marked as settled: press Stop during the wait after a
        span change, press GRAB again, and the trace came off a filter chain
        still full of the previous span with nothing saying so."""
        recs = self.float_of(self.settle_recs, DEFAULT_SETTLE_RECS,
                             "settle record lengths")
        notes = self.an.settle(recs, span_code, stop=self.abort.is_set,
                               log=self.log)
        self.settle_due.clear()
        return notes

    def do_average(self):
        """Restart the average and wait it out, writing nothing.

        The [START] key and the wait that belongs with it: use it to leave a
        finished average on the screen, or to see how long one takes, without
        filling the output folder with captures nobody asked for. Stop ends the
        wait; it does not stop the analyzer, which carries on averaging."""
        if self.busy or not self.an.inst:
            return
        self.abort.clear()
        self.set_busy(True)
        threading.Thread(target=self._average_worker, daemon=True).start()

    def _average_worker(self):
        try:
            finishes, how = self.average_finishes()
            dwell = self.exp_wait()
            if not finishes and dwell <= 0:
                # Nothing to wait for and no dwell asked for, so STRT is the
                # button's whole job. Waiting on the completion bit anyway would
                # hold the GUI - and the analyzer's front panel - until the
                # timeout or Stop, which is what it used to do.
                self.an.start()
                self.log(f"Average restarted and left running - {how} has no "
                         f"finish to wait for. Nothing saved.")
                return
            if finishes:
                self.log(f"Average restarted - {how}, nothing will be saved.")
            else:
                self.log(f"Average restarted - {how} has no finish to wait for, "
                         f"so letting it build for {dwell:g} s. Nothing saved.")
            t0 = time.perf_counter()
            self.an.start()
            state = (self.an.wait_done(self.float_of(self.timeout_s, 600.0,
                                                     "timeout"),
                                       stop=self.abort.is_set)
                     if finishes else self.wait_out(dwell))
            elapsed = time.perf_counter() - t0
            if state == "done":
                self.an.autoscale()
                self.log(f"  {'average finished' if finishes else 'ran'} in "
                         f"{elapsed:.1f} s")
            else:
                self.log(f"  average {state} after {elapsed:.1f} s "
                         f"(the analyzer is still running it)")
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.an.recover()
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def do_peek(self):
        """Draw the trace as it stands, into the window only.

        Deliberately does not restart the average, range or settle: it reads the
        settings and the trace and nothing else, so an average part way through
        is left exactly as it was and can be looked at again as it builds."""
        if self.busy or not self.an.inst:
            return
        self.abort.clear()
        self.set_busy(True)
        threading.Thread(target=self._peek_worker, daemon=True).start()

    def _peek_worker(self):
        try:
            snap = self.read_all_settings()
            self.root.after(0, lambda v=snap: self.show_settings(v))
            ylabel = trace_units(snap)
            # A peek saves nothing, so there is no metadata to carry this - it
            # has to be said in the log or the picture is read at face value.
            bad_read = readout_fault(snap)
            if bad_read:
                self.log(f"  *** SUSPECT: {bad_read} ***")
            binary = self.binary.get() and binary_valid(snap)
            if self.binary.get() and not binary:
                self.log(f"  ({binary_refusal(snap)})")
            t0 = time.perf_counter()
            if binary:
                try:
                    freqs, amps = self.an.trace_binary(TRACE, N_BINS,
                                                       reads_in_db(snap))
                except ValueError as exc:
                    self.log(f"  ({exc} - reading bin by bin instead)")
                    binary = False
            if not binary:
                # 800 queries takes long enough that Stop has to reach it, and
                # the progress callback is the only place inside the readout
                # where it can be looked at.
                def tick(i, n):
                    self.log(f"    {i}/{n} bins")
                    if self.abort.is_set():
                        raise KeyboardInterrupt
                freqs, amps = self.an.trace_ascii(TRACE, N_BINS, progress=tick)
            i = int(np.argmax(amps))
            # .4g rather than .2f: a volt-unit trace is a handful of nanovolts,
            # which two decimal places would round away to 0.00.
            self.log(f"Peek: {len(freqs)} points in "
                     f"{time.perf_counter() - t0:.2f} s, peak {amps[i]:.4g} "
                     f"{pretty_units(ylabel)} at {freqs[i]:.6g} Hz - "
                     f"nothing saved")
            if Figure is None:
                self.log("  (no matplotlib: there is nothing to draw it with)")
                return
            png = self.plot_png(
                [(freqs, amps, "peek")], self.plot_title(note="peek"),
                self.plot_subtitle(snap, freqs), ylabel, trace_yscale(snap))
            self.root.after(0, lambda d=png: self.show_peek(d))
        except KeyboardInterrupt:
            self.log("Peek stopped part way through the readout - nothing shown.")
            self.an.recover()
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.an.recover()
        finally:
            # Not the grab path: a peek is not a run and saves no config.
            self.root.after(0, lambda: self.set_busy(False))

    def do_grab(self):
        """One capture, at whatever the analyzer is set to.

        The sweep boxes are not read. They used to be - GRAB swept if they
        happened to be filled in and took a single capture if they were not -
        so what the button did depended on the contents of three boxes
        somewhere else on the panel, and a stitch left in them turned the next
        single capture into an eight-hour run. RUN SWEEP is the other one.
        """
        if self.busy or not self.an.inst:
            return
        self.abort.clear()
        self.set_busy(True)
        threading.Thread(target=self._grab_worker,
                         args=([""], [None], [None]), daemon=True).start()

    def sweep_lists(self):
        """The sweep boxes as (cases, starts, spans), or None if they will not
        parse. Empty lists stay empty here - the caller decides what that
        means, which is 'nothing to sweep' for RUN SWEEP."""
        try:
            spans = [int(round(v)) for v in parse_list(self.spans_txt.get())]
            for code in spans:
                if not 0 <= code < len(SPANS):
                    raise ValueError(f"span code {code} is not in 0-19")
            starts = parse_list(self.starts_txt.get())
        except ValueError as exc:
            self.log(f"Sweep list: {exc}")
            return None
        cases = [c.strip() for c in self.cases_txt.get().split(",") if c.strip()]
        return cases, starts, spans

    def do_sweep(self):
        """Every case, at every start frequency, at every span."""
        if self.busy or not self.an.inst:
            return
        lists = self.sweep_lists()
        if lists is None:
            return
        cases, starts, spans = lists
        if not (cases or starts or spans):
            self.log("Nothing to sweep: fill in span codes, start frequencies "
                     "or cases first. GRAB one takes a single capture at the "
                     "current settings.")
            return
        self.abort.clear()
        self.set_busy(True)
        threading.Thread(target=self._grab_worker,
                         args=(cases or [""], starts or [None], spans or [None]),
                         daemon=True).start()

    def _grab_worker(self, cases, starts, spans):
        """Wrapper. The buttons are handed back here and nowhere else.

        A worker that dies on its way out of the run leaves set_busy(True) in
        force, and there is no way back from that: every action returns early on
        self.busy, Connect is disabled, and Stop only sets the abort flag. Under
        pythonw the traceback has no console to reach either, so the app simply
        stops responding and has to be restarted. The allocation below used to
        sit outside the try, which is exactly where an oversized sweep raises.
        """
        try:
            self._grab_runs(cases, starts, spans)
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            self.root.after(0, self.save_config)
            self.root.after(0, lambda: self.set_busy(False))

    def _grab_runs(self, cases, starts, spans):
        outdir = self.target_dir()
        stamp = datetime.datetime.now().strftime("%Y%m%d")
        total = len(cases) * len(starts) * len(spans)
        if total > MAX_SWEEP_RUNS:
            self.log(f"That is {total} runs ({len(cases)} case(s) x "
                     f"{len(starts)} start freq(s) x {len(spans)} span(s)), "
                     f"past the {MAX_SWEEP_RUNS} this will set matrices aside "
                     f"for. Trim the sweep lists.")
            return

        # Axes kept in the shape the bench scripts used, so a sweep with one
        # case squeezes down to their [variable, span, bin] matrices. Cells are
        # NaN until a run fills them: a sweep that ends early is then missing
        # data rather than carrying a floor of zeros into the analysis, and the
        # axes still line up with the values written to the JSON beside them.
        freqs_m = np.full((len(cases), len(starts), len(spans), N_BINS), np.nan)
        amps_m = np.full_like(freqs_m, np.nan)
        done = []
        # What each segment was measured under, kept so the sweep can describe
        # itself once instead of leaving a .txt beside every trace.
        records = []
        ended = ""
        # A sweep writes one set of files for the whole thing. 65 segments used
        # to leave 195 behind, and every one of them is in the combined output
        # too - the same trace, the same settings, a folder that takes a
        # scroll to read. A single capture is still a single capture.
        segments = total == 1 or self.keep_segments.get()
        # The y axis the combined plot will carry, taken from the snapshot of
        # the last run that actually happened rather than from self.last_ylabel:
        # that is updated on the main thread through after(0, ...) and this runs
        # on the worker, so reading it here is a race that can label a sweep
        # with the previous capture's units.
        sweep_ylabel, sweep_yscale = self.last_ylabel, self.last_yscale

        # Wall clock for the whole sweep, and the part of it spent waiting for a
        # human rather than for the analyzer. The ETA is rescaled by what the
        # finished runs took, so a twenty minute pause to move a cable must not
        # be read as evidence that the runs are slow.
        t_sweep = time.perf_counter()
        paused_s = 0.0
        plan = []
        # Whether the window follows the combined plot as it builds. Off only
        # for a single grab, which has nothing to build. Not tied to the
        # combined plot tick: that says whether to SAVE the picture, and now
        # that a sweep no longer writes a plot per segment, letting it decide
        # the live view as well would leave the window blank for the whole run.
        building = total > 1 and Figure is not None
        self.last_progress = 0.0

        try:
            os.makedirs(outdir, exist_ok=True)
            if total > 1:
                self.log(f"Sweep: {len(cases)} case(s) x {len(starts)} start "
                         f"freq(s) x {len(spans)} span(s) = {total} runs")
                plan = self.sweep_plan(cases, starts, spans)
                if plan:
                    done_at = (datetime.datetime.now() + datetime.timedelta(
                        seconds=sum(plan)))
                    self.log(f"  about {fmt_hms(sum(plan))} of measuring, "
                             f"finishing around {done_at:%H:%M}"
                             + (" (pauses between cases not counted)"
                                if len(cases) > 1 and self.pause_cases.get()
                                else ""))
                else:
                    self.log("  (no time estimate: the analyzer would not say "
                             "what span or averaging it is on)")

            for ic, case in enumerate(cases):
                if case and self.pause_cases.get():
                    t_pause = time.perf_counter()
                    go = self.confirm(f"Set up case '{case}', then continue.")
                    paused_s += time.perf_counter() - t_pause
                    if not go:
                        raise KeyboardInterrupt
                for isf, start in enumerate(starts):
                    if start is not None:
                        # .10g, not %g: a stitch step is rarely a round number
                        # and 6 digits would shave a fraction of a bin off it.
                        # Through command(), so that if this analyzer answers
                        # the graph-indexed spelling of STRF or SPAN the sweep
                        # moves with it - a bare "SPAN 11" against an analyzer
                        # that wanted "SPAN 0,11" is read as a graph number and
                        # silently changes nothing, which is the one failure the
                        # qform machinery exists to catch.
                        self.an.put(self.an.command("STRF", f"{start:.10g}"))
                        self.settle_due.set()
                    for isp, span in enumerate(spans):
                        if span is not None:
                            # Through write_settings rather than put(command()):
                            # a SPAN write silently reinstalls that span's
                            # default overlap, and this loop is what produced
                            # the 30 Aug sweep where NAVG 400 bought 6
                            # independent records at span 11. write_settings
                            # puts the held OVLP back afterwards, and still
                            # writes through command(), so the qform spelling
                            # above is honoured either way.
                            self.an.write_settings({"SPAN": span},
                                                   log=self.log)
                            self.settle_due.set()
                        if self.abort.is_set():
                            raise KeyboardInterrupt
                        label = " ".join(
                            p for p in (case,
                                        f"span {span}" if span is not None else "",
                                        f"start {start:g} Hz" if start else "")
                            if p)
                        if label:
                            self.log(f"--- {label}")
                        if plan:
                            self.log("  " + self.eta(
                                plan, len(done),
                                time.perf_counter() - t_sweep - paused_s))
                        freqs, amps, snap, notes = self.run_one()
                        freqs_m[ic, isf, isp] = freqs
                        amps_m[ic, isf, isp] = amps
                        sweep_ylabel = trace_units(snap)
                        sweep_yscale = trace_yscale(snap)
                        done.append((freqs, amps, label or self.safe_title()))
                        records.append({"case": case, "span": code_of(snap,
                                                                      "SPAN"),
                                        "freqs": freqs, "amps": amps,
                                        "snap": snap, "notes": notes,
                                        "units": sweep_ylabel})
                        if segments:
                            self.save_run(outdir, stamp, case, freqs, amps,
                                          snap, notes, show=not building)
                        if building:
                            self.show_sweep_so_far(done, total, sweep_ylabel,
                                                   sweep_yscale)
                        self.root.after(0, lambda v=snap: self.show_settings(v))
        except KeyboardInterrupt:
            ended = "stopped"
        except Exception as exc:
            ended = "failed"
            self.log(f"ERROR: {exc}")

        try:
            self.an.lock_panel(False)
        except Exception:
            pass

        # Whatever ended the sweep, write up the runs that did complete: the
        # point of stopping early is usually that the segments captured so far
        # are enough, so they still get their matrices and their combined plot.
        if ended:
            self.log(f"{ended.capitalize()} after {len(done)} of {total} run(s).")
        try:
            if done and total > 1:
                self.save_sweep(outdir, stamp, cases, starts, spans,
                                freqs_m, amps_m, done, total, ended,
                                sweep_ylabel, sweep_yscale, records)
        except Exception as exc:
            self.log(f"ERROR: the sweep files could not be written: {exc}")

    def sweep_plan(self, cases, starts, spans):
        """What each run of the sweep should cost, in seconds, in the order the
        loop will walk them. [] when the analyzer will not say.

        Read once, before the loop rather than per run: NAVG, the averaging mode
        and the overlap are what set the per-capture time, and they are worth
        asking the analyzer for rather than guessing at. The span is read too,
        for the runs that leave it where it is.
        """
        try:
            snap = self.read_settings("SPAN", "NAVG", "AVGO", "AVGM", "OVLP",
                                      "DISP0", "UNIT0")
        except Exception:
            return []
        codes = [code_of(snap, "SPAN") if s is None else s for s in spans]
        if any(c is None for c in codes):
            return []
        # Only a linear average that is switched on has a length of its own.
        # Anything else runs for the exponential wait, which is what run_one
        # will sit out.
        averaged = (code_of(snap, "AVGO", 0) == 1
                    and code_of(snap, "AVGM", 0) == 0)
        # A settle is due whenever the sweep writes a span or a start frequency,
        # and whenever a hold re-asserts the range before the capture.
        settles = (any(s is not None for s in spans)
                   or any(s is not None for s in starts)
                   or self.pinned_range is not None)
        autorange = 0.0
        if self.pinned_range is None and self.do_arng.get():
            autorange = self.float_of(self.arng_s, 15.0, "auto-range time")
        per = [capture_time(
            code,
            navg=code_of(snap, "NAVG"), ovlp=value_of(snap, "OVLP"),
            averaged=averaged,
            settle_recs=(self.float_of(self.settle_recs, DEFAULT_SETTLE_RECS,
                                       "settle record lengths")
                         if settles else 0.0),
            extra_settle_s=self.float_of(self.settle_s, 0.0, "settle"),
            autorange_s=autorange,
            exp_wait_s=self.exp_wait(),
            timeout_s=self.float_of(self.timeout_s, 600.0, "timeout"),
            transfer_s=(TRANSFER_BINARY_S
                        if self.binary.get() and binary_valid(snap)
                        else TRANSFER_ASCII_S)) for code in codes]
        if any(not np.isfinite(t) for t in per):
            return []
        return [t for _ in cases for _ in starts for t in per]

    @staticmethod
    def eta(plan, finished, measuring_s):
        """"run 3/20 - 4m12s in, about 11m30s left, done ~15:42".

        `measuring_s` is wall time with any pause for a case change taken back
        out of it: a sweep that waited twenty minutes for someone to move a
        cable has not learned that its runs are slow.

        The plan is rescaled by what the finished runs actually took, so the
        estimate stops being a model as soon as there is a measurement to
        replace it with - which is also what absorbs the overlap that
        capture_time can only put a floor under.

        The correction is weighted in rather than applied whole, because one run
        is not a measurement of anything. A first segment that timed out, or was
        read early, would otherwise set the ratio for the entire sweep on its
        own: an instant run against a 109 s plan says "about 0.0s left" with
        fifteen minutes still to go. At n runs the measurement carries n/(n+2)
        of the answer, so it leads by the third or fourth and the model is gone
        by the tenth.
        """
        total = len(plan)
        spent = sum(plan[:finished])
        scale = 1.0
        if finished and spent > 0:
            weight = finished / (finished + 2.0)
            scale = weight * (measuring_s / spent) + (1.0 - weight)
        left = sum(plan[finished:]) * scale
        if not np.isfinite(left):
            return f"run {finished + 1}/{total}"
        done_at = datetime.datetime.now() + datetime.timedelta(seconds=left)
        return (f"run {finished + 1}/{total} - {fmt_hms(measuring_s)} in, "
                f"about {fmt_hms(left)} left, done ~{done_at:%H:%M}"
                + ("" if finished else " (estimated)"))

    def run_one(self):
        """One measurement: range, average, read out. Returns the trace, the
        settings snapshot it was taken under and the notes for the metadata."""
        notes = {}
        locked = self.lock.get()
        if locked:
            self.an.lock_panel(True)
            self.log("  front panel locked")
        try:
            # The span is needed before the run, not after: the record length
            # sets the settle and every statistic below rests on it.
            span_code = code_of(self.read_settings("SPAN"), "SPAN")

            if self.pinned_range is not None:
                # Re-assert the pin every run. The analyzer will move its own
                # range on an overload if anything has put ARNG back to auto -
                # the front panel can, and so can a Defaults apply.
                self.an.pin_range(self.pinned_range)
                self.settle_due.set()
            elif self.do_arng.get():
                seconds = self.float_of(self.arng_s, 15.0, "auto-range time")
                rng, overloads, polls = self.an.autorange(
                    seconds, stop=self.abort.is_set)
                notes["auto range"] = (f"{seconds:g} s, settled at {rng} dBV, "
                                       f"overload on {overloads}/{polls} polls")
                self.log(f"  auto-range done, range {rng} dBV "
                         f"(overload on {overloads}/{polls} polls)")
                self.settle_due.set()

            if self.settle_due.is_set():
                notes.update(self.settle_for(span_code))
            settle = self.float_of(self.settle_s, 0.0, "settle")
            if settle > 0:
                time.sleep(settle)
                notes["extra settle (s)"] = f"{settle:g}"
            if self.abort.is_set():
                # Stopped while ranging or settling: no point restarting the
                # average just to abandon it on the first poll.
                raise KeyboardInterrupt

            finishes, how = self.average_finishes()
            t0 = time.perf_counter()
            if finishes:
                self.log("  measuring...")
                self.an.start()
                state = self.an.wait_done(
                    self.float_of(self.timeout_s, 600.0, "timeout"),
                    stop=self.abort.is_set)
            else:
                # No completion bit is coming, so the run length has to be said
                # rather than waited for. The measurement timeout is not it -
                # that is the limit on a wait that normally ends by itself, and
                # using it here made every such capture take the full ten
                # minutes.
                dwell = self.exp_wait()
                self.log(f"  measuring... ({how} has no finish to wait for, so "
                         f"the trace is read after {dwell:g} s)")
                self.an.start()
                state = self.wait_out(dwell)
            measured = time.perf_counter() - t0
            if state == "stopped":
                raise KeyboardInterrupt
            if state == "done" and not finishes:
                # Not a measurement that finished on its own: it ran for as long
                # as it was told to, and the metadata should say which.
                state = f"{measured:.0f} s of {how}"
                self.log(f"  read after {measured:.1f} s")
            elif state != "done":
                self.log(f"  (measurement {state} after {measured:.1f} s - "
                         f"reading the trace as it stands)")
            else:
                self.log(f"  measured in {measured:.1f} s")
            notes["measure time (s)"] = f"{measured:.3f}"
            notes["measurement"] = state

            # Straight after the run and before anything else can clear the
            # byte. An overload here need not show on the trace at all: the
            # input stage sees everything the anti-alias filter passes, so
            # out-of-band content saturates it while the displayed band looks
            # clean.
            status = self.an.refresh_status(log=self.log)
            over = status.overloaded
            notes["overload"] = status.describe()

            self.an.autoscale()

            # Read the settings before the transfer: the display mode decides
            # whether the binary dump is valid, and the same snapshot goes in
            # the metadata and the panel.
            snap = self.read_all_settings()

            # One verdict, listing every reason: an overload must not be hidden
            # by a range that happened to hold, nor the other way round.
            # `hold` rather than `hold_notes`: that name is the library
            # function this method also needs, via check_hold.
            hold, hold_ok = self.check_hold(snap)
            notes.update(hold)
            faults = [] if hold_ok else [hold["trace quality"]
                                         .removeprefix("SUSPECT: ")]
            if over:
                faults.append("overload flagged during the run")
            elif not status.complete:
                # Half a status byte is not a clean one: an ERRS that came back
                # clear says nothing about the FFT overload in FFTS. Same
                # standard the range hold is held to - unverified and verified
                # are different claims, and only one belongs on a trace a
                # comparison rests on.
                silent = " and ".join(name for name, value
                                      in (("ERRS", status.errs),
                                          ("FFTS", status.ffts))
                                      if value is None)
                faults.append(f"overload unverified: {silent} did not answer")
            # Which scale the trace is labelled on, and whether that was read or
            # assumed. An assumed UNIT0 is a 160 dB assumption.
            bad_read = readout_fault(snap)
            if bad_read:
                faults.append(bad_read)
            bad_avg = averaging_fault(snap)
            if bad_avg:
                faults.append(bad_avg)
            # What the overlap has done to NAVG, and whether it looks like the
            # span put it there. The statistics below do not depend on this -
            # record_stats counts from the clock - but NAVG is the number that
            # ends up quoted, and this says what it is really worth. Given the
            # span from the snapshot, falling back to the one read before the
            # run so a dead SPAN does not silence the check.
            bad_ovlp = overlap_fault(snap, code_of(snap, "SPAN", span_code))
            if bad_ovlp:
                faults.append(bad_ovlp)
            notes["trace quality"] = ("SUSPECT: " + "; ".join(faults) if faults
                                      else hold.get("trace quality", "clean"))
            if faults:
                self.log(f"  *** {notes['trace quality']} ***")
            # `averaged` because the elapsed/T_rec count assumes the analyzer
            # averaged what it acquired. With AVGO off it averages nothing and
            # keeps only the newest record, so the run length buys no statistics
            # at all and the error bar is a single bin's own. An unreadable AVGO
            # takes the same branch, which is the safe direction to be wrong in,
            # and averaging_fault has already said so above.
            notes.update(stats_notes(record_stats(
                code_of(snap, "SPAN", span_code), measured,
                navg=code_of(snap, "NAVG"), ovlp=value_of(snap, "OVLP"),
                averaged=code_of(snap, "AVGO", 0) == 1)))
            binary = self.binary.get() and binary_valid(snap)
            if self.binary.get() and not binary:
                self.log(f"  ({binary_refusal(snap)})")

            t0 = time.perf_counter()
            if binary:
                try:
                    freqs, amps = self.an.trace_binary(TRACE, N_BINS,
                                                       reads_in_db(snap))
                except ValueError as exc:
                    self.log(f"  ({exc} - reading bin by bin instead)")
                    binary = False
            if not binary:
                freqs, amps = self.an.trace_ascii(
                    TRACE, N_BINS,
                    progress=lambda i, n: self.log(f"    {i}/{n} bins"))
            transferred = time.perf_counter() - t0
            notes["transfer"] = ("binary dump (SPEB?)" if binary
                                 else "bin by bin (BVAL?/SPEC?)")
            notes["transfer time (s)"] = f"{transferred:.3f}"
            self.log(f"  {len(freqs)} points in {transferred:.2f} s "
                     f"({'binary' if binary else 'ascii'})")
            return freqs, amps, snap, notes
        finally:
            if locked:
                self.an.lock_panel(False)
                self.log("  front panel unlocked")

    def save_run(self, outdir, stamp, case, freqs, amps, snap, notes,
                 show=True):
        """CSV, plot and metadata for one measurement, under one shared base
        name so the three files of a capture always belong together.

        `show=False` still writes all three; it only keeps this one trace out of
        the preview, which during a sweep belongs to the combined plot."""
        code = code_of(snap, "SPAN")
        start = float(freqs[0])
        maxfreq = round(float(np.max(freqs)))
        ylabel = trace_units(snap)

        parts = [self.safe_title()]
        if case:
            parts.append(safe_name(case))
        if code is not None:
            parts.append(f"span{code}")
        if start:
            parts.append(f"strf{start:g}Hz")
        parts += [f"{maxfreq}Hz", stamp]
        if not (self.save_csv.get() or self.save_png.get()
                or self.save_txt.get()):
            self.log("  (nothing ticked to save)")
            return
        # Every extension, not only the ticked ones. unique_base tests them
        # together so that a capture's three files share a name - but handing it
        # just what this run is about to write lets a run with the metadata
        # unticked land on a stem an earlier run's .txt already holds, and the
        # two then read as one capture while describing different settings.
        base = unique_base(outdir, "_".join(parts), (".csv", ".png", ".txt"))
        stem = os.path.basename(base)

        if self.save_csv.get():
            write_csv(base + ".csv", freqs, amps, ylabel)
            self.log(f"  {stem}.csv")
        if self.save_txt.get():
            extra = {
                "span": (f"{code} - {SPANS[code][0]}"
                         if code is not None and 0 <= code < len(SPANS) else "?"),
                "start frequency (Hz)": f"{start:g}",
                "stop frequency (Hz)": f"{float(freqs[-1]):g}",
                "bins": str(len(freqs)),
                "trace units": ylabel,
            }
            extra.update(notes)
            with open(base + ".txt", "w", encoding="utf-8") as fh:
                fh.write(metadata_text(self.an, snap, extra, self.command))
        if self.save_png.get():
            self.write_plot(base + ".png", [(freqs, amps, stem)],
                            self.plot_title(case),
                            self.plot_subtitle(snap, freqs, notes), ylabel,
                            trace_yscale(snap), show=show)

    # -- plots ------------------------------------------------------------

    def plot_title(self, case="", note=""):
        """What the plot is of, in the words that were typed - the file name is
        already on the file, and its underscored, span-coded form made a poor
        heading for a picture that ends up in a talk or a logbook."""
        parts = [self.title.get().strip() or "sr760"]
        if case:
            parts.append(case)
        title = " - ".join(parts)
        return f"{title}  ({note})" if note else title

    @staticmethod
    def plot_subtitle(snap, freqs, notes=None):
        """The line under the title: the few settings that decide what the trace
        means, spelled out. The rest stays in the metadata file."""
        bits = []
        code = code_of(snap, "SPAN")
        if code is not None and 0 <= code < len(SPANS):
            bits.append(f"span {code} - {SPANS[code][0]}")
        if len(freqs):
            bits.append(f"{float(freqs[0]):g} to {float(freqs[-1]):g} Hz")
        window = label_of(snap, "WNDO")
        if window:
            bits.append(f"{window} window")
        if code_of(snap, "AVGO", 0) == 1:
            n, kind = code_of(snap, "NAVG"), label_of(snap, "AVGT")
            bits.append(f"peak hold over {n or '?'} records" if kind == "Peak hold"
                        else " ".join(p for p in (str(n) if n else "", kind,
                                                  "averages") if p))
        else:
            bits.append("no averaging")
        # A measurement that timed out or was stopped was read off the screen
        # part way through, which the picture itself gives no hint of.
        state = (notes or {}).get("measurement")
        if state and state != "done":
            bits.append(f"measurement {state}")
        # An overload or a range that slipped its pin is invisible in the trace
        # by construction, so the plot has to carry the warning or a suspect
        # capture looks exactly like a good one.
        quality = (notes or {}).get("trace quality", "")
        if quality.startswith("SUSPECT"):
            bits.append(quality)
        err = (notes or {}).get("relative error (1 sigma)")
        if err and err != "?":
            bits.append(f"1 sigma {err}")
        bits.append(datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        return SUBTITLE_SEP.join(bits)

    def y_window(self, ylabel):
        """The dB plot window, or (None, None) for a linear trace, which is
        autoscaled instead."""
        if not ylabel.startswith("dB"):
            return None, None
        return (self.float_of(self.ymin, DEFAULT_YMIN, "y min"),
                self.float_of(self.ymax, DEFAULT_YMAX, "y max"))

    def plot_png(self, traces, title, subtitle, ylabel, yscale="linear",
                 refs=None):
        """The same plot as a PNG in memory, for the peek. Drawn coarser than a
        saved one: it is only ever seen scaled down into the preview box."""
        refs = self.refs_for(ylabel) if refs is None else refs
        buf = io.BytesIO()
        save_plot(buf, traces, title, subtitle, ylabel, *self.y_window(ylabel),
                  dpi=PEEK_DPI, yscale=yscale, refs=refs)
        self.remember_plot(traces, title, subtitle, ylabel, yscale, refs)
        return buf.getvalue()

    def write_plot(self, path, traces, title, subtitle, ylabel,
                   yscale="linear", show=True, refs=None):
        """`show=False` writes the file without taking over the preview and the
        zoom window. That is what a sweep wants for its per-run plots: they are
        still saved, but the window is showing the combined picture building up
        and must not flicker through each segment on its own on the way."""
        if Figure is None:
            self.log("  (no matplotlib: skipping the plot)")
            return
        refs = self.refs_for(ylabel) if refs is None else refs
        try:
            save_plot(path, traces, title, subtitle, ylabel,
                      *self.y_window(ylabel), yscale=yscale, refs=refs)
        except Exception as exc:
            self.log(f"  (plot failed: {exc})")
            return
        self.log(f"  {os.path.basename(path)}")
        if not show:
            return
        self.remember_plot(traces, title, subtitle, ylabel, yscale, refs)
        self.root.after(0, lambda p=path: self.show_preview(p))

    def show_sweep_so_far(self, done, planned, ylabel, yscale, force=False):
        """Draw the sweep as far as it has got, into the window only.

        The combined plot used to appear once, at the end, which is a long time
        to wait to notice that segment three is sitting on the wrong range or
        that the overlaps are not joining. Every trace so far goes on one pair
        of axes, in the same colours and through the same drawing code the saved
        PNG uses, so what builds up in the window is the picture that gets
        written at the end.

        Nothing is saved here - `save_sweep` still writes the file once, when
        the sweep is over. Returns whether it drew.
        """
        if Figure is None or not done:
            return False
        now = time.perf_counter()
        if not force and now - self.last_progress < PROGRESS_MIN_S:
            return False
        self.last_progress = now
        subtitle = SUBTITLE_SEP.join(
            [f"{len(done)} of {planned} runs so far",
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M")])
        try:
            png = self.plot_png(done, self.plot_title(note="sweep building"),
                                subtitle, ylabel, yscale)
        except Exception as exc:
            self.log(f"  (progress plot failed: {exc})")
            return False
        self.root.after(0, lambda d=png, n=len(done): self.show_building(
            d, n, planned))
        return True

    @staticmethod
    def sweep_groups(records, cases):
        """The sweep's segments split into the sets that belong in one file:
        one per (case, span), in the order the loop walked them.

        Start frequencies tile a band, so they join into one curve - that is
        what a stitch is. A case and a span do not. A case is whatever you
        changed by hand between runs, so those are separate measurements by
        construction. A span changes the bin width, and with it the resolution
        and the noise bandwidth of every point: two spans over one band are two
        measurements of it, not two pieces of one, and sorting them into a
        single frequency column interleaves rows of different resolutions into
        something no reader can take apart again.

        The span only enters the file name when the sweep used more than one,
        so a stitch at a single span keeps the name it has always had.
        """
        many_spans = len({r["span"] for r in records}) > 1
        groups, order = {}, []
        for case in cases:
            for r in records:
                if r["case"] != case:
                    continue
                key = (case, r["span"])
                if key not in groups:
                    parts = ([safe_name(case)] if case else [])
                    if many_spans:
                        parts.append("span"
                                     + ("?" if r["span"] is None
                                        else str(r["span"])))
                    groups[key] = {"case": case, "span": r["span"], "rows": [],
                                   "suffix": ("_" + "_".join(parts)
                                              if parts else "")}
                    order.append(key)
                groups[key]["rows"].append(r)
        return [groups[k] for k in order]

    def write_sweep_csv(self, base, records, cases, ylabel):
        """The sweep's traces, one CSV per (case, span), sorted into one curve.

        The third column says which segment each point came from, so nothing is
        lost by joining them - the overlaps stay separable, and a two-column
        reader that ignores it still sees the stitch.
        """
        written = []
        for group in self.sweep_groups(records, cases):
            rows = group["rows"]
            freqs = np.concatenate([r["freqs"] for r in rows])
            amps = np.concatenate([r["amps"] for r in rows])
            seg = np.concatenate([np.full(len(r["freqs"]), i, dtype=float)
                                  for i, r in enumerate(rows, 1)])
            order = np.argsort(freqs, kind="stable")
            path = base + group["suffix"] + ".csv"
            np.savetxt(path, np.column_stack([freqs[order], amps[order],
                                              seg[order]]),
                       delimiter=",", comments="",
                       header=f"Frequency (Hz),{safe_name(ylabel)},segment")
            written.append((path, len(rows)))
        return written

    def sweep_metadata_text(self, base, records, cases, starts, spans,
                            planned, ended, ylabel):
        """The whole sweep described once.

        Everything the per-segment files used to carry, minus the repetition.
        The settings block is written once, because a sweep holds the analyzer
        still apart from the span and the start frequency it is moving on
        purpose; what varies goes in a table, one line a segment. A segment the
        run flagged is quoted in full underneath, because the whole point of
        the flag is that nothing on the trace shows it.
        """
        freqs = np.concatenate([r["freqs"] for r in records])
        extra = {
            "sweep": f"{len(cases)} case(s) x {len(starts)} start freq(s) x "
                     f"{len(spans)} span(s) = {planned} runs",
            "runs completed": str(len(records)),
            "ended early": ended or "-",
            "trace units": ylabel,
            "frequency range (Hz)": f"{np.min(freqs):g} to {np.max(freqs):g}",
            "bins per segment": str(N_BINS),
        }
        scales = sorted({r["units"] for r in records})
        if len(scales) > 1:
            extra["UNITS CHANGED"] = (f"{', '.join(scales)} - the segments are "
                                      f"not all on one scale")
        head = metadata_text(self.an, records[-1]["snap"], extra, self.command)

        # Grouped exactly as the CSVs are, and numbered the same way, so a row
        # here and a segment number there are the same segment.
        table, suspect = [], []
        for group in self.sweep_groups(records, cases):
            table += ["", f"segments of {os.path.basename(base)}"
                          f"{group['suffix']}.csv"
                          + (f"   (case '{group['case']}')"
                             if group["case"] else "")
                          + (f"   (span {group['span']})"
                             if group["span"] is not None else ""),
                      f"  {'#':>4} {'start (Hz)':>12} {'top (Hz)':>12} "
                      f"{'meas (s)':>9} {'N_indep':>8} {'1 sigma':>8}  "
                      f"{'overload':<10} quality"]
            for i, r in enumerate(group["rows"], 1):
                n = r["notes"]
                quality = n.get("trace quality", "")
                table.append(
                    f"  {i:>4} {float(r['freqs'][0]):>12.6g} "
                    f"{float(r['freqs'][-1]):>12.6g} "
                    f"{n.get('measure time (s)', '?'):>9} "
                    f"{n.get('independent records', '?'):>8} "
                    f"{n.get('relative error (1 sigma)', '?'):>8}  "
                    f"{n.get('overload', '?'):<10} "
                    f"{'SUSPECT' if quality.startswith('SUSPECT') else 'clean'}")
                if quality.startswith("SUSPECT"):
                    suspect.append(f"  {group['suffix'] or 'sweep'} #{i}: "
                                   f"{quality}")
        table.insert(0, "")
        table.insert(1, "the settings above are as read after the last segment")
        if suspect:
            table += ["", "suspect segments"] + suspect
        return head + "\n".join(table) + "\n"

    def save_sweep(self, outdir, stamp, cases, starts, spans, freqs_m, amps_m,
                   done, planned, ended="", ylabel=None, yscale=None,
                   records=()):
        """The whole sweep in one place: the raw matrices, a JSON note of what
        each axis means, and every trace on one pair of axes.

        Written the same way whether the sweep ran to the end or was stopped
        partway, so a run cut short still gives the concatenated picture of the
        segments it did capture. What is missing says so - unmeasured cells stay
        NaN, the JSON counts the runs, and the plot title carries the shortfall
        rather than passing a partial sweep off as a whole one."""
        ylabel = self.last_ylabel if ylabel is None else ylabel
        # Every name the sweep might write, so the whole set shares one stem
        # even when only some of them are ticked - the same rule save_run had
        # to be taught, for the same reason.
        suffixes = ["_freqs.npy", "_amps.npy", "_axes.json", ".png", ".txt"]
        suffixes += [g["suffix"] + ".csv"
                     for g in self.sweep_groups(records, cases)] or [".csv"]
        base = unique_base(outdir, f"{self.safe_title()}_sweep_{stamp}",
                           suffixes)
        if records and self.save_csv.get():
            for path, n in self.write_sweep_csv(base, records, cases, ylabel):
                self.log(f"  {os.path.basename(path)}  ({n} segments)")
        if records and self.save_txt.get():
            with open(base + ".txt", "w", encoding="utf-8") as fh:
                fh.write(self.sweep_metadata_text(base, records, cases,
                                                  starts, spans, planned,
                                                  ended, ylabel))
            self.log(f"  {os.path.basename(base)}.txt")
        if self.save_npy.get():
            np.save(base + "_freqs.npy", freqs_m)
            np.save(base + "_amps.npy", amps_m)
            axes = {
                "shape": "[case][start frequency][span][bin]",
                "cases": cases,
                "start_freqs_hz": ["current" if s is None else s for s in starts],
                "span_codes": ["current" if s is None else s for s in spans],
                "span_hz": ["current" if s is None else span_hz(s) for s in spans],
                "bins": N_BINS,
                "runs_planned": planned,
                "runs_completed": len(done),
                "ended_early": ended or None,
                "unmeasured": "NaN",
            }
            with open(base + "_axes.json", "w", encoding="utf-8") as fh:
                json.dump(axes, fh, indent=2)
            self.log(f"{os.path.basename(base)}_freqs.npy / _amps.npy "
                     f"{freqs_m.shape}"
                     + (f", {len(done)} of {planned} filled"
                        if len(done) < planned else ""))
        if self.combined.get():
            # Every run in a sweep is measured the same way, so they share a y
            # axis - the label comes from the settings snapshot of the last run
            # that happened, handed in by the caller. self.last_ylabel is the
            # fallback only: it is written on the main thread and read here on
            # the worker, so it can still be the previous capture's units.
            subtitle = SUBTITLE_SEP.join(
                [f"{len(done)} of {planned} runs"
                 + (f" - {ended}" if ended else ""),
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M")])
            self.write_plot(base + ".png", done, self.plot_title(note="sweep"),
                            subtitle, ylabel,
                            self.last_yscale if yscale is None else yscale)

    # -- settings panel ---------------------------------------------------

    def edited(self, key):
        """True if the panel value differs from what the analyzer last said."""
        return self.set_vars[key].get().strip() != self.set_inst[key]

    def averaging_worth(self):
        """What the Averaging boxes are worth, as (text, ok).

        Read off the panel rather than the analyzer, so it answers for what is
        typed and not yet applied - which is the moment the question is being
        asked. NAVG counts records the analyzer averaged, not independent ones,
        and SPAN reinstalls its own default overlap, so this is the difference
        between what was asked for and what the trace is worth.
        """
        def shown(key):
            return self.set_vars[key].get().strip() if key in self.set_vars \
                else ""

        def number(key):
            try:
                return float(shown(key))
            except ValueError:
                return float("nan")

        if shown("AVGO") != "On":
            return "averaging off:\none record, 1 sigma 100%", False
        kind = shown("AVGT")
        if kind and kind != "RMS":
            return f"{kind.lower()}:\nnot noise averaging", False
        if shown("AVGM") == "Exponential":
            return "exponential:\nno definite depth", False
        navg, ovlp = number("NAVG"), number("OVLP")
        n = independent_records(navg, ovlp)
        if not np.isfinite(n):
            return "set a number of\naverages", True
        rel = 1.0 / np.sqrt(n)
        # Two lines, always. "7.23 of 400" says the overlap has eaten the
        # averaging more plainly than a multiplier would, and a third line for
        # it did not fit the cell - it was drawn 8 px past the bottom.
        # 1.5 is the same threshold stats_notes calls out in the metadata.
        short = navg / n > 1.5
        return (f"worth {f'{n:.3g} of {navg:g}' if short else f'all {navg:g}'} "
                f"records\n1 sigma {rel:.3g} ({10 * np.log10(1 + rel):.2f} dB)",
                not short)

    def refresh_averaging(self):
        """Main thread. Keep the read-out under Averaging telling the truth."""
        text, ok = self.averaging_worth()
        self.avg_worth.configure(text=text, foreground="#060" if ok else "#c60")

    def refresh_marks(self):
        # Every settings box already reports its own keystrokes here, so the
        # averaging read-out rides along rather than tracing the vars twice.
        self.refresh_averaging()
        pending = 0
        for key, mark in self.set_marks.items():
            if self.edited(key):
                pending += 1
                mark.configure(text="*")
            else:
                mark.configure(text=" ")
        if not self.read_stamp:
            self.set_status.configure(text="not read yet", foreground="#666")
        elif pending:
            self.set_status.configure(
                text=f"{pending} edit(s) not applied - press Apply changes",
                foreground="#c60")
        else:
            self.set_status.configure(
                text=f"in sync with the analyzer ({self.read_stamp})",
                foreground="#060")

    def show_settings(self, values, overwrite=False):
        """Main thread only. Puts instrument values in the panel, keeping any
        edit not applied yet - unless overwrite is set, which is the case after
        an Apply, when the analyzer is the authority on what took effect."""
        kept = 0
        for key, raw in values.items():
            s = BY_KEY[key]
            value = fmt_setting(s, raw)
            was_edited = self.edited(key)
            self.set_inst[key] = value
            if overwrite or not was_edited:
                self.set_vars[key].set(value)
            elif self.set_vars[key].get().strip() != value:
                kept += 1
        if values:
            self.last_ylabel = trace_units(values)
            self.last_yscale = trace_yscale(values)
        if "ARNG" in values:
            self.arng_live.set(code_of(values, "ARNG", 0) == 1)
        self.read_stamp = datetime.datetime.now().strftime("%H:%M:%S")
        if kept:
            self.log(f"  (panel: kept {kept} unapplied edit(s), the analyzer "
                     f"reports something else)")
        self.refresh_marks()

    def read_settings(self, *keys):
        return self.an.read_settings(*keys)

    def read_all_settings(self, retry_all=False):
        return self.an.read_all_settings(retry_all=retry_all, log=self.log)

    def command(self, key, value):
        return self.an.command(key, value)

    def wait_out(self, seconds):
        """An abortable dwell, wired to the Stop button."""
        return self.an.dwell(seconds, stop=self.abort.is_set)

    def exp_wait(self):
        return self.float_of(self.exp_wait_s, DEFAULT_EXP_WAIT_S,
                             "exponential wait")

    def average_finishes(self):
        return self.an.average_finishes()

    def do_read_settings(self):
        if self.busy or not self.an.inst:
            return
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(None,),
                         daemon=True).start()

    def do_apply_settings(self):
        if self.busy or not self.an.inst:
            return
        changes = {}
        for key, var in self.set_vars.items():
            if not self.edited(key):
                continue
            try:
                changes[key] = parse_setting(BY_KEY[key], var.get())
            except ValueError:
                self.log(f"  {key}: '{var.get()}' is not a valid value, skipped")
        if not changes:
            self.log("No setting changes to apply.")
            return
        self.set_busy(True)
        threading.Thread(target=self._settings_worker, args=(changes,),
                         daemon=True).start()

    # Stage preset is gone from the panel. Two blocks to choose between, one
    # of them a reproduction of how the old data was taken, made every session
    # start with a decision about which discipline was in force - and the panel
    # already shows every setting and marks the ones that differ from the
    # analyzer, which is the same information without the choice. sr760.PRESETS
    # stays: the protocol runner applies it by name, where a script has no
    # panel to read.

    def _settings_worker(self, changes):
        try:
            if changes:
                for key, value in changes.items():
                    self.an.put(self.command(key, value))
                    self.log(f"  {self.command(key, value)}")
                if any(k in SETTLE_KEYS for k in changes):
                    # Span, start frequency, range or coupling: the filter chain
                    # has to flush before the next average means anything.
                    self.settle_due.set()
                if "ARNG" in changes and self.pinned_range is not None:
                    self.log("  (a range hold is armed - it will be re-asserted "
                             "before the next capture, overriding this)")
                self.report_status()
            # Read back either way: after a write the analyzer is the authority
            # on what it accepted, since it clamps values it dislikes.
            values = self.read_all_settings(retry_all=not changes)
            self.root.after(0,
                            lambda v=values, c=changes: self.after_settings(v, c))
        except Exception as exc:
            self.log(f"ERROR: {exc}")
        finally:
            self.root.after(0, lambda: self.set_busy(False))

    def report_status(self):
        """Log the error status byte if the analyzer set anything. Bit 7 is the
        input overload the ranging routine watches - it says nothing about the
        commands just sent, so it is called out separately rather than read as a
        rejection."""
        line = self.an.status_line()
        if line:
            self.log(f"  ({line})")

    def after_settings(self, values, changes):
        """Main thread. Show what the analyzer reported, then say plainly which
        writes did not stick and let the rest stand."""
        self.show_settings(values, overwrite=bool(changes))
        for key, code in sorted((changes or {}).items()):
            if key not in values:
                continue
            asked = fmt_setting(BY_KEY[key], code)
            got = fmt_setting(BY_KEY[key], values[key])
            if asked != got:
                # Either the analyzer clamped the value, or the command was not
                # in the shape it wanted - which it accepts without complaining.
                self.log(f"  ({BY_KEY[key].label}: asked for {asked}, the "
                         f"analyzer reports {got})")
        blind = sorted(k for k in (changes or {}) if k not in values)
        for key in blind:
            shown = fmt_setting(BY_KEY[key], changes[key])
            self.set_inst[key] = shown
            self.set_vars[key].set(shown)
        if blind:
            self.log("  (" + ", ".join(BY_KEY[k].label for k in blind)
                     + ": written but not readable, so the panel takes the "
                       "write at face value)")
            self.refresh_marks()

    # -- auto-grab --------------------------------------------------------

    def toggle_auto(self):
        if self.auto.get():
            self.schedule_auto()
        elif self.auto_job is not None:
            self.root.after_cancel(self.auto_job)
            self.auto_job = None

    def schedule_auto(self):
        try:
            ms = max(1000, int(float(self.interval.get()) * 1000))
        except ValueError:
            ms = 60000
        # Say when a scheduled grab could not run. do_grab returns without a
        # word if the panel is busy or the analyzer has gone, so an unattended
        # overnight run would otherwise come back to fewer files than expected
        # and nothing in the log to say which ones are missing or why.
        if self.busy:
            self.log("Auto-grab skipped: the previous run has not finished. "
                     "Lengthen the interval or shorten the sweep.")
        elif not self.an.inst:
            self.log("Auto-grab skipped: not connected.")
        else:
            self.do_grab()
        self.auto_job = self.root.after(ms, self.schedule_auto)

    def on_close(self):
        self.save_config()
        self.abort.set()
        self.auto.set(False)
        self.toggle_auto()
        self.an.close()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
