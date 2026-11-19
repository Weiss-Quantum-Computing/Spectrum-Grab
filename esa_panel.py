"""
Spectrum Grab's panel for the Agilent / HP E4405B spectrum analyser.

Not run by itself: spectrum_grab.py builds it when "E4405B" is picked in the
Analyzer box (and at startup, if that is what was used last). It is the SR760
panel, App, with everything that is the SR760's replaced - and what is left
over is what the two instruments genuinely share: the output folder and file
naming, GRAB / Peek / Stop, auto-grab, the plot panel, the comparison against
saved sequences, the log and the settings grid's edit-and-apply bookkeeping.

What differs, and why it is a different class rather than a mode:

* A sweep here is cases x **segments**, each segment a start-stop band. That
  is the analogue of the SR760's start-frequency stitch: a band cut into n
  segments is swept with n times the points.
* There is no range hold, no overlap and no settle in record lengths. The ESA
  does not move its own range; what it has instead is Meas Uncal, IF overload
  and Align Needed, which run_one reads after every sweep.
* A capture takes the analyser to single sweep, triggers one measurement (a
  whole average when averaging is on), reads the trace in ASCII and then, by
  default, puts continuous sweep back and hands the front panel back.

The instrument itself is e4405b.py, which is also the scripting library.
"""

import datetime
import json
import os
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np

import e4405b as E
import spectrum_grab as sg
from spectrum_grab import App, Figure, MAX_SWEEP_RUNS, STATUS_CHARS, \
    SUBTITLE_SEP, elide


def compact_hz(hz):
    """A frequency for a file name: 13.2GHz, 100kHz."""
    return E.fmt_hz(hz).replace(" ", "")


class EsaApp(App):
    INSTRUMENT = "E4405B"
    WINDOW_TITLE = "Spectrum Grab - E4405B"
    DEFAULT_TITLE = "e4405b"
    DEFAULT_ADDR = E.DEFAULT_ADDRESS
    drv = E
    last_ylabel = "dBm"

    def make_analyzer(self):
        return E.E4405B(connect=False, log=self.log)

    def config_path(self):
        """Its own file beside the SR760's, so neither panel's saved boxes
        land in the other's - they share a folder box and little else."""
        return os.path.join(os.path.dirname(sg.CONFIG_PATH),
                            "config_e4405b.json")

    def __init__(self, root):
        super().__init__(root)
        # The SR760's [START] restarts an average; here the same button takes
        # one sweep, or one whole average, and leaves it on the screen.
        self.avg_btn.configure(text="Single sweep")

    # -- layout -----------------------------------------------------------

    def build_sweep(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Sweep  (blank = one grab at the "
                                        "current settings)")
        f.pack(fill="x", **pad)
        grid = ttk.Frame(f)
        grid.pack(fill="x", padx=6, pady=(6, 2))
        grid.columnconfigure(1, weight=1)
        ttk.Label(grid, text="Segments:").grid(row=0, column=0, sticky="e",
                                               padx=(0, 4), pady=1)
        self.segs_txt = tk.StringVar(value="")
        ttk.Entry(grid, textvariable=self.segs_txt).grid(row=0, column=1,
                                                         sticky="ew", pady=1)
        ttk.Label(grid, text="Cases:").grid(row=1, column=0, sticky="e",
                                            padx=(0, 4), pady=1)
        self.cases_txt = tk.StringVar(value="")
        ttk.Entry(grid, textvariable=self.cases_txt).grid(row=1, column=1,
                                                          sticky="ew", pady=1)
        ttk.Label(f, text="segments are start-stop, comma separated:  "
                          "9k-100M, 100M-1G, 1G-3G",
                  foreground="#666").pack(anchor="w", padx=8)

        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=2)
        ttk.Label(row, text="Tile").pack(side="left")
        self.tile_start = tk.StringVar(value="0")
        ttk.Entry(row, textvariable=self.tile_start, width=8).pack(
            side="left", padx=4)
        ttk.Label(row, text="to").pack(side="left")
        self.tile_stop = tk.StringVar(value="3G")
        ttk.Entry(row, textvariable=self.tile_stop, width=8).pack(
            side="left", padx=4)
        ttk.Label(row, text="in").pack(side="left")
        self.tile_n = tk.StringVar(value="10")
        ttk.Entry(row, textvariable=self.tile_n, width=5).pack(side="left",
                                                               padx=4)
        ttk.Label(row, text="segments").pack(side="left")
        ttk.Button(row, text="Fill segments",
                   command=self.do_tile).pack(side="left", padx=8)

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
        self.keep_segments = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="keep per-segment files",
                        variable=self.keep_segments).pack(side="left", padx=10)

    def build_options(self, parent, pad):
        f = ttk.LabelFrame(parent, text="Acquisition")
        f.pack(fill="x", **pad)
        # A capture needs single sweep, so that "finished" means something.
        # Left there, the screen freezes on the captured trace, which is what
        # you want when comparing it with the file and not when tuning
        # something up - so going back is the default, and a switch.
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=(6, 2))
        self.resume_cont = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="go back to continuous sweep after a capture",
                        variable=self.resume_cont).pack(side="left")
        # Any command puts the analyser in remote, where every key but Local
        # is dead. Handing it back after each job is the opposite of the
        # SR760's "lock front panel": this instrument locks by default.
        row = ttk.Frame(f)
        row.pack(fill="x", padx=6, pady=2)
        self.local = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="hand the front panel back (Local) when "
                                  "idle",
                        variable=self.local).pack(side="left")

        grid = ttk.Frame(f)
        grid.pack(fill="x", padx=6, pady=(2, 6))
        for i, (label, attr, default) in enumerate(
                (("settle before start (s)", "settle_s", "0"),
                 ("measurement timeout (s)", "timeout_s", "600"),
                 ("plot y min (blank = auto)", "ymin", ""),
                 ("plot y max (blank = auto)", "ymax", ""))):
            r, c = divmod(i, 2)
            ttk.Label(grid, text=label + ":").grid(row=r, column=c * 2,
                                                   sticky="e", padx=(0, 4),
                                                   pady=1)
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            ttk.Entry(grid, textvariable=var, width=8).grid(
                row=r, column=c * 2 + 1, sticky="w", padx=(0, 12), pady=1)

    def build_hold(self, parent, pad):
        """Nothing to build: the ESA has no auto range to pin. Attenuation
        stays where it is put unless Atten auto is on, and that is a box in
        the settings grid like any other."""

    def build_settings(self, parent, pad):
        self.set_vars = {}
        self.set_marks = {}
        self.set_inst = {}
        self.read_stamp = ""
        for group, settings in E.SETTING_GROUPS:
            frame = ttk.LabelFrame(parent, text=group)
            frame.pack(fill="x", padx=8, pady=2)
            grid = ttk.Frame(frame)
            grid.pack(fill="x", padx=6, pady=4)
            for i, s in enumerate(settings):
                row, col = divmod(i, 2)
                ttk.Label(grid, text=s.label + ":").grid(
                    row=row, column=col * 2, sticky="e", padx=(0, 4), pady=1)
                self.setting_widget(grid, s, row, col * 2 + 1)

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=8, pady=(2, 6))
        self.read_btn = ttk.Button(bar, text="Read",
                                   command=self.do_read_settings,
                                   state="disabled")
        self.read_btn.pack(side="left")
        self.apply_btn = ttk.Button(bar, text="Apply changes",
                                    command=self.do_apply_settings,
                                    state="disabled")
        self.apply_btn.pack(side="left", padx=4)
        self.align_btn = ttk.Button(bar, text="Align now",
                                    command=self.do_align, state="disabled")
        self.align_btn.pack(side="left", padx=4)
        self.set_status = ttk.Label(parent, text="not read yet",
                                    foreground="#666")
        self.set_status.pack(anchor="w", padx=10)
        # Whatever the analyser says about itself that is not a setting:
        # alignment, and anything the last sweep was flagged for.
        self.health = ttk.Label(parent, text="", foreground="#666",
                                wraplength=420, justify="left")
        self.health.pack(anchor="w", padx=10, pady=(0, 4))

    # -- helpers ----------------------------------------------------------

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy or not self.an.inst else "normal"
        for btn in (self.grab_btn, self.sweep_btn, self.avg_btn, self.peek_btn,
                    self.read_btn, self.apply_btn, self.align_btn):
            btn.configure(state=state)
        self.refresh_compare()
        self.connect_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def refresh_hold(self):
        pass

    def refresh_averaging(self):
        pass

    def release_panel(self):
        """Give the front panel back, if that is ticked. Any thread."""
        if self.local.get() and self.an.inst:
            self.an.go_local()

    def show_health(self, status):
        """Any thread. One line under the settings about the analyser's own
        state, so Align Needed is on the panel and not only in a .txt."""
        if status is None or not status.read:
            text, colour = "", "#666"
        else:
            bits = status.faults()
            if status.alignment():
                bits.append("Align Needed - amplitudes not to specification")
            text = "; ".join(bits) if bits else "analyser status clean"
            colour = "#c60" if bits else "#060"
        self.root.after(0, lambda: self.health.configure(text=text,
                                                         foreground=colour))

    def config_vars(self):
        return {
            "addr": self.addr, "outdir": self.outdir, "title": self.title,
            "dated": self.dated, "save_csv": self.save_csv,
            "save_png": self.save_png, "save_txt": self.save_txt,
            "segments": self.segs_txt, "cases": self.cases_txt,
            "tile_start": self.tile_start, "tile_stop": self.tile_stop,
            "tile_n": self.tile_n, "save_npy": self.save_npy,
            "combined": self.combined, "pause_cases": self.pause_cases,
            "keep_segments": self.keep_segments,
            "resume_cont": self.resume_cont, "local": self.local,
            "settle_s": self.settle_s, "timeout_s": self.timeout_s,
            "ymin": self.ymin, "ymax": self.ymax, "interval": self.interval,
        }

    def y_window(self, ylabel):
        """The y limits typed in the panel, or (None, None) to autoscale -
        which is what blank boxes mean, and what a linear-unit trace always
        gets. No default window: a dBm trace can sit anywhere from +30 to
        -150 depending on the attenuator and the bandwidth."""
        if not ylabel.startswith("dB"):
            return None, None
        try:
            return float(self.ymin.get()), float(self.ymax.get())
        except ValueError:
            return None, None

    # -- actions ----------------------------------------------------------

    def do_connect(self):
        if self.closed:
            return
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
                self.log(f"Address:   {self.an.addr}"
                         + (f"   options {self.an.options}"
                            if self.an.options else ""))
                self.root.after(0, lambda: self.addr.set(self.an.addr))
                values = self.read_all_settings(retry_all=True)
                self.root.after(0, lambda v=values: self.show_settings(v))
                status = self.an.refresh_status()
                self.show_health(status)
                if status.alignment():
                    self.log(f"  *** {status.alignment()} ***")
                self.release_panel()
            except Exception as exc:
                self.root.after(0, lambda: self.status.configure(
                    text="Not connected", foreground="#a00"))
                self.log(f"ERROR: {exc}")
            finally:
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def do_tile(self):
        """Fill the segments box with equal bands covering start to stop."""
        try:
            start = E.parse_hz(self.tile_start.get())
            stop = E.parse_hz(self.tile_stop.get())
            segments = E.tile_segments(start, stop,
                                       int(float(self.tile_n.get())))
        except ValueError as exc:
            self.log(f"Tile: {exc}")
            return
        self.segs_txt.set(", ".join(f"{a:.12g}-{b:.12g}" for a, b in segments))
        width = segments[0][1] - segments[0][0]
        line = (f"Tile: {len(segments)} segments of {E.fmt_hz(width)} from "
                f"{E.fmt_hz(start)} to {E.fmt_hz(stop)}")
        try:
            points = float(self.set_inst.get("POINTS", ""))
            line += (f" - at {points:g} points a segment that is "
                     f"{E.fmt_hz(width / (points - 1))} a point")
        except (ValueError, ZeroDivisionError):
            pass
        self.log(line)

    def do_align(self):
        if self.busy or not self.an.inst:
            return
        if not messagebox.askokcancel(
                "Spectrum Grab",
                "Run a full alignment now?\n\nIt takes a few minutes, during "
                "which the analyser does not sweep, and it should not have a "
                "strong signal near 50 MHz on its input."):
            return
        self.set_busy(True)

        def work():
            try:
                self.log("Alignment running - this takes a few minutes...")
                t0 = time.perf_counter()
                passed = self.an.align()
                self.log(f"  alignment {'passed' if passed else 'FAILED'} "
                         f"after {time.perf_counter() - t0:.0f} s")
                self.show_health(self.an.refresh_status(log=self.log))
                values = self.read_all_settings()
                self.root.after(0, lambda v=values: self.show_settings(v))
            except Exception as exc:
                self.log(f"ERROR: {exc}")
                self.an.recover()
            finally:
                self.release_panel()
                self.root.after(0, lambda: self.set_busy(False))
        threading.Thread(target=work, daemon=True).start()

    def _settings_worker(self, changes):
        try:
            if changes:
                self.an.write_settings(changes, log=self.log)
            # Read back either way: the analyser clamps what it dislikes and
            # moves whatever is coupled to what was written - a new span
            # changes RBW, VBW and sweep time while their autos are on.
            values = self.read_all_settings(retry_all=not changes)
            self.root.after(0,
                            lambda v=values, c=changes: self.after_settings(v, c))
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.an.recover()
        finally:
            self.release_panel()
            self.root.after(0, lambda: self.set_busy(False))

    def _average_worker(self):
        """The Single sweep button: one measurement, left on the screen,
        nothing saved. The analyser stays in single sweep afterwards - that
        is the point - until a capture or the Sweep box puts it back."""
        try:
            snap = self.read_settings("SWT", "AVG", "NAVG")
            n, est = E.sweep_count(snap), E.capture_time(snap)
            self.log(f"Single sweep - {n} sweep(s)"
                     + (f", about {E.fmt_hms(est)}" if np.isfinite(est) else "")
                     + ". Nothing will be saved.")
            t0 = time.perf_counter()
            self.an.start(log=self.log)
            state = self.an.wait_done(
                self.float_of(self.timeout_s, 600.0, "timeout"),
                stop=self.abort.is_set)
            elapsed = time.perf_counter() - t0
            if state == "stopped":
                self.an.abort()
            self.log(f"  {'finished' if state == 'done' else state} after "
                     f"{elapsed:.1f} s")
            self.show_health(self.an.refresh_status(log=self.log))
            values = self.read_all_settings()
            self.root.after(0, lambda v=values: self.show_settings(v))
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.an.recover()
        finally:
            self.release_panel()
            self.root.after(0, lambda: self.set_busy(False))

    def _peek_worker(self):
        try:
            snap = self.read_all_settings()
            self.root.after(0, lambda v=snap: self.show_settings(v))
            ylabel = E.trace_units(snap)
            for fault in (E.readout_fault(snap), E.sweep_fault(snap)):
                if fault:
                    self.log(f"  *** SUSPECT: {fault} ***")
            t0 = time.perf_counter()
            freqs, amps = self.an.trace(snap)
            i = int(np.argmax(amps))
            self.log(f"Peek: {len(freqs)} points in "
                     f"{time.perf_counter() - t0:.2f} s, peak {amps[i]:.4g} "
                     f"{ylabel} at {E.fmt_hz(freqs[i])} - nothing saved")
            self.show_health(self.an.refresh_status())
            if Figure is None:
                self.log("  (no matplotlib: there is nothing to draw it with)")
                return
            self.remember_plot([(freqs, amps, "peek")],
                               self.plot_title(note="peek"),
                               self.plot_subtitle(snap, freqs), ylabel,
                               E.trace_yscale(snap), self.refs_for(ylabel))
            self.show_peek()
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.an.recover()
        finally:
            self.release_panel()
            self.root.after(0, lambda: self.set_busy(False))

    def sweep_lists(self):
        """The sweep boxes as (cases, segments), or None if they will not
        parse."""
        try:
            segments = E.parse_segments(self.segs_txt.get())
        except ValueError as exc:
            self.log(f"Sweep list: {exc}")
            return None
        cases = [c.strip() for c in self.cases_txt.get().split(",")
                 if c.strip()]
        return cases, segments

    def do_sweep(self):
        """Every case, over every segment."""
        if self.busy or not self.an.inst:
            return
        lists = self.sweep_lists()
        if lists is None:
            return
        cases, segments = lists
        if not (cases or segments):
            self.log("Nothing to sweep: fill in segments or cases first. "
                     "GRAB one takes a single capture at the current "
                     "settings.")
            return
        self.abort.clear()
        self.set_busy(True)
        threading.Thread(target=self._grab_worker,
                         args=(cases or [""], segments or [None], None),
                         daemon=True).start()

    # -- acquisition ------------------------------------------------------

    def _grab_runs(self, cases, segments, _spans=None):
        """`segments` is a list of (start, stop) bands, or [None] for 'where
        the analyser is'. The third argument is the SR760 panel's span list,
        which GRAB one passes and this instrument has no use for."""
        outdir = self.target_dir()
        stamp = datetime.datetime.now().strftime("%Y%m%d")
        total = len(cases) * len(segments)
        if total > MAX_SWEEP_RUNS:
            self.log(f"That is {total} runs ({len(cases)} case(s) x "
                     f"{len(segments)} segment(s)), past the {MAX_SWEEP_RUNS} "
                     f"limit. Trim the sweep lists.")
            return
        done, records, ended = [], [], ""
        keep = total == 1 or self.keep_segments.get()
        sweep_ylabel, sweep_yscale = self.last_ylabel, self.last_yscale
        building = total > 1 and Figure is not None
        self.last_progress = 0.0
        moves = any(s is not None for s in segments)
        home = {}
        t_sweep = time.perf_counter()

        try:
            os.makedirs(outdir, exist_ok=True)
            if total > 1:
                self.log(f"Sweep: {len(cases)} case(s) x {len(segments)} "
                         f"segment(s) = {total} runs")
            if moves:
                # Where the analyser was, to put it back afterwards: a sweep
                # that ends on its last segment leaves the screen on a band
                # nobody chose.
                home = self.read_settings("FSTART", "FSTOP")
            for ic, case in enumerate(cases):
                if case and self.pause_cases.get():
                    if not self.confirm(f"Set up case '{case}', then "
                                        f"continue."):
                        raise KeyboardInterrupt
                for iseg, seg in enumerate(segments):
                    if seg is not None:
                        self.an.set_band(*seg, log=self.log)
                    if self.abort.is_set():
                        raise KeyboardInterrupt
                    label = " ".join(
                        p for p in (case,
                                    f"{E.fmt_hz(seg[0])} to {E.fmt_hz(seg[1])}"
                                    if seg is not None else "") if p)
                    if label:
                        self.log(f"--- {label}"
                                 + (f"   (run {len(done) + 1}/{total})"
                                    if total > 1 else ""))
                    freqs, amps, snap, notes = self.run_one(band=seg)
                    sweep_ylabel = E.trace_units(snap)
                    sweep_yscale = E.trace_yscale(snap)
                    done.append((freqs, amps, label or self.safe_title()))
                    records.append({"case": case, "span": None, "ic": ic,
                                    "iseg": iseg, "freqs": freqs,
                                    "amps": amps, "snap": snap,
                                    "notes": notes, "units": sweep_ylabel})
                    if keep:
                        self.save_run(outdir, stamp, case, freqs, amps, snap,
                                      notes, show=not building)
                    if building:
                        self.show_sweep_so_far(done, total, sweep_ylabel,
                                               sweep_yscale)
                    self.root.after(0, lambda v=snap: self.show_settings(v))
        except KeyboardInterrupt:
            ended = "stopped"
        except Exception as exc:
            ended = "failed"
            self.log(f"ERROR: {exc}")
            self.an.recover()

        # Leave the analyser as it was found, whatever ended the job.
        try:
            if moves and "FSTART" in home and "FSTOP" in home:
                self.an.set_band(float(home["FSTART"]), float(home["FSTOP"]))
                self.log(f"  analyser back on "
                         f"{E.fmt_hz(float(home['FSTART']))} to "
                         f"{E.fmt_hz(float(home['FSTOP']))}")
            if self.resume_cont.get() and self.an.resume():
                self.log("  continuous sweep back on")
            if moves:
                values = self.read_all_settings()
                self.root.after(0, lambda v=values: self.show_settings(v))
            self.release_panel()
        except Exception as exc:
            self.log(f"  (could not put the analyser back: {exc})")

        if ended:
            self.log(f"{ended.capitalize()} after {len(done)} of {total} "
                     f"run(s).")
        elif total > 1:
            self.log(f"Sweep finished in "
                     f"{E.fmt_hms(time.perf_counter() - t_sweep)}.")
        try:
            if done and total > 1:
                self.save_sweep(outdir, stamp, cases, segments, done, total,
                                ended, sweep_ylabel, sweep_yscale, records)
        except Exception as exc:
            self.log(f"ERROR: the sweep files could not be written: {exc}")

    def run_one(self, band=None, start=None):
        """One measurement: trigger, wait, check, read out. Returns the trace,
        the settings snapshot it was taken under and the metadata notes.

        `band` is the (start, stop) the sweep wrote, if it wrote one; a trace
        that came back on some other band is marked SUSPECT."""
        notes = {}
        settle = self.float_of(self.settle_s, 0.0, "settle")
        if settle > 0:
            if self.an.dwell(settle, stop=self.abort.is_set) == "stopped":
                raise KeyboardInterrupt
            notes["settle (s)"] = f"{settle:g}"

        before = self.read_settings("SWT", "AVG", "NAVG")
        n, est = E.sweep_count(before), E.capture_time(before)
        timeout = self.float_of(self.timeout_s, 600.0, "timeout")
        if np.isfinite(est) and est > timeout:
            self.log(f"  (this should take about {E.fmt_hms(est)}, longer "
                     f"than the {timeout:g} s timeout - raise the timeout or "
                     f"the trace is read part way through)")
        self.log("  measuring..."
                 + (f" {n} sweeps, about {E.fmt_hms(est)}"
                    if n > 1 and np.isfinite(est) else ""))
        t0 = time.perf_counter()
        self.an.start(log=self.log)
        state = self.an.wait_done(timeout, stop=self.abort.is_set)
        measured = time.perf_counter() - t0
        if state == "stopped":
            self.an.abort()
            raise KeyboardInterrupt
        if state == "done":
            self.log(f"  measured in {measured:.1f} s")
        else:
            self.log(f"  (measurement {state} after {measured:.1f} s - "
                     f"reading the trace as it stands)")
        notes["measure time (s)"] = f"{measured:.3f}"
        notes["measurement"] = state
        notes["sweeps"] = str(n)

        # Condition registers, read while the finished sweep is still the one
        # they describe - before anything restarts the analyser.
        status = self.an.refresh_status()
        self.show_health(status)
        notes["status"] = status.describe()
        snap = self.read_all_settings()

        faults = list(status.faults())
        if state != "done":
            faults.append(f"measurement {state}: the trace was read before "
                          f"the sweep finished")
        for fault in (E.readout_fault(snap), E.sweep_fault(snap)):
            if fault:
                faults.append(fault)

        t0 = time.perf_counter()
        freqs, amps = self.an.trace(snap)
        transferred = time.perf_counter() - t0
        for fault in (E.flat_fault(amps), E.band_fault(freqs, band)):
            if fault:
                faults.append(fault)

        notes["trace quality"] = ("SUSPECT: " + "; ".join(faults) if faults
                                  else "clean")
        if faults:
            self.log(f"  *** {notes['trace quality']} ***")
        if status.alignment():
            # A caution on every amplitude rather than a fault in this trace,
            # so it has its own line and is said once a session in the log.
            notes["alignment"] = status.alignment()
            self.log_once(f"  ({status.alignment()})")
        resolution = E.resolution_note(snap)
        if resolution:
            notes["resolution"] = resolution
        if E.zero_span(snap):
            notes["x axis"] = ("zero span: the first column is time in "
                               "seconds, not frequency")
        notes["transfer"] = "ASCII (TRAC? TRACE1)"
        notes["transfer time (s)"] = f"{transferred:.3f}"
        self.log(f"  {len(freqs)} points in {transferred:.2f} s")
        return freqs, amps, snap, notes

    # -- files ------------------------------------------------------------

    def save_run(self, outdir, stamp, case, freqs, amps, snap, notes,
                 show=True):
        """CSV, plot and metadata for one measurement, under one shared base
        name: <title>[_case]_<start>-<stop>_<date>."""
        ylabel = E.trace_units(snap)
        parts = [self.safe_title()]
        if case:
            parts.append(E.safe_name(case))
        if E.zero_span(snap):
            parts.append(f"zerospan_{compact_hz(E.value_of(snap, 'FCENT'))}")
        else:
            parts.append(f"{compact_hz(float(freqs[0]))}-"
                         f"{compact_hz(float(freqs[-1]))}")
        parts.append(stamp)
        if not (self.save_csv.get() or self.save_png.get()
                or self.save_txt.get()):
            self.log("  (nothing ticked to save)")
            return
        base = E.unique_base(outdir, "_".join(parts), (".csv", ".png", ".txt"))
        stem = os.path.basename(base)

        if self.save_csv.get():
            E.write_csv(base + ".csv", freqs, amps, ylabel)
            self.log(f"  {stem}.csv")
        if self.save_txt.get():
            extra = {
                "start frequency (Hz)": f"{E.value_of(snap, 'FSTART', float('nan')):.12g}",
                "stop frequency (Hz)": f"{E.value_of(snap, 'FSTOP', float('nan')):.12g}",
                "points": str(len(freqs)),
                "trace units": ylabel,
            }
            extra.update(notes)
            with open(base + ".txt", "w", encoding="utf-8") as fh:
                fh.write(E.metadata_text(self.an, snap, extra, self.command))
        if self.save_png.get():
            self.write_plot(base + ".png", [(freqs, amps, stem)],
                            self.plot_title(case),
                            self.plot_subtitle(snap, freqs, notes), ylabel,
                            E.trace_yscale(snap), show=show, case=case)

    @staticmethod
    def plot_subtitle(snap, freqs, notes=None):
        """The line under the title: the settings that decide what the trace
        means, then anything that makes it untrustworthy."""
        notes = notes or {}
        bits = E.plot_bits(snap, () if E.zero_span(snap) else freqs)
        if E.zero_span(snap):
            bits.insert(0, f"zero span at "
                           f"{E.fmt_hz(E.value_of(snap, 'FCENT'))}, x is "
                           f"seconds")
        quality = notes.get("trace quality", "")
        if quality.startswith("SUSPECT"):
            bits.append(quality)
        if notes.get("alignment"):
            bits.append("align needed")
        bits.append(datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        return SUBTITLE_SEP.join(bits)

    def sweep_metadata_text(self, base, records, cases, segments, planned,
                            ended, ylabel):
        """The whole sweep described once: the settings block as read after
        the last segment, then one line a segment for what varied."""
        freqs = np.concatenate([r["freqs"] for r in records])
        extra = {
            "sweep": f"{len(cases)} case(s) x {len(segments)} segment(s) = "
                     f"{planned} runs",
            "runs completed": str(len(records)),
            "ended early": ended or "-",
            "trace units": ylabel,
            "frequency range (Hz)": f"{np.min(freqs):.12g} to "
                                    f"{np.max(freqs):.12g}",
        }
        scales = sorted({r["units"] for r in records})
        if len(scales) > 1:
            extra["UNITS CHANGED"] = (f"{', '.join(scales)} - the segments "
                                      f"are not all on one scale")
        cautions = sorted({r["notes"]["alignment"] for r in records
                           if r["notes"].get("alignment")})
        if cautions:
            extra["alignment"] = cautions[0]
        head = E.metadata_text(self.an, records[-1]["snap"], extra,
                               self.command)
        table, suspect = [], []
        for group in self.sweep_groups(records, cases):
            table += ["", f"segments of {os.path.basename(base)}"
                          f"{group['suffix']}.csv"
                          + (f"   (case '{group['case']}')"
                             if group["case"] else ""),
                      f"  {'#':>4} {'start (Hz)':>14} {'stop (Hz)':>14} "
                      f"{'points':>6} {'RBW (Hz)':>10} {'sweeps':>6} "
                      f"{'meas (s)':>9}  quality"]
            for i, r in enumerate(group["rows"], 1):
                n = r["notes"]
                quality = n.get("trace quality", "")
                rbw = E.value_of(r["snap"], "RBW")
                table.append(
                    f"  {i:>4} {float(r['freqs'][0]):>14.10g} "
                    f"{float(r['freqs'][-1]):>14.10g} "
                    f"{len(r['freqs']):>6} "
                    f"{'?' if rbw is None else format(rbw, 'g'):>10} "
                    f"{n.get('sweeps', '?'):>6} "
                    f"{n.get('measure time (s)', '?'):>9}  "
                    f"{'SUSPECT' if quality.startswith('SUSPECT') else 'clean'}")
                if quality.startswith("SUSPECT"):
                    suspect.append(f"  {group['suffix'] or 'sweep'} #{i}: "
                                   f"{quality}")
        table.insert(0, "")
        table.insert(1, "the settings above are as read after the last "
                        "segment")
        if suspect:
            table += ["", "suspect segments"] + suspect
        return head + "\n".join(table) + "\n"

    def save_sweep(self, outdir, stamp, cases, segments, done, planned,
                   ended="", ylabel=None, yscale=None, records=()):
        """The whole sweep in one place: a CSV per case with the segments
        joined into one curve, the description, the raw matrices and every
        trace on one pair of axes. Written the same way whether the sweep ran
        to the end or was stopped part way."""
        ylabel = self.last_ylabel if ylabel is None else ylabel
        suffixes = ["_freqs.npy", "_amps.npy", "_axes.json", ".png", ".txt"]
        suffixes += [g["suffix"] + ".csv"
                     for g in self.sweep_groups(records, cases)] or [".csv"]
        base = E.unique_base(outdir, f"{self.safe_title()}_sweep_{stamp}",
                             suffixes)
        if records and self.save_csv.get():
            for path, n in self.write_sweep_csv(base, records, cases, ylabel):
                self.log(f"  {os.path.basename(path)}  ({n} segments)")
        if records and self.save_txt.get():
            with open(base + ".txt", "w", encoding="utf-8") as fh:
                fh.write(self.sweep_metadata_text(base, records, cases,
                                                  segments, planned, ended,
                                                  ylabel))
            self.log(f"  {os.path.basename(base)}.txt")
        if records and self.save_npy.get():
            lengths = {len(r["freqs"]) for r in records}
            if len(lengths) > 1:
                # The point count is a setting like any other and can be
                # changed between cases; a matrix cannot hold ragged rows.
                self.log(f"  (no .npy matrices: the segments have "
                         f"{sorted(lengths)} points, not one length - the "
                         f"CSV has them all)")
            else:
                shape = (len(cases), len(segments), lengths.pop())
                freqs_m = np.full(shape, np.nan)
                amps_m = np.full(shape, np.nan)
                for r in records:
                    freqs_m[r["ic"], r["iseg"]] = r["freqs"]
                    amps_m[r["ic"], r["iseg"]] = r["amps"]
                np.save(base + "_freqs.npy", freqs_m)
                np.save(base + "_amps.npy", amps_m)
                axes = {
                    "shape": "[case][segment][point]",
                    "cases": cases,
                    "segments_hz": ["current" if s is None else list(s)
                                    for s in segments],
                    "points": shape[2],
                    "units": ylabel,
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
            subtitle = SUBTITLE_SEP.join(
                [f"{len(done)} of {planned} runs"
                 + (f" - {ended}" if ended else ""),
                 datetime.datetime.now().strftime("%Y-%m-%d %H:%M")])
            self.write_plot(base + ".png", done, self.plot_title(note="sweep"),
                            subtitle, ylabel,
                            self.last_yscale if yscale is None else yscale)
