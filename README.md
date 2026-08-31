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

**follow new captures** is ticked by default, so a grab, a sweep step or a peek
replaces what is on screen. Untick it to hold a zoomed view while a sweep or an
auto-grab runs. While following, a new capture resets the view - including the
toolbar's history, since going Home to the previous capture's limits would mean
nothing.

The window needs matplotlib's Tk backend. A Python that has matplotlib but not
that still saves plots and shows the preview; the button just says so.

## Requirements

- NI-488.2 (or any VISA with GPIB support). Check in NI MAX that the analyzer
  shows up, e.g. as `GPIB0::10::INSTR`.
- Python 3.9+
- `pip install pyvisa numpy matplotlib pillow`

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

**Defaults** stages the block of settings the bench scripts wrote at the top of
every run - PSD, LogMag, Vrms, Hanning, 1000 linear RMS averages, input A, AC,
float, auto range and auto offset on, 390 Hz span from 0 Hz. Nothing is sent
until you press Apply, so you can look the whole block over first.

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
- **Cases** - free-text labels, e.g. `in lock, out of lock, no light`. Each one
  goes into the file names, and with **pause before each case** ticked the sweep
  stops and waits for you before starting it - the scripts'
  `input("Press Enter to continue")`, for setting something up by hand between
  runs.

The three nest: every case runs every start frequency at every span. A sweep of
more than one run also writes

```
<title>_sweep_<date>_freqs.npy    shape [case][start frequency][span][bin]
<title>_sweep_<date>_amps.npy     same shape
<title>_sweep_<date>_axes.json    what each axis is
<title>_sweep_<date>.png          every trace on one pair of axes
```

which is the `freqs_matrix` / `amps_matrix` pair from the scripts, with the axis
values written down next to it instead of living in a comment.

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
table, for two reasons: the table holds the manual's printed values, which are
rounded - 390 Hz for a span that is really 390.625 - and the error would
accumulate into a visible misalignment over a long stitch; and measuring end to
end rather than between two adjacent bins divides the analyzer's own printed
rounding by 399. It is also the same spacing the frequency column of the CSV is
built from, so the overlapping points land exactly on saved data.

Because the spacing comes from the instrument, Fill needs a connection, and it
declines while the panel has a span change you have not applied yet - it would
otherwise measure the old span. Offline it falls back to the span table and says
in the log that the step is an estimate.

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
