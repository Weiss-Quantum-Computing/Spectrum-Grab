"""
The E4405B panel's Power steps window: the power a source delivers at each of
a list of drive levels, measured in zero span, and the amplitude table for the
DDS pulse compiler that follows from it.

Opened from the panel's Acquisition box. It is a window of its own because
the panel is already as tall as a 1080-line screen, and because none of it
means anything outside zero span.

How a measurement goes:

1. **Set zero span** parks the analyser on the carrier: zero span, sample
   detector, the sweep time and resolution bandwidth typed here. The trigger
   is in the panel's settings grid (External, for a TTL at the start of the
   sequence), and so are the attenuation and the reference level.
2. The source plays its levels. Either all of them in one sequence - a
   staircase, `Steps per capture` long - or one a cycle, as a power scan does,
   with `Captures` set to the number of points: the analyser is armed again
   for each trigger and the steps are counted on across the captures.
3. **CAPTURE STEPS** arms, waits for each trigger (as long as the panel's
   measurement timeout), and reads the level of every step: the mean power of
   the middle 60 % of it. Step length blank means "find the steps": every
   burst that stands clear of the floor is one.
4. With the amplitude words that were played typed in, the level is set
   against 'power goes as the word squared' and, if ticked, written as the
   compiler's table file.

Files, under one base name `<title>_steps_<carrier>_<date>`: `.csv` the
levels, `_traces.csv` every capture as it came off the analyser, `.png`, `.txt`
and, when asked for, `_amplitude_table.txt`. Nothing is copied into the DDS
folder from here: putting a table in place changes every pulse's area and is
done by hand, when the pi powers are about to be measured again.

The arithmetic is powersteps.py, which also runs on saved traces by itself.
"""

import datetime
import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import numpy as np

import e4405b as E
import powersteps as P
import spectrum_grab as sg

# The Cs clock transition; the DDS chain's microwave band is within a few MHz.
DEFAULT_CARRIER = "9.19263177G"

FIELDS = (
    # key, default
    ("carrier", DEFAULT_CARRIER), ("rbw", "1M"), ("swt", "0.1"),
    ("points", "401"), ("first", "0"), ("length", ""), ("period", ""),
    ("count", "1"), ("captures", "1"), ("margin", "10"), ("words", ""),
    ("anchor_w", "1"), ("anchor_word", str(P.ANCHOR_WORD)),
)


def make_vars():
    """The window's boxes, made with the panel so they are in its config
    file whether or not the window is ever opened."""
    out = {key: tk.StringVar(value=default) for key, default in FIELDS}
    out["table"] = tk.BooleanVar(value=False)
    return out


def compact_hz(hz):
    return E.fmt_hz(hz).replace(" ", "")


class StepsDialog:
    def __init__(self, app):
        self.app = app
        self.v = app.steps
        self.win = win = tk.Toplevel(app.root)
        win.title("Power steps (zero span) - E4405B")
        win.protocol("WM_DELETE_WINDOW", self.close)
        pad = dict(padx=8, pady=4)

        def box(parent, rows):
            grid = ttk.Frame(parent)
            grid.pack(fill="x", padx=6, pady=4)
            for i, (label, key, width) in enumerate(rows):
                r, c = divmod(i, 2)
                ttk.Label(grid, text=label + ":").grid(
                    row=r, column=c * 2, sticky="e", padx=(0, 4), pady=1)
                ttk.Entry(grid, textvariable=self.v[key], width=width).grid(
                    row=r, column=c * 2 + 1, sticky="w", padx=(0, 12), pady=1)
            return grid

        f = ttk.LabelFrame(win, text="Analyser")
        f.pack(fill="x", **pad)
        box(f, (("Carrier (Hz)", "carrier", 16), ("Res BW (Hz)", "rbw", 10),
                ("Sweep time (s)", "swt", 10), ("Points", "points", 10)))
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(0, 6))
        self.setup_btn = ttk.Button(row, text="Set zero span",
                                    command=self.do_setup)
        self.setup_btn.pack(side="left")
        ttk.Label(row, text="trigger, attenuation and reference level are in "
                            "the panel's settings",
                  foreground="#666").pack(side="left", padx=8)

        f = ttk.LabelFrame(win, text="Steps  (step length blank = find them "
                                     "as bursts above the floor)")
        f.pack(fill="x", **pad)
        box(f, (("First step starts at (s)", "first", 10),
                ("Step length (s)", "length", 10),
                ("Step period (s)", "period", 10),
                ("Steps per capture", "count", 10),
                ("Captures (triggers)", "captures", 10),
                ("Burst threshold (dB)", "margin", 10)))

        f = ttk.LabelFrame(win, text="Amplitude words played, in order  "
                                     "(blank = levels only)")
        f.pack(fill="x", **pad)
        ttk.Entry(f, textvariable=self.v["words"], width=70).pack(
            fill="x", padx=6, pady=(6, 2))
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(2, 6))
        ttk.Checkbutton(row, text="write the amplitude table, with word",
                        variable=self.v["table"]).pack(side="left")
        ttk.Entry(row, textvariable=self.v["anchor_word"], width=6).pack(
            side="left", padx=4)
        ttk.Label(row, text="at").pack(side="left")
        ttk.Entry(row, textvariable=self.v["anchor_w"], width=6).pack(
            side="left", padx=4)
        ttk.Label(row, text="W").pack(side="left")

        row = ttk.Frame(win)
        row.pack(fill="x", **pad)
        self.capture_btn = ttk.Button(row, text="CAPTURE STEPS",
                                      command=self.do_capture)
        self.capture_btn.pack(side="left", fill="x", expand=True, ipady=6)
        self.saved_btn = ttk.Button(row, text="Analyse saved traces...",
                                    command=self.do_saved)
        self.saved_btn.pack(side="left", padx=(8, 0), ipady=6)
        ttk.Label(win, text="Stop, the log and the plot are in the main "
                            "window.", foreground="#666").pack(
            anchor="w", padx=10, pady=(0, 6))
        self.refresh()

    # -- window -----------------------------------------------------------

    def close(self):
        self.app.steps_dialog = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def lift(self):
        self.win.deiconify()
        self.win.lift()

    def refresh(self):
        """Buttons follow the panel's busy state. Main thread."""
        app = self.app
        live = "disabled" if app.busy or not app.an.inst else "normal"
        try:
            self.setup_btn.configure(state=live)
            self.capture_btn.configure(state=live)
            self.saved_btn.configure(state="disabled" if app.busy
                                     else "normal")
        except tk.TclError:
            pass

    # -- what was typed ---------------------------------------------------

    def number(self, key, name, blank=None):
        text = self.v[key].get().strip()
        if not text:
            if blank is None:
                raise ValueError(f"{name} is blank")
            return blank
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{name}: '{text}' is not a number") from None

    def params(self):
        """Everything the analysis needs, checked, or ValueError."""
        p = {"first": self.number("first", "first step", 0.0),
             "length": self.number("length", "step length", 0.0) or None,
             "margin": self.number("margin", "burst threshold", P.CLEAR_DB),
             "count": int(self.number("count", "steps per capture", 1.0)),
             "captures": int(self.number("captures", "captures", 1.0)),
             "words": P.parse_words(self.v["words"].get()) or None,
             "table": bool(self.v["table"].get())}
        p["period"] = self.number("period", "step period",
                                  p["length"] or 0.0) or None
        if p["count"] < 1 or p["captures"] < 1:
            raise ValueError("steps per capture and captures are at least 1")
        if p["captures"] > sg.MAX_SWEEP_RUNS:
            raise ValueError(f"{p['captures']} captures is past the "
                             f"{sg.MAX_SWEEP_RUNS} limit")
        if p["length"]:
            P.windows(p["first"], p["length"], p["period"], p["count"])
        if p["table"]:
            if p["words"] is None:
                raise ValueError("the amplitude table needs the words that "
                                 "were played")
            p["anchor_w"] = self.number("anchor_w", "anchor power")
            p["anchor_word"] = int(self.number("anchor_word", "anchor word"))
            if p["anchor_w"] <= 0:
                raise ValueError("the anchor power has to be above zero")
        return p

    # -- actions ----------------------------------------------------------

    def do_setup(self):
        app = self.app
        if app.busy or not app.an.inst:
            return
        try:
            changes = E.power_vs_time(
                E.parse_hz(self.v["carrier"].get()),
                self.number("swt", "sweep time"),
                E.parse_hz(self.v["rbw"].get()),
                int(self.number("points", "points")))
        except ValueError as exc:
            app.log(f"Power steps: {exc}")
            return
        app.log("Power steps: zero span at "
                f"{E.fmt_hz(float(changes['FCENT']))}")
        app.abort.clear()
        app.set_busy(True)
        threading.Thread(target=app._settings_worker, args=(changes,),
                         daemon=True).start()

    def do_capture(self):
        app = self.app
        if app.busy or not app.an.inst:
            return
        try:
            p = self.params()
        except ValueError as exc:
            app.log(f"Power steps: {exc}")
            return
        app.abort.clear()
        app.set_busy(True)
        threading.Thread(target=self._capture_worker, args=(p,),
                         daemon=True).start()

    def do_saved(self):
        app = self.app
        if app.busy:
            return
        try:
            p = self.params()
        except ValueError as exc:
            app.log(f"Power steps: {exc}")
            return
        path = filedialog.askopenfilename(
            parent=self.win, title="Zero-span traces",
            initialdir=app.target_dir(),
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            t, traces = P.read_traces(path)
            base = E.unique_base(
                os.path.dirname(path),
                os.path.splitext(os.path.basename(path))[0] + "_steps",
                (".csv", ".png", ".txt", "_amplitude_table.txt"))
            app.log(f"Power steps from {os.path.basename(path)}: "
                    f"{len(traces)} capture(s)")
            self.finish(base, t, traces, p, None, [],
                        source=os.path.basename(path))
        except Exception as exc:
            app.log(f"ERROR: {exc}")

    def _capture_worker(self, p):
        app = self.app
        t, traces, snap, all_notes, ended = None, [], None, [], ""
        total = p["captures"]
        try:
            outdir = app.target_dir()
            os.makedirs(outdir, exist_ok=True)
            app.log(f"Power steps: {total} capture(s)"
                    + (f", {p['count']} step(s) each" if p["length"] else
                       ", steps found as bursts"))
            for i in range(total):
                if app.abort.is_set():
                    raise KeyboardInterrupt
                if total > 1:
                    app.log(f"--- capture {i + 1}/{total}")
                x, y, snap, notes = app.run_one()
                if notes.get("measurement") != "done":
                    # A sweep that never ran is not a capture: with an
                    # external trigger this is 'no trigger came'.
                    app.an.abort()
                    raise RuntimeError(
                        f"the sweep did not finish ({notes.get('measurement')})"
                        + (f" - no {E.waits_for_trigger(snap).lower()} trigger "
                           f"arrived within the timeout"
                           if E.waits_for_trigger(snap) else ""))
                if not E.zero_span(snap):
                    raise RuntimeError("the analyser is not in zero span - "
                                       "press Set zero span first")
                if E.trace_units(snap) != "dBm":
                    raise RuntimeError(f"the trace is in "
                                       f"{E.trace_units(snap)}; power steps "
                                       f"are read in dBm (Y units)")
                if t is not None and len(x) != len(t):
                    raise RuntimeError("the number of points changed during "
                                       "the series")
                t = x
                traces.append(y)
                all_notes.append(notes)
        except KeyboardInterrupt:
            ended = "stopped"
        except Exception as exc:
            ended = "failed"
            app.log(f"ERROR: {exc}")
            app.an.recover()

        try:
            if app.resume_cont.get() and app.an.resume():
                app.log("  continuous sweep back on")
            app.release_panel()
        except Exception as exc:
            app.log(f"  (could not put the analyser back: {exc})")

        try:
            if ended:
                app.log(f"{ended.capitalize()} after {len(traces)} of {total} "
                        f"capture(s).")
            if traces:
                stamp = datetime.datetime.now().strftime("%Y%m%d")
                name = "_".join([app.safe_title(), "steps",
                                 compact_hz(E.value_of(snap, "FCENT", 0.0)),
                                 stamp])
                base = E.unique_base(outdir, name, (
                    ".csv", ".png", ".txt", "_traces.csv",
                    "_amplitude_table.txt"))
                P.write_traces(base + "_traces.csv", t, traces)
                app.log(f"  {os.path.basename(base)}_traces.csv")
                self.finish(base, t, traces, p, snap, all_notes, ended=ended)
        except Exception as exc:
            app.log(f"ERROR: the power steps could not be worked out: {exc}")
        finally:
            app.root.after(0, app.save_config)
            app.root.after(0, lambda: app.set_busy(False))

    # -- analysis and files -----------------------------------------------

    def finish(self, base, t, traces, p, snap, all_notes, source="",
               ended=""):
        """Levels, files and the plot from captured traces. `snap` is None
        for traces read off disk, which carry no analyser settings."""
        app = self.app
        steps, captures, floor = P.analyse(t, traces, p["first"], p["length"],
                                           p["period"], p["count"],
                                           p["margin"])
        words = p["words"]
        if words is not None and len(words) != len(steps):
            app.log(f"  ({len(words)} words typed for {len(steps)} steps - "
                    f"the words are left out, and there is no table)")
            words = None
        app.log(f"  {len(steps)} step(s)"
                + (f", floor {floor:.2f} dBm" if np.isfinite(floor) else ""))
        lines = P.summary_lines(steps, captures, words, floor)
        for line in lines:
            app.log("  " + line)
        stem = os.path.basename(base)

        P.levels_csv(base + ".csv", steps, words, floor, captures)
        app.log(f"  {stem}.csv")

        suspect = sorted({n.get("trace quality", "") for n in all_notes
                          if n.get("trace quality", "").startswith("SUSPECT")})
        table_note = ""
        if p["table"] and words is not None:
            try:
                points = P.amplitude_table(words, [s.dbm for s in steps],
                                           p["anchor_w"], p["anchor_word"])
                rel, _law, usable = P.response(words, [s.dbm for s in steps],
                                               floor)
                near = [str(w) for w, u in zip(words, usable) if not u]
                comments = [
                    "",
                    f"MEASURED {datetime.datetime.now().isoformat(timespec='seconds')}"
                    f" with {getattr(app.an, 'idn', '') or 'saved traces'}.",
                    f"Source: {source or stem + '_traces.csv'}"
                    + (f", zero span at "
                       f"{E.fmt_hz(E.value_of(snap, 'FCENT', 0.0))}, RBW "
                       f"{E.fmt_hz(E.value_of(snap, 'RBW', 0.0))}, "
                       f"{E.value_of(snap, 'ATT', float('nan')):g} dB "
                       f"attenuation." if snap else "."),
                    f"Relative powers as measured, scaled by one factor so "
                    f"that word {p['anchor_word']} is {p['anchor_w']:g} W.",
                    f"Typed powers above the last point "
                    f"({points[-1][0]:.6g} W) are refused by the compiler.",
                    "A table changes the area of every pulse: measure the pi "
                    "powers again when it goes in.",
                ]
                if near:
                    comments.append(
                        f"Within {P.CLEAR_DB:g} dB of the analyser's floor, "
                        f"so read high: word(s) {', '.join(near)}.")
                if suspect or ended:
                    comments.append("NOT A CLEAN MEASUREMENT: "
                                    + "; ".join(suspect + ([f"series {ended}"]
                                                           if ended else [])))
                comments.append("")
                with open(base + "_amplitude_table.txt", "w",
                          encoding="utf-8", newline="\n") as fh:
                    fh.write(P.table_text(points, comments))
                table_note = f"{stem}_amplitude_table.txt"
                app.log(f"  {table_note}  ({len(points)} points, top "
                        f"{points[-1][0]:.4g} W)")
            except ValueError as exc:
                table_note = f"not written: {exc}"
                app.log(f"  No amplitude table: {exc}")

        extra = {
            "measurement": "power steps, zero span",
            "captures": str(len(traces)),
            "steps": str(len(steps)),
            "steps found by": (f"timing: first {p['first']:g} s, length "
                               f"{p['length']:g} s, period {p['period']:g} s, "
                               f"{p['count']} a capture" if p["length"] else
                               f"bursts more than {p['margin']:g} dB above "
                               f"the floor"),
            "averaged": f"mean power over the middle {P.KEEP:.0%} of a step",
            "floor (dBm)": f"{floor:.2f}" if np.isfinite(floor) else "not seen",
        }
        if source:
            extra["traces from"] = source
        if words is not None:
            extra["words"] = ", ".join(str(w) for w in words)
        if table_note:
            extra["amplitude table"] = table_note
        if ended:
            extra["series"] = ended
        if all_notes:
            last = all_notes[-1]
            for key in ("status", "alignment", "resolution"):
                if last.get(key):
                    extra[key] = last[key]
            extra["trace quality"] = ("SUSPECT: " + " | ".join(suspect)
                                      if suspect else "clean")
        with open(base + ".txt", "w", encoding="utf-8") as fh:
            if snap is not None:
                fh.write(E.metadata_text(app.an, snap, extra, app.command))
            else:
                fh.write("".join(f"{k:<21}: {v}\n" for k, v in extra.items()))
            fh.write("\nsteps\n" + "".join(f"  {line}\n" for line in lines))
        app.log(f"  {stem}.txt")

        if sg.Figure is None:
            app.log("  (no matplotlib: skipping the plot)")
            return steps, floor
        bits = []
        if snap is not None:
            bits.append(f"zero span at {E.fmt_hz(E.value_of(snap, 'FCENT'))}")
            bits += E.plot_bits(snap)
        if suspect:
            bits.append("SUSPECT, see the .txt")
        bits.append(datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        title = app.plot_title(note="power steps")
        try:
            with sg.matplotlib.rc_context(sg.PNG_RC):
                fig = sg.Figure(figsize=(8, 9))
                sg.FigureCanvasAgg(fig)
                top, _low = P.draw(
                    fig, t, traces, steps, captures, words, floor,
                    finish=sg.labplot.finish if sg.labplot else None)
                top.set_title(title + "\n" + ", ".join(bits),
                              fontsize=sg.PNG_FONTS["notes"] + 1)
                fig.tight_layout()
                fig.savefig(base + ".png", format="png", dpi=sg.PLOT_DPI)
        except Exception as exc:
            app.log(f"  (plot failed: {exc})")
            return steps, floor
        app.log(f"  {stem}.png")
        # The panel shows the traces themselves; the levels are in the file.
        shown = sg.TimeTraces((t, y, f"capture {i + 1}")
                              for i, y in enumerate(traces[:8]))
        app.remember_plot(shown, title, sg.SUBTITLE_SEP.join(bits), "dBm",
                          "linear", ())
        app.show_saved(base + ".png")
        return steps, floor
