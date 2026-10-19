---
title: How it works
description: How bottomtime reads Shearwater and Garmin logs, lines up two computers' logs of one dive, and checks itself against the manufacturers' own numbers.
---

# How it works

## Reading the logs

**Shearwater.** Shearwater computers store each dive in their own binary
format, which Shearwater's code calls the Petrel Native Format (PNF).
Shearwater Cloud keeps those logs inside its database, compressed, one per
dive. bottomtime decodes them with [pnf](https://github.com/snehankekre/pnf),
a small Python package I wrote and documented byte by byte in
[SPEC.md](https://github.com/snehankekre/pnf/blob/main/SPEC.md). Shearwater's
XML and CSV exports are made from the same logs, but they leave out GF99, the
deco ceiling, @+5, and battery voltage. Reading the native format keeps them.

The app reads Shearwater Cloud's live database through SQLite's backup
function, which takes a consistent copy even while Shearwater Cloud is
running, then archives that copy. Shearwater Cloud re-compresses a dive's log
each time it exports or syncs, so bottomtime recognises a dive it already has
by a hash of the decompressed log.

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

## Lining up two computers

A Shearwater's clock is local time with no time zone. A Garmin's clock is
UTC, and each dive records the UTC offset where it happened. The two clocks
also drift apart over months. So bottomtime can't match dives by start time
alone.

For every pair of logs from different computers that started within two days
of each other, bottomtime:

1. Puts both on one clock. A Shearwater log is shifted by each UTC offset the
   Garmin recorded on dives near it in time. Two Shearwaters, or two Garmins,
   are compared on their own clocks.
2. Requires the two logs to overlap for at least half of the shorter one.
3. Resamples both depth profiles onto a 10-second grid, in memory only, and
   slides one along the other by up to five minutes each way, scoring each
   position with normalised cross-correlation. A score of 1 means the two
   profiles have the same shape.
4. Accepts the pair if the best score is at least 0.95.

Pool sessions and other flat profiles have little shape to correlate, so they
get a second test. If the logs overlap for at least 85% of the shorter one
and, once lined up, their depths differ by a median of 0.75 m or less, two
computers on the same body recorded them.

Computers split dives differently. A Garmin keeps logging through a short
surface interval that a Shearwater ends a dive on, so one Garmin activity can
match two Shearwater logs. bottomtime allows that as long as those two logs
don't overlap each other in time. When one log scores within 0.02 of two
different partners, bottomtime links neither, rather than guess.

Each accepted link stores the clock offset (the time zone, for a Garmin and a
Shearwater) and the residual skew, which is the extra slide the
cross-correlation found. The dive page shows the skew as "seconds left over".
Linked logs form one dive, and each log's offset from the dive's start is
stored with it. Nothing is merged or resampled. The app draws every sample at
its own time plus that offset.

On my 144 Shearwater dives and 341 Garmin activities, this links 84
Garmin–Shearwater pairs and one Perdix–Petrel pair. Of the Garmin–Shearwater
pairs, 71 passed on profile shape and 13 on the pool-session depth test. The
residual skew averages 35 seconds and reaches 190 seconds, on a dive where
the Perdix 2's clock and the Garmin's were three minutes apart.

Dives shorter than 2 minutes or shallower than 2 m are marked as pool or test
dives, left unnumbered, and never matched. Real dives are numbered in date
order.

## Comparing the computers

A dive computer measures pressure and turns it into depth using the water
density it's set to, so a computer set to lighter water reads deeper. Depth is
proportional to one over the density: a computer set to fresh water (1000
kg/m³) reads 1020/1000 − 1 = 2.0% deeper than one set to EN13319 (1020).
Shearwater's salt setting is 1030 kg/m³ and Garmin's is 1025, so the same word
on the two brands reads 0.5% apart.

For each dive two computers logged, bottomtime pairs their depth readings on
the shared timeline, keeping pairs taken within 1.5 seconds of each other with
both deeper than 5 m. Shallower than that, a fixed offset between two wrists
dominates the percentage. It needs at least 20 pairs. The median ratio is the
depth gap, and the ratio of the two density settings is the part the settings
explain. Both are on the dive's page, along with each computer's gradient
factors, minutes of required stops, and CNS at the end.

Of my dives, 58 were logged by two computers with enough readings below 5 m.
In every one, the two computers were set to different water densities. On 20
of them the Descent was on fresh water and the Perdix on EN13319, and those
differ by a median of 1.7%. The settings explain the whole gap, to within 0.25
points, on 22 of the 58. The median gap the settings don't explain is 0.32
points. Two dives in December 2025 differ by 6%, with the Perdix a steady 0.6
to 0.8 m shallower than the Descent at 9 to 15 m, which looks like where each
computer was worn.

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
haven't identified and a timestamp. Started from one snapshot, bottomtime's
model lands within a median 0.01 bar of the computer's next snapshot four
minutes later, 0.03 bar on 90% of 1,343 such stretches on my dives, and 0.19
bar at worst. So the tissue loading matches the computer's.

The same tissue state also gives a GF99 at the current depth, which the
Shearwater logs, so every dive checks the calculation against the computer.
Over the 4,427 readings where my Shearwaters logged a GF99 of 5 or more,
bottomtime's GF99 was within a median 1.0 point of the computer's, 3.2 points
on 90% of readings, and 9.2 points on 99%. The worst reading was 32 points
off. On 77 of my 81 logs with such readings, the median gap was 2 points or
less. The dive page shows that gap for each dive in the chart's title.

Since the tissues match, the gap comes from GF99 itself. The computer logs
GF99 as a whole number, and the GF99 in a sample doesn't line up exactly with
the depth in the same sample: the computer's own snapshot tissues agree best
with the GF99 logged about 30 seconds after the snapshot's timestamp. The
calculation also isn't Shearwater's own code, so treat it as a close estimate.
It's left out under VPM-B and DCIEM, which don't use gradient factors.
bottomtime doesn't calculate it for Garmin logs yet. A Garmin doesn't log
GF99, so there would be nothing to check the calculation against.

## Checking itself

`bottomtime verify` runs four checks, and the numbers on this site come from
it.

1. **Decoder against Shearwater's XML.** Shearwater Cloud's XML export is a
   separate rendering of the same native logs. For every dive with an XML
   export, every shared value of every sample must match what pnf decoded, to
the precision the XML is written in:
   depth, temperature, time to surface, stop depth and time, NDL, gas, ppO2,
   battery voltage, and more. On the 142 dives I exported, they all match.
2. **GF99 against Shearwater Cloud.** Shearwater Cloud calculates an
   end-of-dive GF99 for each dive. The highest GF99 bottomtime decoded in the
   last ten minutes must be within one point of it. It is, on all 144 dives.
   The deco ceiling must also never be deeper than the stop depth the
   computer showed.
3. **Matched dives agree.** Every pair of linked logs, lined up with the
   stored offsets, must show the same depth profile.
4. **The store is consistent.** Counts add up and every reference points at
   a real row.

If a check fails, `verify` says which dive and which value, and exits with an
error.

## What's been tested

I've validated the decoder on a Perdix 2 and on a Petrel 3 integrated with a
Choptima CCR, and the Garmin side on a Descent Mk3. Other Shearwater models
share the same log format, but a different model or firmware could fill a
field differently. bottomtime keeps every byte it doesn't understand, and
`verify` would flag a value that disagrees with Shearwater's own export. If
yours do, please [report it](https://github.com/snehankekre/bottomtime/issues)
with the dive attached.

On Windows, CI builds the app on GitHub's Windows Server 2025 machines and
opens its window to check that the page loads and talks to the app. A
separate check installs Shearwater Cloud's Windows app, signs in, lets it
sync, and reads every dive from the folder it keeps them in, which on my
account was 144 dives. The Garmin download worked there too, and fetched all 341 of my dives.
The tests run on Windows, macOS, and Linux on every change.
