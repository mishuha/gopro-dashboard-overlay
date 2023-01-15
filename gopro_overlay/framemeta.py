import bisect
import datetime
from datetime import timedelta
from pathlib import Path
from typing import Callable, List, Generator, MutableMapping

from gopro_overlay import timeseries_process
from gopro_overlay.entry import Entry
from gopro_overlay.ffmpeg import load_gpmd_from, MetaMeta
from gopro_overlay.gpmd import GoproMeta
from gopro_overlay.gpmd_calculate import timestamp_calculator_for_packet_type
from gopro_overlay.gpmd_visitors_cori import CORIVisitor, CORIComponentConverter
from gopro_overlay.gpmd_visitors_gps import GPS5EntryConverter, GPSVisitor
from gopro_overlay.gpmd_visitors_grav import GRAVisitor, GRAVComponentConverter
from gopro_overlay.gpmd_visitors_xyz import XYZVisitor, XYZComponentConverter
from gopro_overlay.timeunits import Timeunit, timeunits
from gopro_overlay.timing import PoorTimer


class View:
    def __init__(self, data, version):
        self.data = data
        self.version = version


class Window:

    def __init__(self, ts, duration: Timeunit, samples, key=lambda e: 1, missing=None):
        self.ts = ts
        self.duration = duration
        self.samples = samples
        self.tick = (duration / samples).align(timeunits(millis=100))
        self.key = key
        self.missing = missing

        self.last_time = None
        self.last_view = None
        self.cache = {}
        self.version = 0

    def view(self, at: Timeunit):

        if self.last_time is not None and abs(at - self.last_time) < self.tick:
            return self.last_view

        return self._view_recalc(at)

    def _view_recalc(self, at: Timeunit):

        at = at.align(timeunits(millis=100))

        start = at - self.duration / 2
        end = at + self.duration / 2

        current = start

        data = []

        while current < end:
            if current < self.ts.min or current > self.ts.max:
                data.append(self.missing)
            else:
                entry = self.cache[current] if current in self.cache else self.cache.setdefault(current, self.ts.get(current))
                value = self.key(entry)
                if value is not None:
                    data.append(value)
                else:
                    data.append(self.missing)
            current += self.tick

        self.version += 1
        self.last_time = at
        self.last_view = View(data, self.version)

        return self.last_view


class Stepper:

    def __init__(self, framemeta, step: Timeunit):
        self._framemeta = framemeta
        self._step = step

    def __len__(self):
        max_ms = self._framemeta.framelist[-1]
        steps = int(max_ms / self._step) + 1
        return steps

    def steps(self):
        end = self._framemeta.framelist[-1]
        running = timeunits(millis=0)
        while running <= end:
            yield running
            running += self._step


max_distance = timeunits(seconds=6)


class FrameMeta:
    def __init__(self):
        self.modified = False
        self.framelist: List[Timeunit] = []
        self.frames: MutableMapping[Timeunit, Entry] = {}

    def __len__(self):
        self.check_modified()
        return len(self.framelist)

    def stepper(self, step: Timeunit):
        self.check_modified()
        return Stepper(self, step)

    def add(self, at_time: Timeunit, entry):
        self.frames[at_time] = entry
        self.modified = True

    @property
    def min(self):
        self.check_modified()
        return self.framelist[0]

    @property
    def max(self):
        self.check_modified()
        return self.framelist[-1]

    @property
    def mid(self):
        return self.min + ((self.max - self.min) / 2)

    def _update(self):
        self.framelist = sorted(list(self.frames.keys()))
        self.modified = False

    def check_modified(self):
        if self.modified:
            self._update()

    def get(self, frame_time: Timeunit, interpolate=True) -> Entry:
        self.check_modified()

        if frame_time in self.frames:
            return self.frames[frame_time]

        if not interpolate:
            raise KeyError(f"Frame at {frame_time}ms not found")

        return self._get_interpolate(frame_time)

    def _get_interpolate(self, frame_time) -> Entry:

        if frame_time < self.min:
            print(f"Request for data at time {frame_time}, before start of metadata, returning first item")
            return self.frames[self.framelist[0]]

        if frame_time > self.max:
            print(f"Request for data at time {frame_time}, after end of metadata, returning last item")
            return self.frames[self.framelist[-1]]

        later_idx = bisect.bisect_left(self.framelist, frame_time)
        earlier_idx = later_idx - 1

        earlier_time = self.framelist[earlier_idx]
        earlier_item = self.frames[earlier_time]

        delta = frame_time - earlier_time

        if delta > max_distance:
            print(f"Closest item to wanted time {frame_time} is {delta} away")

        return earlier_item

    def items(self, step: timedelta = timedelta(seconds=0)):
        self.check_modified()

        last_dt = datetime.datetime(year=1900, month=1, day=1, tzinfo=datetime.timezone.utc)

        for pts in self.framelist:
            entry = self.frames[pts]
            entry_dt = entry.dt

            if entry_dt >= last_dt + step:
                last_dt = entry_dt

                yield entry

    def process_deltas(self, processor, skip=1):
        self.check_modified()
        diffs = list(zip(self.framelist, self.framelist[skip:]))

        for a, b in diffs:
            updates = processor(self.frames[a], self.frames[b], skip)
            if updates:
                self.frames[a].update(**updates)

    def process(self, processor):
        self.check_modified()
        for e in self.framelist:
            updates = processor(self.frames[e])
            if updates:
                self.frames[e].update(**updates)

    def duration(self):
        self.check_modified()
        return self.framelist[-1]


def gps_framemeta(meta: GoproMeta, units, metameta=None):
    frame_meta = FrameMeta()

    meta.accept(
        GPSVisitor(
            converter=GPS5EntryConverter(
                units,
                calculator=timestamp_calculator_for_packet_type(meta, metameta, "GPS5"),
                on_item=lambda c, e: frame_meta.add(c, e)
            ).convert
        )
    )

    return frame_meta


def accl_framemeta(meta, units, metameta=None):
    framemeta = FrameMeta()

    meta.accept(
        XYZVisitor(
            "ACCL",
            on_item=XYZComponentConverter(
                frame_calculator=timestamp_calculator_for_packet_type(meta, metameta, "ACCL"),
                units=units,
                on_item=lambda t, x: framemeta.add(t, x)
            ).convert
        )
    )

    kalman = timeseries_process.process_kalman_pp3("accl", lambda i: i.accl)
    framemeta.process(kalman)

    return framemeta


def grav_framemeta(meta, units, metameta=None):
    framemeta = FrameMeta()

    meta.accept(
        GRAVisitor(
            on_item=GRAVComponentConverter(
                frame_calculator=timestamp_calculator_for_packet_type(meta, metameta, "GRAV"),
                units=units,
                on_item=lambda t, x: framemeta.add(t, x)
            ).convert
        )
    )

    return framemeta


def cori_framemeta(meta, units, metameta=None):
    framemeta = FrameMeta()

    meta.accept(
        CORIVisitor(
            on_item=CORIComponentConverter(
                frame_calculator=timestamp_calculator_for_packet_type(meta, metameta, "CORI"),
                units=units,
                on_item=lambda t, x: framemeta.add(t, x)
            ).convert
        )
    )

    return framemeta


def merge_frame_meta(gps: FrameMeta, other: FrameMeta, update: Callable[[FrameMeta], dict]):
    if other:
        for item in gps.items():
            frame_time = item.timestamp
            closest_previous = other.get(timeunits(millis=frame_time.magnitude))
            item.update(**update(closest_previous))


def parse_gopro(gpmd_from, units, metameta: MetaMeta):
    with PoorTimer("parsing").timing():
        with PoorTimer("GPMD", 1).timing():
            gopro_meta = GoproMeta.parse(gpmd_from)

        with PoorTimer("extract GPS", 1).timing():
            gps_frame_meta = gps_framemeta(gopro_meta, units, metameta=metameta)

        with PoorTimer("extract ACCL", 1).timing():
            merge_frame_meta(
                gps_frame_meta,
                accl_framemeta(gopro_meta, units, metameta=metameta),
                lambda a: {"accl": a.accl}
            )

        with PoorTimer("extract GRAV", 1).timing():
            merge_frame_meta(
                gps_frame_meta,
                grav_framemeta(gopro_meta, units, metameta=metameta),
                lambda a: {"grav": a.grav}
            )

        with PoorTimer("extract CORI", 1).timing():
            merge_frame_meta(
                gps_frame_meta,
                cori_framemeta(gopro_meta, units, metameta=metameta),
                lambda a: {"cori": a.cori, "ori": a.ori}
            )

        return gps_frame_meta


def framemeta_from(filepath: Path, units, metameta: MetaMeta):
    gpmd_from = load_gpmd_from(filepath)
    return parse_gopro(gpmd_from, units, metameta)


def framemeta_from_datafile(datapath, units, metameta: MetaMeta):
    with open(datapath, "rb") as data:
        return parse_gopro(data.read(), units, metameta)
