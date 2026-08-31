# Spectrum Grab

One-click trace capture from an SRS SR760 FFT spectrum analyzer over GPIB. This
is the `read_sr760fft_data*.py` bench scripts rolled into a single program, in
the same shape as
[keysight-scope-grab](https://github.com/Weiss-Quantum-Computing/keysight-scope-grab):
press GRAB (or the space bar) and
you get, in your chosen folder:

| File | Contents |
|------|----------|
| `<name>.csv` | `Frequency (Hz)` plus one amplitude column, headed with the units the trace was actually measured in |
| `<name>.png` | Plot of the trace |
| `<name>.txt` | Metadata: span, start frequency, window, averaging, input range and every other setting, each with the command that would put it back |

The panel shows the most recent plot inline; double-click it to open the
full-resolution PNG, or press **Zoom / pan** for an interactive copy you can
zoom into. **Peek** draws the trace into the preview box without writing
anything, and **Start average** restarts the average and waits it out without
writing anything either - so looking is never the same as collecting.

## Zoom and pan

**Zoom / pan** under the preview opens the last plot in a window of its own,
with matplotlib's navigation toolbar: rectangle zoom, pan, back, forward, Home
and Save, the same set the ILC panel has. The toolbar prints the cursor's
frequency and amplitude as you move, which is the quickest way to put a number
on a peak.

It is drawn from the trace data rather than from the PNG, through the same
drawing code the PNG goes through - so what you zoom into differs from the file
on disk in nothing but the axis limits, log axis and settings line included. A
window rather than the preview box for two reasons: a spectrum is worth looking
at large, and the preview has to go on showing PNGs this session did not draw,
which have no trace data behind them to redraw from.

**follow new captures** is ticked by default, so a grab, a peek or a sweep step
replaces what is on screen. During a sweep that means the combined plot as it
builds, not each segment on its own - see [What it will
cost](#what-it-will-cost) below. Untick it to hold a zoomed view while a sweep
or an auto-grab runs. While following, a new capture resets the view -
including the toolbar's history, since going Home to the previous capture's
limits would mean nothing.

The window needs matplotlib's Tk backend. A Python that has matplotlib but not
that still saves plots and shows the preview; the button just says so.

## Two files

`sr760.py` is the instrument layer **and** the scripting library;
`spectrum_grab.py` is the panel and imports it. Same split as
[BK4063B-AWG-GUI](https://github.com/Weiss-Quantum-Computing/BK4063B-AWG-GUI):
one copy of the command spellings, the settings model, the status handling and
the file format, shared between the panel and anything that scripts the
analyser. **`sr760.py` has to sit beside `spectrum_grab.py` for the panel to
run.**

```python
from sr760 import SR760, PRESETS, TRACE, N_BINS

with SR760() as an:                    # or SR760(connect=False) then .connect()
    an.apply(PRESETS["protocol"])
    an.pin_range(-30)
    an.start()
    an.wait_done(600)
    an.refresh_status()                # one read; every reader shares the cache
    f, a, used_binary = an.trace(TRACE, N_BINS)
```

`sr760.py` imports numpy and pyvisa and nothing else - no tkinter, no
matplotlib, no pillow - so the headless protocol runner in Trek-EOM-ILC can
import it on the bare system interpreter. The class is `SR760`, with
`Analyzer = SR760` kept as an alias so older imports keep working.

Writing files through the library is what makes a scripted capture
indistinguishable from a panel one: `write_csv`, `metadata_text`, `safe_name`
and `unique_base` all live there, so both produce the same names and the same
metadata block.

## Tests

```
python tests/test_grab.py
```

No GPIB and no instrument: a fake analyzer stands in for the SR760, and the
panel is built for real - every widget, with Tk withdrawn - so `run_one`,
`save_run` and `_grab_worker` are driven the way a bench session drives them.
Nothing opens a VISA session or touches the saved config.

Almost every check is a regression, and each names the wrong answer it used to
give, because none of these failures looked like failures: a trace labelled
`dBV` when the analyzer was on volts, an error bar five times too good, an
overload line reading a confident `no` when half the status timed out. The
analyzer never complains about any of it, so the test has to.

`sr760.py` is loaded by path from the protocol runner in Trek-EOM-ILC, so a
change here lands there with no import error to warn you. That repo's
`tests/test_protocol_sr760.py` is the other half of this one, and its
`tests/test_protocol.py` and `tests/test_rin.py` are worth running too.

## Presets

**These are library blocks, not a panel button.** Stage preset used to offer a
choice between them, which made every session open with a question about which
discipline was in force - and the settings panel already shows every value and
marks the ones that differ from the analyzer, which is the same information
without the choice. `sr760.PRESETS` stays for the protocol runner, which
applies one by name because a script has no panel to read.

- **legacy** - byte-identical to what the `read_sr760fft_data` bench scripts set
  at the top of every run, `ARNG:1` and `NAVG:1000` included. This is what the
  old data was taken under and what to load to reproduce it.
- **protocol** - the RIN validation discipline. `ARNG:0` because the range is
  pinned for a whole set; `OVLP:0` because with no overlap `NAVG` **is** the
  independent record count, so `record_stats` becomes a check on the run rather
  than a correction to it; `NAVG:100` for a 10% (0.41 dB) bin error, which is
  the accuracy segment overlaps are compared at. PSD in Vrms, which
  `trace_units()` reports as `Vrms/sqrtHz` - the V/rtHz the RIN maths wants.
  Span and start frequency are deliberately absent: they are per-segment and
  belong in the measurement-set definition, not in a global preset that would
  move the band out from under a segment.

`SCRIPT_DEFAULTS` still exists and still means `PRESETS["legacy"]`.

Two averaging modes are refused for noise work, by `averaging_fault()`. Vector
averaging averages the complex spectrum, so anything not phase-locked to the
trigger averages toward zero - on noise that is a floor which falls forever as
NAVG rises, beautifully clean and completely wrong. Exponential averaging never
settles on a count, so the statistics cannot be stated. Either one marks the
trace `SUSPECT` in the metadata and on the plot; the protocol runner refuses to
start at all.

`overlap_fault()` is the third of them, and it is about attribution rather than
arithmetic. The statistics do not depend on it — `record_stats` counts
independent records from the clock, `elapsed / T_rec`, which is right whatever
the overlap turns out to be — but NAVG is the number on the front panel and the
number anyone reads off the file, so when the overlap has eaten it the trace
should say what ate it:

```
SUSPECT: 98.4375% overlap: 400 averages are worth 7.23 independent records,
so NAVG overstates the statistics 55x - and 98.4375% is this span's default,
so a SPAN write may have installed it in place of the value that was asked for
```

It only speaks when the overlap is actually costing something — past 1.5x, the
threshold `stats_notes` uses — and it names the span only when the overlap sits
exactly on that span's default, since it can also be a deliberate choice.

`code_of()` reads an enum's index and truncates, which is right for a choice and
wrong for a quantity: `OVLP 98.4375` came back from it as `98`. **`value_of()`
is the one to use for the `num` settings.**

## The status byte is cached

Reading an SR760 status byte **clears** it, so whoever reads first consumes the
flag and everyone after them sees a clean instrument. `refresh_status()` does
the one real read of `ERRS` and `FFTS`; `error_byte()`, `overload()` and
`status_line()` all report from the cache. `start()` invalidates it, because a
flag raised before a run says nothing about the run - and if nothing has
refreshed since, `overload()` refreshes rather than returning a pre-capture
value. Stale-but-plausible is the failure this exists to stop.

The capture path calls `refresh_status()` exactly once, straight after
`wait_done()`.

## Requirements

- NI-488.2 (or any VISA with GPIB support). Check in NI MAX that the analyzer
  shows up, e.g. as `GPIB0::10::INSTR`.
- Python 3.9+
- `pip install pyvisa numpy matplotlib pillow`
- `sr760.py` beside `spectrum_grab.py` - the panel imports the instrument
  layer from it. Scripts need only that file, numpy and pyvisa.

matplotlib and pillow are both optional in the sense that the app starts and
captures without them: without matplotlib there are no plots and no preview
(the CSV and metadata are still written), and without pillow the preview falls
back to Tk's integer subsample. The log says so at startup when matplotlib is
missing - which happens when the app is launched with a Python that does not
have it, e.g. the bare python.org install rather than the Anaconda one the bench
scripts run under.

## Usage

```
pythonw spectrum_grab.py
```

`pythonw` keeps the console window from appearing. The app connects to the
address in the box on startup; clear the box and press **Connect** to scan every
GPIB resource instead and take the first one that identifies as an SR760.

- **Save to** - output folder, plus **dated subfolder**, which appends
  `YYYYMMDD` the way the scripts built the date into `PATHNAME`.
- **Title** - the front of every file name. Characters illegal in file names
  are replaced with `_`.
- **csv / plot / metadata** - which of the three files each grab writes.
- **Space** grabs, except while the focus is in a text field or on a control
  that uses the space bar itself, so typing a space in the title box does not
  fire an acquisition.
- **Start average** - restarts the average (`STRT`, the front panel's START
  key) and waits for it to finish, then autoscales. No trace is read and no
  file is written: use it to leave a finished average on the screen, or to find
  out how long one takes, without filling the folder with captures nobody
  asked for. Stop ends the wait, not the analyzer's average.

  Only a *linear* average ever reports itself finished. An exponential average
  goes on re-weighting the newest record forever, and with averaging off nothing
  is counting at all, so in both cases there is no completion bit to wait for
  and the **exponential wait** below decides how long it runs instead. At 0 s
  the app just sends `STRT` and hands the GUI straight back. The averaging mode
  is asked of the analyzer each time, since it is one knob turn away on the
  front panel.
- **Peek (saves nothing)** - reads the trace as it stands and plots it in the
  preview box, writing nothing. It does not restart, range or settle, so an
  average part way through is left exactly as it was and can be looked at again
  as it builds. The frame says `not saved` and double-click does nothing, so a
  peek can never be mistaken for a capture. Peak amplitude and its frequency go
  in the log.
- **Auto-grab** - repeat on a fixed interval.
- **Stop** - ends a sweep after the step in progress and releases the front
  panel. It also ends a Start average wait and a peek's bin-by-bin readout.

## Range hold

A locked-range measurement set is one where the comparison IS the result: dark
against light, resistor against resistor, segment A against segment B. Auto
range moves the input range whenever the signal asks it to, and a range step is
a step in the noise floor, so those pairs end up differing by the ranging as
much as by the physics with nothing in the files to say which.

**Range hold** arms once and stays armed across as many grabs as the set takes:

- **Auto-range and pin** runs the ranging routine, then pins whatever it settled
  on. **Pin as-is** pins the range that is already set, which is what you want
  when it was chosen by hand at the front panel - the usual case for a segmented
  measurement where each band sits just under overload.
- While held, every capture re-asserts `ARNG 0` and the pinned `IRNG` before
  measuring. That is not belt and braces: the front panel can put auto range
  back, and so can a Defaults apply.
- After the average and **before the trace is written**, the range is read back
  and compared. A range that moved makes the trace `SUSPECT` in the metadata and
  on the plot, rather than saving it as clean. So does a range that could not be
  read back at all - unverified is not the same as verified.
- **Release** stops holding. It does not move the range; it only stops pinning
  and checking it.

`SCRIPT_DEFAULTS` ships `ARNG:0` for the same reason. The bench scripts shipped
`ARNG:1`; that is right for a survey and wrong for every comparison.

## What a run is worth

`NAVG` counts records the analyzer averaged, not independent ones. With `OVLP`
above zero the records share samples, so they carry less information than their
count suggests. The metadata now records what the error bar actually rests on:

```
record length T_rec (s)   : 1.026        # bins / span
independent records       : 117.0        # elapsed / T_rec
relative error (1 sigma)  : 0.0925       # 1 / sqrt(N_indep)
relative error (dB)       : 0.38
averages reported (NAVG)  : 1000
NAVG / independent        : 8.55  <- NAVG overstates the statistics
overlap (%)               : 90
```

At 90% overlap, 1000 averages in 120 s are worth 117 - the reported figure
overstates the statistics eight-fold. The 1-sigma figure also goes on the plot,
so a segment carries its own error bar into the comparison with its neighbour.

## Settling

**settle (record lengths)**, default 5, waits after any change of span, start
frequency, range or coupling before averaging starts, and the value used goes in
the metadata. It is in record lengths because that is what the analyzer's
settling scales with: at the 191 mHz span a record is 35 minutes and at 100 kHz
it is 4 ms, so a fixed number of seconds is either uselessly long at one end or
no wait at all at the other.

This is not what `wait_ready()` does. That waits for the analyzer to answer a
query again, which it does perfectly happily while its decimation filters are
still full of the previous span's data. **settle before start (s)** is still
there and is added on top, unconditionally.

## Overload

The error status byte is read straight after every averaging run, before
anything else can clear it, and `overload` goes in the metadata. An overload
here need not show on the trace at all: the input stage sees everything the
anti-alias filter passes, so a servo bump at 150-300 kHz or RF on an
unterminated line can saturate the front end while the displayed 0-100 kHz band
looks perfectly clean. A trace taken through a saturated front end is not a
measurement of anything, and nothing on the screen says so - which is why it is
called out in the log, written to the metadata and printed on the plot.

## File names

```
<title>[_<case>]_span<code>[_strf<start>Hz]_<stop freq>Hz_<date>.csv
```

The start frequency appears only when it is not 0 Hz, and the case only when
you are sweeping cases. A name already taken gets `_1`, `_2` and so on - and the
counter is chosen so the csv, png and txt of one capture always share a name
rather than drifting apart when only some of the three exist.

## Analyzer settings

The right-hand panel mirrors the instrument: span, start and center frequency,
window, measurement type, display mode and units, input source, coupling,
grounding, range, auto range and auto offset, the averaging block, and the
display and marker settings. It reads on connect, on **Read**, and automatically
after every measurement, so settings changed with the front panel show up
without asking.

### What the averaging is worth

The Averaging block has five settings laid out two to a row, which leaves one
cell of the grid empty. That cell holds a read-out with nothing to fill in: it
reads the **Number** and **Overlap (%)** boxes as you type them — before Apply —
and says what they buy.

```
worth all 100 records          worth 7.23 of 400 records
1 sigma 0.1 (0.41 dB)          1 sigma 0.372 (1.37 dB)
```

`7.23 of 400` says what the overlap ate more plainly than a multiplier would,
and the line turns amber past 1.5x.

NAVG counts records the analyzer averaged, not independent ones. Records that
overlap share samples, so `N` of them are worth `1 + (N-1)(1 - overlap)` — the
same count `record_stats` arrives at from the clock afterwards, and the same one
the sweep's time estimate is built on. At `OVLP 0` it is just `N`, which is one
more thing the `protocol` preset buys.

### Holding the overlap, or letting it go

A SPAN write reinstalls that span's default overlap, and **let a span change
reset the overlap** in Acquisition decides what happens next. Unticked (the
default) the held value is read back and put in again, so a span sweep cannot
change what the averaging is worth. Ticked, the span's default stands.

That is a speed dial, not a correctness one, because `record_stats` counts what
actually arrived either way. At NAVG 400:

| span | held at `OVLP 0` | reset to the default | independent records |
|---|---|---|---|
| 9  | 27m40s | 42.4s | 400 → 5.0 |
| 11 | 6m56s  | 14.0s | 400 → 7.2 |
| 13 | 1m45s  | 9.4s  | 400 → 25.9 |
| 15 | 27.4s  | 8.3s  | 400 → 100.8 |
| 17, 19 | — | — | no default overlap, so no difference |

A six-span sweep goes from 37 minutes to 1m25s. The sweep estimate follows the
setting, pricing each span at the overlap it will really run at, and the
read-out under Averaging prices whichever way it is set. Two things to watch:
the narrow spans are where the trade is, and holding the overlap there can push
a run past the **measurement timeout** — NAVG 400 at span 9 wants 27m40s
against a 600 s default, so it would be cut short and read early.

It matters because **SPAN reinstalls its own default overlap**, up to 98.4375%
at the narrow end, so NAVG can be honoured to the letter while the trace is
worth a fifty-fifth of it. 1.5x is the threshold the metadata file uses too. The
read-out also names the cases where there is no count to state at all: averaging
off, exponential mode, and vector or peak-hold averaging.

## The span table is exact

`SPANS` carries the analyzer's printed **labels** and its true **frequencies**,
which are not the same thing. Every span is 100 kHz halved: code 11 is 390.625
Hz where the front panel says 390, and code 13 is 1562.5 Hz where it says
1.56 kHz. The table used to hold the printed figures, which put 0.16% into every
record length and into everything costed from one — settles, run estimates,
independent-record counts.

The measured default overlaps are the proof: exactly 93.75% at code 13 and
98.4375% at code 11. Those are `1 - 0.016/T_rec` for record lengths of 0.256 s
and 1.024 s, which are 400 bins over 1562.5 Hz and 390.625 Hz and nothing else.
With the rounded table they came out 93.76 and 98.44.

`run_protocol.py` keeps its own copy of the table, because the planner has to
cost a session on a machine with no instrument code; `check_span_table()`
reconciles the two whenever `sr760` is importable.

To change something, edit the field and press **Apply changes**:

- Only edited fields are written. A `*` next to a field marks it as edited but
  not yet applied, and the status line counts them.
- A read never discards an edit you have not applied yet. If a value changes on
  the analyzer while you have a pending edit for the same field, your edit stays
  in the box and the log says the analyzer disagrees.
- After a write the panel re-reads the instrument, so what you see is what the
  analyzer accepted rather than what you asked for.
- The error status byte is read after every apply and anything non-zero is
  logged.

**Auto-offset** and **Auto-scale** send `AOFF` and `AUTS 0`. Auto offset runs
on the analyzer for several seconds with the bus ignored, so the panel read that
follows waits until the analyzer answers `SPAN?` again before asking anything
else - reading straight away spent a VISA timeout on each of the first few
queries and dropped those settings for the rest of the session, which is where
the `SPAN? / STRF? / CTRF? failed: VI_ERROR_TMO` run of failures came from. The
log says how long the wait took.

Settings traffic and captures share one VISA session, so they are serialised:
the buttons grey out while a measurement is running and vice versa, and Connect
is refused mid-sweep rather than pulling the session out from under it.

## Sweeps

All three boxes are optional. Blank means "leave the instrument where it is",
so an empty Sweep panel is a single grab at the current settings.

- **Span codes** - e.g. `9, 11, 15`. Codes are 0-19; the panel's Span dropdown
  shows which frequency each one is.
- **Start freqs (Hz)** - e.g. `0, 300, 600`, or `0:1000:47` for start:stop:step.
- **Cases** - free-text labels, e.g. `in lock, out of lock, no light`. **A case
  sets nothing on the analyzer.** It is a label for something *you* change by
  hand between runs — a shutter, a cable, a resistor, the lock — so it can be
  anything at all, and the panel neither knows nor checks what it was. What it
  does is go into the file names and the plot titles, become an axis of the
  `.npy` matrices, and — with **pause before each case** ticked — stop the sweep
  with a "set up case 'X', then continue" prompt before each one, which is the
  scripts' `input("Press Enter to continue")`. Anything the analyzer itself can
  set belongs in the settings panel or in the span and start-frequency lists
  instead.

The three nest: every case runs every start frequency at every span, and
**RUN SWEEP** is what starts it.

**GRAB one** and **RUN SWEEP** are separate buttons. GRAB one takes a single
capture at whatever the analyzer is set to and does not read the sweep boxes at
all; RUN SWEEP reads them and refuses if they are empty. They used to be one
button whose behaviour depended on three boxes elsewhere on the panel, so a
stitch left in them turned the next single capture into an eight-hour run.
Space is still GRAB one, and **Auto-grab** takes single captures for the same
reason — it sits under that button.

### One set of files, not one per segment

A sweep writes its output **once, for the whole sweep**:

```
<title>_sweep_<date>.csv          every segment, sorted into one curve
<title>_sweep_<date>.txt          the settings, and a line per segment
<title>_sweep_<date>_freqs.npy    shape [case][start frequency][span][bin]
<title>_sweep_<date>_amps.npy     same shape
<title>_sweep_<date>_axes.json    what each axis is
<title>_sweep_<date>.png          every trace on one pair of axes
```

Six files. It used to write a CSV, a plot and a metadata file for *every
segment* as well — a 65-segment stitch left 199 files behind, and all of it was
in the combined output too.

The combined CSV carries a third column saying which segment each point came
from, so joining them loses nothing: the overlaps stay separable, and a
two-column reader that ignores it still sees the stitch.

**What joins and what does not.** Start frequencies tile a band, so they join
into one curve — that is what a stitch *is*. A case and a span do not:

| varies | one file or many | why |
|---|---|---|
| start frequency | **one** | segments of one band at one resolution |
| span | **one per span** | a different bin width, so a different resolution and noise bandwidth — two spans over one band are two measurements of it, not two pieces of one |
| case | **one per case** | whatever you changed by hand; separate measurements by construction |

So `<title>_sweep_<date>.csv` for a stitch, and
`<title>_sweep_<date>_dark_span11.csv` when a sweep varies both. The span only
enters the name when the sweep actually used more than one, so a single-span
stitch keeps the name it has always had. The `.txt` stays one file for the whole
sweep, with a table per CSV, numbered to match its `segment` column.

The combined `.txt` is what the per-segment ones used to be, minus the
repetition. The settings block is written once — a sweep holds the analyzer
still apart from the span and start frequency it is moving on purpose — and
what varies goes in a table:

```
segments  (the settings above are as read after the last one)
   #  case   span   start (Hz)   top (Hz)  meas (s)  N_indep  1 sigma  overload  quality
   1  -        11            0        390    102.6    100.0      0.1   no        clean
   2  -        11          390        780    102.6    100.0      0.1   no        clean
```

A segment the run flagged is quoted in full underneath, because the whole point
of the flag is that nothing on the trace shows it.

**keep per-segment files** puts the old behaviour back if you want a segment on
its own — you get both sets. A single **GRAB one** is unaffected either way: it
writes its own csv/plot/metadata as it always did.

### Folders taken before that

```bash
python tools/consolidate_sweep.py <folder> --apply
```

brings a folder written the old way up to the same shape: it builds the
combined `.csv` and `.txt` from the per-segment files, checks the result reads
back identically, and then **moves** the per-segment files into a `segments/`
subfolder. It does not delete them, and the reason is that they are not
redundant — the `.npy` matrices hold frequencies and amplitudes and nothing
else, so the units, the input range, the averaging and the overlap each segment
was taken under exist only in those `.txt` files. Once you have looked at what
it produced, `segments/` is yours to remove.

Without `--apply` it says what it would do and changes nothing.

Two things it works out for itself. A title swept **twice on the same day** is
two acquisitions, and the `_1` that `unique_base` adds does not separate them —
the second sweep only collides on the start frequencies the first one reached,
so an aborted 14-segment run followed by a 52-segment one came out as 52
unsuffixed files and 14 suffixed ones, mixed. What does separate them is that a
sweep visits each (span, start frequency) once, so in capture order a repeat
means a new sweep began. That holds whatever the span is, which a gap in the
clock does not — at the 191 mHz span one segment takes thirty-five minutes. The
second run is written as `_sweep_<date>_1` and reads as `(run 2)` in Compare.

And a **single capture** is left exactly where it is. One segment is not a
sweep, and folding it into a combined file of one would only rename it.

### What it will cost

A sweep of more than one run prices itself before it starts, and counts down as
it goes:

```
Sweep: 1 case(s) x 10 start freq(s) x 1 span(s) = 10 runs
  about 18m12s of measuring, finishing around 22:55
--- span 11
  run 1/10 - 0.0s in, about 18m12s left, done ~22:55 (estimated)
--- span 11 start 1 Hz
  run 2/10 - 1m50s in, about 16m24s left, done ~22:55
```

The estimate is the analyzer's own arithmetic: a record is `bins / span`, so a
linear average of `NAVG` records takes `NAVG * T_rec`, plus the settle, the
autorange and the transfer. NAVG, the averaging mode and the overlap are read
from the analyzer once before the loop rather than guessed at. It is exact at
`OVLP 0` - which is what the `protocol` preset sets, and one more reason it does
- and a floor with overlap, because the analyzer still has to finish an FFT
between records that share samples.

It does not stay a model. Each figure is rescaled by what the finished runs
actually took, weighted `n/(n+2)` so that one freak-fast run cannot drag the
whole estimate to zero and a genuinely slow sweep is believed by the third or
fourth. Time spent waiting at a **pause before each case** prompt is held out of
that correction - a sweep that waited twenty minutes for someone to move a cable
has not learned that its runs are slow - and the up-front line says so.

An average with no finish of its own is priced at the **exponential wait**, not
at NAVG, and the **measurement timeout** caps the estimate the way it caps the
wait. If the sweep names no span and the analyzer will not say which one it is
on, there is no estimate rather than a made-up one.

### Watching it build

The preview and the zoom window show the combined plot **as it builds**, a trace
at a time, rather than flashing through each segment on its own and assembling
the picture only at the end. Waiting until the last run to find out that segment
three is sitting on the wrong range, or that the overlaps are not joining, is a
long wait when a run is two minutes.

It is drawn through the same code and in the same colours as the PNG written at
the end, so nothing shifts when the sweep finishes - only the title note, from
`(sweep building)` to `(sweep)`, and the `3 of 6 runs so far` under it. The
frame above the preview says `Sweep building - 3 of 6 runs` while it goes, and
double-clicking does nothing until there is a file: the combined PNG is written
once, when the sweep ends.

Each run's own CSV, plot and metadata are still written as they always were.
The change is only which of them the window is showing. Untick **combined
plot** and the window goes back to showing each segment as it lands, and a
single grab is unaffected. The redraw is throttled to once every couple of
seconds, because every trace so far is redrawn each time and a long sweep of
short runs would otherwise spend its time drawing rather than measuring.

The `.npy` pair in that set is the `freqs_matrix` / `amps_matrix` from the
scripts, with the axis values written down next to it instead of living in a
comment.

**Stop** writes all of that too. A sweep cut short is written up exactly as a
finished one - same file names, the combined plot of the segments captured so
far - because the usual reason for stopping is that what you have is enough. The
difference is that it says so rather than passing itself off as complete:

- the matrices keep the shape the sweep was planned at, and cells no run reached
  stay `NaN`, so the indices still line up with the axis values in the JSON and
  nothing reads as a measurement of zero;
- `runs_planned`, `runs_completed` and `ended_early` go in the JSON;
- the plot title carries the shortfall, e.g. `(stopped after 3 of 8)`.

A sweep that dies on an instrument error is written up the same way, so a fault
on the last segment does not cost you the earlier ones.

## Comparing sequences

**Add sequences…** picks saved captures off disk and draws them underneath
whatever you measure next — this week's floor against last week's, a device
against a 50 Ω termination, one range step against another.

A *sequence* is what a stitch leaves behind: the CSVs of one title, taken on one
day, joined back into a single curve in frequency order. Overlapping points
where segments meet are both kept rather than averaged, which is what makes a
bad join visible. Pick any of a sequence's CSVs in the dialog and the ones you
picked are grouped by the name and the folder they share, so a comparison across
dated folders — the usual one — takes one trip through the dialog.

They are read through the CSVs and not the `.npy` matrices for one reason: **the
matrices do not record the units**. The CSV header does, and without it there is
no way to know that `20260826` is in `dBVrms/√Hz` and `20260830` is in
`Vpk/√Hz`. Stacking those two raw would put `1e-8` against `-160` on one axis.

So everything is converted onto the scale of the plot it is going under, through
volts peak, and the legend says what each sequence *was*:

```
50 ohm full span grounded  20260830  (66 seg)
Trek X2 mon no drive  20260826  (65 seg), was dBVrms/√Hz
```

What cannot be converted is left out and named in the log, once, rather than
drawn on an axis that does not describe it. A spectrum and a spectral density
are not the same measurement and converting between them needs a bin width no
file here carries; phase is not an amplitude at all. A linear reading of zero or
less has no dB equivalent and becomes a gap rather than dragging the axis to the
floor.

Loaded sequences show up in three places:

- **underneath every capture** — grabs, peeks and the building sweep alike — in
  grey and behind, so the colours go on meaning the thing being measured;
- **on the saved PNGs**, since a plot that was read against a reference should
  carry it;
- **as the subject of their own plot**, with **Plot comparison**, which gives
  them the colours and writes `<title>_compare_<date>.png`. That one is drawn on
  the first loaded sequence's scale — the first whose units are recognised, so a
  stray headerless CSV cannot become a target nothing else can convert to.

They outlive a grab and a sweep on purpose: the reason to load last week's floor
is to take this week's against it, and that is several captures. **Clear** drops
them. Nothing is ever written back to the files they came from.

**Stitch to _x_ Hz, overlap _n_ points** fills the start-frequency box with
segments that tile 0 Hz up to _x_ at the span the analyzer is on, stepping by
`(400 - n)` bins so each segment repeats the last _n_ frequency points of the
one before.

The overlap is counted in frequency points rather than in hertz because that is
what makes the pieces line up: stepping a whole number of bins puts the shared
points at *the same frequencies* in both runs, so they can be matched or
averaged directly instead of interpolated. An overlap of 0 still leaves no gap -
the next segment starts one bin past the last point of the previous one.

The bin spacing is read from the analyzer (`BVAL?` at both ends of the trace,
divided by the 399 intervals between them) rather than derived from the span
table. The table is exact now - every span is 100 kHz halved, and code 11 is
390.625 Hz rather than the 390 the front panel prints - so that is no longer
the reason. What is left is that measuring end to end rather than between two
adjacent bins divides the analyzer's own printed rounding by 399, and that this
is the same spacing the frequency column of the CSV is built from, so the
overlapping points land exactly on saved data.

Because the spacing comes from the instrument, Fill needs a connection, and it
declines while the panel has a span change you have not applied yet - it would
otherwise measure the old span. Offline it falls back to the span table and says
in the log that the step is an estimate.

### The top of the band

Nothing is offered a start frequency whose span would run off the end of the
analyzer's 100 kHz band. The SR760 clamps a start it cannot honour and says
nothing about having done so, so a stitch that walked past the top used to hand
it start frequencies it quietly moved — and two starts past the limit both land
in the same place and measure the same band twice.

Where the band runs out before your stop frequency does, the last start is
**pulled down** to the highest one that fits rather than dropped:

```
66 segments of 400 points at 3.9058 Hz per point (measured on the analyzer),
stepping 390 points (1523.26 Hz) to 99996.1 Hz - neighbours share 10 point(s).
  (the last segment starts at 98437.68 Hz rather than 99012.03 Hz, which would
  have run past 100000 Hz. The band is still covered to the top; that segment
  shares 157 points with the one before it instead of 10.)
```

So the top of the band is still measured, and the cost is only that the last two
segments repeat more frequency than the rest — which is the same kind of overlap
the stitch is built on. A stop frequency above 100 kHz is capped, and said. A
span wider than the band is refused outright.

One case is deliberately *not* pulled down: a stitch that needs more segments
than a sweep will take (`MAX_LIST_ITEMS`, 2000) stops at the limit and says the
top is not covered. Moving its last start to 100 kHz would leap over everything
in between and call the gap an overlap.

## Acquisition

- **fast binary transfer (SPEB?)** - pulls the whole trace in one read instead
  of two queries per bin, about two orders of magnitude quicker. The dump is a
  dB mapping of the display, so it is only used while the display is LogMag;
  with a linear display the app falls back to the bin-by-bin readout on its own
  and says so in the log. Bin 0 is read the slow way either way and used to put
  the dump back on the analyzer's own scale - in dB when the units are dBV or
  dBVrms, and through dB and back out again when they are Vpk or Vrms, since
  `SPEC?` then answers in volts. A bin 0 with no dB equivalent (zero or
  negative) drops the capture to the bin-by-bin readout rather than returning a
  trace that is quietly wrong.
- **lock front panel while measuring** - `OVRM 0` for the duration of the
  measurement, so a stray knob cannot change the settings the metadata claims
  were used. It is released again even if the run fails.
- **auto range (ARNG)** - the analyzer's own auto range, left switched on
  rather than frozen. It is in the settings panel too, but there it is an edit
  waiting for Apply; the checkbox goes to the analyzer the moment it is
  clicked, and follows the instrument again on the next read, so it always
  shows what `ARNG` actually is.
- **before each grab, auto-range then freeze** - the scripts' ranging routine: auto range on,
  leave it long enough to settle while watching the overload bits, then back to
  manual so the range stays put for the rest of the sweep and the noise floors
  stay comparable. The range it settled on and how often an overload showed up
  go in the metadata.
- **settle before start** - dead time between setting the span and starting the
  average, for anything that needs to ring down first.
- **measurement timeout** - how long to wait for a *linear* average to finish.
  On expiry the trace is read as it stands and the log says so, rather than
  hanging forever.
- **exponential wait** - how long to let an average run when it has no finish of
  its own: exponential averaging, or averaging switched off. Neither ever sets
  the completion bit, so there is nothing to wait for and the run length has to
  be stated instead. The measurement timeout is the wrong knob for it - that is
  the limit on a wait that normally ends by itself, so reusing it made every
  such capture take the full ten minutes. Stop cuts the wait short. The
  metadata and the plot notes record what actually happened,
  `30 s of exponential averaging`, rather than calling it a timeout.
- **plot y min / y max** - the default plot window, for dB traces only. It is
  kept unless the trace falls outside it, and the notes line under the plot
  title says `y-scale widened to fit the trace` when it had to be widened, so a
  plot that does not compare with the others is flagged. A volt trace is
  autoscaled instead, on the axis the Display setting implies.

## Plot titling

The title is what the capture is, in the words that were typed: the **Title**
box, plus the case when sweeping cases, plus `(sweep)` or `(peek)` where that
applies. The file name is on the file already, and its underscored, span-coded
form made a poor heading for a picture that ends up in a talk or a logbook.

Underneath it, in smaller grey type, is the handful of settings that decide what
the trace means, read from the same snapshot the metadata file is written from:

```
span 11 - 390 Hz  .  0 to 390 Hz  .  Hanning window  .  1000 RMS averages  .  2026-08-30 19:03
```

A measurement that timed out or was stopped says so there too, since the picture
itself gives no hint of it, and so does a widened y-scale. The line wraps
between whole items rather than running off the edge of the figure, and a long
title wraps as well. A combined sweep plot gets `N of M runs` instead of the
per-capture settings.

## Units

The CSV header, the y-axis label and the metadata all come from the analyzer's
own `MEAS`, `DISP` and `UNIT` codes rather than being typed in: PSD adds
`/sqrtHz`, and a phase display gives `deg` or `rad`. Change the units on the
analyzer and the files follow. Plots print it as `Vpk/√Hz`; the CSV header and
the metadata keep the ASCII `sqrtHz`.

The **y axis follows the Display setting**, so the plot is drawn the way the
analyzer is drawing it. Only LogMag on volt units gets a log axis: dB data is a
log axis already - taking the log of it again means nothing, and a dB reading is
usually negative, which a log axis cannot draw at all - while Real and Imag are
signed linear quantities and Phase is degrees or radians. A volt trace that
reaches zero cannot go on a log axis either, so it is drawn linear and the notes
under the title say why.

**`UNIT` alone decides the scale the data comes back on.** The display mode does
not - a LogMag display with Vpk units still answers `SPEC?` in volts. This is
measured rather than assumed: a floor the analyzer drew at 10 nV/√Hz, and called
-161 dBV/√Hz once its units were switched to dBV, is 1e-8 V, and 1e-8 V is what
`SPEC?` returned while the display was on LogMag.

Getting that backwards did real damage. The old rule claimed dB whenever the
display was LogMag, which mislabelled every volt-unit trace, and - because the
binary dump is rebased on bin 0 read with `SPEC?` - added a linear value to a dB
trace. That pinned bin 0 near zero and dragged the rest of the trace with it, so
the 10 nV/√Hz floor above came out of the app at about 0 dBVpk/√Hz, some 160 dB
adrift. Captures taken in dBV or dBVrms were never affected.

## Remembered settings

The address, folder, title, sweep lists and every acquisition option are written
to

```
%APPDATA%\SpectrumGrab\config.json
```

so a session starts where the last one left off. The file is written after a
capture, when you pick a folder, and on close - only when something actually
changed. It lives outside the program folder, so updating this repo will not
touch it. Delete it to go back to defaults; a missing or malformed file is
ignored, each value falling back to its default independently.

## Notes

- The SR760 ends a command at the first line feed, so the write terminator is
  pinned to `\n`. If the first `*IDN?` times out the app assumes the analyzer is
  still answering on RS232, sends `OUTP 1` and asks once more - which is the
  `*IDN?` timeout the scripts worked around by hand.
- A measurement is finished when bit 0 of the serial poll byte comes back set;
  the poll starts half a second after `STRT` so it cannot catch the bit left
  over from the previous run.
- An SR760 command either takes the graph number as its first parameter or it
  does not, and that decides both how it is queried and how it is written:
  `WNDO? 0` / `WNDO 0,2` for one, `FMTS?` / `FMTS 1` for the other. Getting it
  wrong is silent in one direction and not the other - the query times out, but
  the write is *accepted and does nothing*, because `WNDO 2` reads as a graph
  number and `FMTS 0,1` stops at the leading 0.

  So each setting carries both spellings and is asked with each until one
  answers; whichever query works also fixes the shape of the write, and the
  pairing is remembered for the session. The log names any setting whose
  spelling was not the expected one. This is worth knowing when reading the old
  scripts: `sr.write("WNDO 2")` never set the window, so those runs used
  whatever window the front panel had. `FMTS 0,0` and `MRLK 0,0` did land, but
  only because the value wanted was the same 0 the analyzer took from the first
  parameter.
- A setting neither spelling answers becomes write-only: it is dropped from the
  read after two failures (each costs a VISA timeout) until the next explicit
  **Read**, and stays editable - Apply still writes it (in the likelier
  spelling), and the panel then takes that write at face value rather than
  leaving it marked as unapplied for ever.
- After an Apply, anything the analyzer reports back differently from what was
  asked is named in the log - `(Window: asked for BMH, the analyzer reports
  Hanning)` - so a value that was clamped, rejected or silently ignored says so
  instead of just reverting in the panel.
- A non-zero error status byte after an Apply is reported by bit number. Bit 7
  on its own is the input overload flag - the same bit the ranging routine
  watches - and means the input is too hot for the current range, not that the
  analyzer rejected a command.
- Each measurement reads the instrument's settings exactly once, and the panel,
  the plot label and the `.txt` file are all rendered from that one snapshot, so
  they can never disagree about what the analyzer was doing.

The `read_sr760fft_data*.py` scripts this replaces are not in the repository -
they carried hard-coded lab paths. Nothing here reads them; they are referred to
above only to say where a behaviour came from.
