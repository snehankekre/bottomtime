---
title: How it works
description: How bottomtime archives, decodes, matches, and checks Shearwater and Garmin dive logs, from the files it reads to the app that shows them.
---

# How it works

bottomtime is a pipeline with one rule: the files a dive computer produced are
the record, and everything else is built from them and can be built again.

```
files you add, or that bottomtime fetches from Shearwater Cloud and Garmin Connect
   │
   ▼
archive/          every file, untouched, named by its SHA-256
   │  decode: pnf for Shearwater, Garmin's FIT SDK for Garmin
   ▼
dives.db          one row per log, its samples, and every record it couldn't read
   │  match: pair the logs of one dive, put them on one timeline, number the dives
   ▼
dives             each dive, and each log's offset from the dive's start
   │
   ├── verify     four checks against the manufacturers' numbers
   └── app, CLI   the dive list, the dive page, comparisons, surface GF, exports
```

The numbers on this page come from my logs: 144 from Shearwater computers
and 341 from a Garmin Descent, as of October 2026.

## The archive

Every file bottomtime reads is copied into `archive/` under a name made from
the SHA-256 hash of its contents, and recorded in the `raw_artifacts` table. A
file it already has gets the same name, so reading a folder again copies
nothing. A file's name also proves its contents haven't changed since it was
saved.

Shearwater Cloud complicates this. It keeps its logs inside its own database,
compressed, and re-compresses a log each time it exports or syncs, so the same
dive arrives as different bytes. bottomtime identifies a Shearwater dive by the
hash of the decompressed log, which stays the same. To read the live database
while Shearwater Cloud is running, the app takes a copy with SQLite's backup
function, which gives a consistent snapshot of a database another program is
writing to, and archives that copy.

Because the archive is the record, the database is disposable. `bottomtime
rebuild`, and the app's **Rebuild from saved files**, build a new database
from the archive: the Garmin files first, then each Shearwater Cloud copy from
oldest to newest, then matching. The new database replaces the old one only if
no file failed. Otherwise the old one stays and the new one is left beside it.
The database records its schema version, currently 5. A newer bottomtime
refuses to open an older database and asks for a rebuild, which the app runs
by itself after an update.

## Reading the logs

**Shearwater.** Shearwater computers store each dive in their own binary
format, which Shearwater's code calls the Petrel Native Format (PNF): a stream
of 32-byte records, one per sample plus configuration and summary records.
bottomtime decodes them with [pnf](https://github.com/snehankekre/pnf), a
small Python package I wrote and documented byte by byte in
[SPEC.md](https://github.com/snehankekre/pnf/blob/main/SPEC.md). Shearwater's
XML and CSV exports are made from the same logs, but they leave out GF99, the
deco ceiling, @+5, and battery voltage. Reading the native format keeps them.

**Garmin.** Descent computers record dives as FIT files. bottomtime reads them
with Garmin's official FIT SDK. Known dive values become columns, and every
other field, including Garmin's undocumented message types, is stored raw. The
app downloads the original FIT files from Garmin Connect with
[garminconnect](https://github.com/cyberjunky/python-garminconnect), which
signs in the way Garmin's own apps do. Garmin's official API needs approval
through Garmin's developer program, so this route can break when Garmin
changes something. Adding FIT files by hand always works.

Garmin Connect also holds copies of other brands' dives, made by its dive log
import tool. Their FIT files carry a manufacturer code other than Garmin's,
and bottomtime skips them.

**What isn't understood is kept.** Any record or message bottomtime can't
interpret goes into the `undecoded_payloads` table, byte for byte, with its
position in the log. My store holds 391,443 of them. That's how the
tissue-state records in Shearwater logs could be worked out later from logs
already in the store, with nothing to download again.

## Lining up two computers

A Shearwater's clock is local time with no time zone. A Garmin's clock is UTC,
and each dive records the UTC offset where it happened. The two clocks also
drift apart over months. So bottomtime can't match dives by start time alone.

Logs shorter than 2 minutes or shallower than 2 m are pool or test dives. They
are kept and shown, but never matched or numbered. Every other pair of logs
from two different computers that started within two days of each other is a
candidate:

1. **One clock.** For a Garmin and a Shearwater, the Shearwater log is tried
   at each UTC offset the Garmin recorded on dives within those two days, and
   at the offset the Garmin recorded most often, which covers a Shearwater dive
   with no Garmin dive near it. Two Shearwaters, or two Garmins, are compared
   on their own clocks, with the longer log as the reference.
2. **Overlap.** The two logs must overlap for at least half of the shorter
   one.
3. **Depth.** The shorter log can't be deeper than the longer one by more than
   5% plus 1 m.
4. **Shape.** Both depth profiles are resampled onto a 10-second grid, in
   memory only, and one slides along the other by up to five minutes each way
   in 10-second steps. Each position is scored with normalised
   cross-correlation, where 1 means the two profiles have the same shape. A
   best score of 0.95 or more accepts the pair.
5. **Pool sessions.** Flat profiles have too little shape to correlate, so a
   pair that fails on shape gets a second test. If the logs overlap for at
   least 85% of the shorter one and, at the best slide, their depths differ by
   a median of 0.75 m or less, two computers on the same body recorded them.

Then bottomtime picks links, highest score first. Computers split dives
differently. A Garmin keeps logging through a short surface interval that a
Shearwater ends a dive on, so one Garmin activity can link to several
Shearwater logs, as long as those logs don't overlap each other in time.
Apart from that, each log links to at most one log from any other computer.
When a log's best two
candidates from one computer score within 0.02 of each other, bottomtime links
neither, rather than guess. A link you confirm or reject in the store stays
that way the next time matching runs.

Each link stores the clock offset (the time zone, for a Garmin and a
Shearwater) and the residual skew, which is the extra slide the
cross-correlation found. The dive page shows the skew as "seconds left over".
Linked logs form one dive. The dive's start comes from its earliest Garmin
log, or from its longest log when it has no Garmin, and each log's offset from
that start is stored with it. A Shearwater log in a dive with a Garmin also
gets its UTC start time from the Garmin's time zone. Nothing is merged or
resampled. The app draws every sample at its own time plus that offset. Real
dives are numbered in date order.

On my logs, this makes 85 links: 84 between a Garmin and a Shearwater, and one
between a Perdix 2 and a Petrel 3 worn on the same dive. 72 passed on profile
shape and 13 on the pool-session test. The residual skew averages 35 seconds
and reaches 190 seconds, on a dive where the Perdix 2's clock and the Garmin's
were three minutes apart. The 485 logs make 400 dives, 319 numbered and 81
pool or test dives.

## Showing a dive

The dive page shows one card, one colour, and one column per computer. A
computer with several logs in one dive, such as a Shearwater that ended a dive
at each surface interval of a skills session, gets one card that says how many
logs it had and adds up their runtimes. Its logs are drawn as one line that
breaks where the computer wasn't logging.

A Shearwater ends a dive once it has been at the surface for its End Dive
Delay, 60 seconds unless you change it, so shallows or an air pocket can end a
dive early. When another computer logged the same dive, bottomtime looks at
what it read during each break. If it was still underwater, at a median of
0.5 m or more, the dive's page says so and points to the setting. On my dives
from 16 to 19 December 2025 the Perdix split single dives into two to six
logs, and during those breaks the Descent was at a median of 0.7 to 1.1 m.

The charts draw each computer's samples at their own times. The readout under
the pointer shows, for each computer, its sample nearest that moment, as long
as it's within 15 seconds or two sample intervals, whichever is longer. A
computer with nothing that close shows as "not recording".

## Comparing the computers

A dive computer measures pressure and turns it into depth using the water
density it's set to, so a computer set to lighter water reads deeper. Depth is
proportional to one over the density: a computer set to fresh water (1000
kg/m³) reads 1020/1000 − 1 = 2.0% deeper than one set to EN13319 (1020).
Shearwater's salt setting is 1030 kg/m³ and Garmin's is 1025, so the same word
on the two brands reads 0.5% apart.

For each dive two computers logged, bottomtime pairs their depth readings on
the shared timeline, keeping pairs taken within 1.5 seconds of each other with
both deeper than 5 m. Shallower than that, a fixed offset between the two
computers dominates the percentage. It needs at least 20 pairs. The median ratio is the
depth gap, and the ratio of the two density settings is the part the settings
explain. Both are on the dive's page, along with each computer's gradient
factors, minutes of required stops, and CNS at the end.

Of my dives, 58 were logged by two computers with enough readings below 5 m.
In every one, the two computers were set to different water densities. On 20
of them the Descent was on fresh water and the Perdix on EN13319, and those
differ by a median of 1.7%. The settings explain the whole gap, to within 0.25
points, on 22 of the 58. The median gap the settings don't explain is 0.32
points.

A density setting changes every reading by the same share, so after dividing
it out, the two computers should agree to within a few tens of centimetres all
dive. bottomtime measures how much the gap still moves around: it takes out
the median ratio, keeps readings where the depth was changing by 3 m a minute
or less, because during an ascent a second of clock difference looks like a
depth difference, and takes the middle half of what's left. On 52 of my 60
two-computer dives that spread is 0.48 m or less. The other eight are 0.62 m
or more, and the dive's page says the computers were at different depths for
parts of the dive.

The two dives on 25 December 2025, cave dives in Sulawesi, differ by 6%. The
gap wasn't steady. With the median taken out it still moved over about 1 m
across the middle half of the readings and reached 1.6 m. The Descent also
logs absolute pressure, and comparing that with the Perdix's depth turned
back into pressure gives the same answer without any density setting: on most
dives the two agree within 0.02 bar, and on these two the difference moves by
0.08 to 0.1 bar and reaches 0.27 bar, about 2.7 m. The computers were at
different depths through parts of those dives, by more than an arm's length.

## Surface GF

Surface GF is what GF99 would be if you surfaced at that moment. Shearwater
and Garmin computers both show it during a dive, and neither records it in
the log. bottomtime calculates it for a Shearwater log by rebuilding the
computer's tissue loading with the Bühlmann ZHL-16C model, the one gradient
factors are applied to:

1. The 16 nitrogen and 16 helium compartments start from the tissue state the
   Shearwater wrote into the log before its first sample. A log without one
   would start saturated with air at the surface pressure the computer logged,
   or carry over from the same computer's dives in the 48 hours before.
2. Every 10-second sample loads them with the gas the computer logged. On a
   rebreather the loop holds the logged ppO2, and the diluent's nitrogen and
   helium share the rest of the pressure.
3. Surface GF is the leading compartment's supersaturation at surface
   pressure, as a percentage of its M-value gradient there.

A Shearwater writes its tissue state into the log about every four minutes,
in six records numbered 0x70 to 0x75. Shearwater Cloud names them as tissue
records and doesn't read them. They hold 16 nitrogen and then 16 helium
pressures, in bar, as little-endian floats, followed by nine more numbers I
haven't identified and a timestamp. pnf's
[SPEC.md](https://github.com/snehankekre/pnf/blob/main/SPEC.md#71-tissue-loading-snapshots-0x700x75)
has the layout. Started from one snapshot, bottomtime's model lands on the
computer's next snapshot four minutes later with a median error of 0.0004 bar
across all 32 compartments. Taking the worst compartment in each four-minute
stretch, the median is 0.006 bar, 1,359 of the 1,372 stretches on my dives
stay under 0.05 bar, and the worst is 0.19 bar. Every one of the 13 misses
over 0.05 bar is in one of the two fastest compartments, whose half-times are
two to eight minutes, where 10-second samples are coarsest. So the tissue
loading matches the computer's.

The same tissue state also gives a GF99 at the current depth, which the
Shearwater logs, so every dive checks the calculation against the computer.
Over the 4,427 readings where my Shearwaters logged a GF99 of 5 or more,
bottomtime's GF99 was within a median 1.0 point of the computer's, 3.2 points
on 90% of readings, and 9.2 points on 99%. The worst reading was 32 points
off. On 77 of my 81 logs with such readings, the median gap was 2 points or
less. The dive page shows that gap for each dive in the chart's title.

Since the tissues match, the gap comes from GF99 itself. The computer logs
GF99 as a whole number, and the GF99 in a sample doesn't line up exactly with
the depth in the same sample: the snapshot tissues agree best with the GF99
logged about 30 seconds after the snapshot's timestamp. The calculation also
isn't Shearwater's code, so treat it as a close estimate.
It's left out under VPM-B and DCIEM, which don't use gradient factors.
bottomtime doesn't calculate it for Garmin logs yet. A Garmin doesn't log
GF99, so there would be nothing to check the calculation against.

## Tissues

The Tissues chart shows all 16 compartments through the dive, for a
Shearwater on gradient factors. Its values come from the snapshots the
computer wrote, with the same ZHL-16C model filling the four minutes between
them. The model starts again from each snapshot, so at every snapshot the
chart shows exactly what the computer recorded, and the model never drifts
for more than four minutes. Where it arrived at each snapshot is kept as a
check, and the dive's page gives the median distance.

The heatmap colours each compartment by its gradient: how far its inert gas
pressure is from the water pressure around you, as a percentage of the way to
its M-value, the same measure GF99 takes from the leading compartment. Below
zero the compartment is still taking on gas, and it's drawn blue. Above zero
it's giving off gas, from yellow through to red at its M-value.

The bars beside the charts show one moment. Each compartment's nitrogen and
helium are drawn as a share of its own M-value, the way a Shearwater draws its
tissue screen, so the bars stay readable from 100 m to the surface. The marks
on each bar are the water pressure around you and the limit your high
gradient factor sets. With the pointer off the charts, the bars show the
moment the leading compartment came closest to its M-value.

## Checking itself

`bottomtime verify` runs four checks against numbers bottomtime didn't
produce. If any check fails, it names the dive, the sample, and the value, and
exits with an error.

1. **The decoder against Shearwater's XML.** Shearwater Cloud's XML export is
   a separate rendering of the same native logs, made by Shearwater's own
   parser. For every dive with an XML export, the sample counts must agree to
   within 8, and every value both have, in every sample, must match what pnf
   decoded to the precision the XML is written in: depth to 0.05 m,
   temperature to 0.5 °C, ppO2 and battery voltage to 0.01, stop depth to
   0.1 m, and time to surface, NDL, stop time, and gas fractions exactly.
   On the 142 dives I exported, the largest difference is zero.
2. **GF99 against Shearwater Cloud.** Shearwater Cloud records an end-of-dive
   GF99 for each dive. The highest GF99 bottomtime decoded in the dive's last
   60 samples, ten minutes, must be within one point of it. It is, on all 144
   dives. The decoded deco ceiling must also never be deeper than the stop
   depth the computer showed, in any sample.
3. **Matched dives agree.** Every pair of linked logs, lined up with the
   offsets stored for the dive page, must show the same depth profile. Each
   reading from one log is paired with the other log's reading within 6
   seconds, and the median difference must be no more than 0.6 m, or 4.5% of the
   dive's maximum depth if that's larger, since water density settings make
   two computers read up to about 3% apart. On my 85 links, the median of
   those medians is 0.16 m and the largest is 0.84 m.
4. **The store is consistent.** Every log belongs to a dive, and the counts of
   logs, links, dives, and undecoded records are reported.

## The app

The app is a window around one HTML page, made with
[pywebview](https://pywebview.flowrl.com), which uses the system's own web
view: WebKit on a Mac and Microsoft Edge WebView2 on Windows. The page calls
Python methods on one object for everything it shows. `bottomtime app
--browser` serves the same page and methods over a local web server instead,
on a random port, with a random token for the session, so other programs on
the computer can't use it.

Everything the app keeps is in one folder: the archive, the database, the
Garmin sign-in token, and the settings. On a Mac that's
`~/Library/Application Support/bottomtime`, and on Windows
`%APPDATA%\bottomtime`. The app finds Shearwater Cloud's database where
Shearwater Cloud keeps it, inside its app container on a Mac and under
`AppData\LocalLow\Shearwater Research\Shearwater\users` on Windows. It reads
nothing until you've added dives yourself, and after that it can look for new
dives each time it opens.

**Updates.** Each time it opens, the app asks GitHub for its releases. A
regular release hears about regular releases, and a test build also hears
about test builds. When a newer version has a download for this computer, the
app downloads it in the background into a folder beside itself and unpacks
it. On a Mac it checks the new app's code signature, which fails on a
download that was cut short or altered. The new version replaces the old one
when you quit, or when you click **Restart now**. On Windows a program's
files can't be replaced while it runs, so a small script waits for the app to
exit, then moves in the new `bottomtime.exe` and `_internal` folder and
nothing else. A file the app downloads itself isn't marked as coming from the
internet, so neither macOS nor Windows asks you to approve the new version. On
a Mac, the app can update itself only from the Applications folder, because
macOS runs an app opened from Downloads from a read-only copy.

**Builds.** PyInstaller bundles the Python the build runs under, so each Mac
download is built separately, for Apple silicon with an Apple silicon Python
and for Intel with an Intel Python under Rosetta. The Mac zips leave out
macOS's hidden file labels. With them, Finder's unzip once wrote stray files
into the app, which broke its signature, and macOS called the app damaged. The
Windows app is built by GitHub Actions on Windows.

## What's been tested

I've validated the decoder on a Perdix 2 and on a Petrel 3 integrated with a
Choptima CCR, and the Garmin side on a Descent Mk3. Other Shearwater models
share the same log format, but a different model or firmware could fill a
field differently. bottomtime keeps every byte it doesn't understand, and
`verify` would flag a value that disagrees with Shearwater's own export. If
yours do, please [report it](https://github.com/snehankekre/bottomtime/issues)
with the dive attached.

The tests run on Linux and Windows on every change, and on macOS when I build
the Mac apps. On Windows, CI also builds the app on GitHub's Windows Server
2025 machines, opens its window to check that the page loads and talks to the
app, and has a copy update itself from the newest release and open again. A
separate check, run by hand, installs Shearwater Cloud's Windows app, signs
in, lets it sync, and reads every dive from the folder it keeps them in, which
on my account was 144 dives. The Garmin download worked there too, and
fetched all 341 of my dives.
