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

On my dives, 61 matched pairs have enough readings below 5 m. In every one,
the two computers were set to different water densities. On 23 of them the
Descent was on fresh water and the Perdix on EN13319, and those differ by a
median of 1.7%. The settings explain the whole gap, to within 0.25 points, on
23 of the 61. The median gap the settings don't explain is 0.29 points, or
0.12 m. Two dives in December 2025 differ by 6%, with the Perdix a steady 0.6
to 0.8 m shallower than the Descent at 9 to 15 m, which looks like where each
computer was worn.

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
