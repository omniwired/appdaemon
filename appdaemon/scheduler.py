import threading
import traceback
import datetime
from datetime import timedelta
import pytz
import astral
import random
import uuid
import time
import re
import asyncio
import logging
from collections import OrderedDict

import appdaemon.utils as utils
from appdaemon.appdaemon import AppDaemon


class Scheduler:

    def __init__(self, ad: AppDaemon):
        self.AD = ad

        self.logger = ad.logging.get_child("_scheduler")
        self.error = ad.logging.get_error()
        self.diag = ad.logging.get_diag()

        self.schedule = {}
        self.schedule_lock = threading.RLock()

        self.sun = {}
        self.sun_lock = threading.RLock()

        self.now = pytz.utc.localize(datetime.datetime.utcnow())

        #
        # If we were waiting for a timezone from metadata, we have it now.
        #
        tz = pytz.timezone(self.AD.time_zone)
        self.AD.tz = tz
        self.AD.logging.set_tz(tz)

        self.stopping = False
        self.realtime = True

        tt = self.set_start_time()

        if self.AD.endtime is not None:
            unaware_end = datetime.datetime.strptime(self.AD.starttime, "%Y-%m-%d %H:%M:%S")
            aware_end = self.AD.tz.localize(unaware_end)
            self.endtime = aware_end.astimezone(pytz.utc)
        else:
            self.endtime = None

        if tt is True:
            self.realtime = False
            self.logger.info("Starting time travel ...")
            self.logger.info("Setting clocks to %s", self.get_now_naive())
            if self.AD.tick == 0:
                self.logger.info("Time displacement factor infinite")
            else:
                self.logger.info("Time displacement factor %s", self.AD.interval / self.AD.tick)
        else:
            self.logger.info("Scheduler tick set to %ss", self.AD.tick)

        #

        self.AD.booted = self.get_now_naive()

        # Take a note of DST

        self.was_dst = self.is_dst()

        # Setup sun

        self.init_sun()

        self.update_sun()

    def set_start_time(self):
        tt = False
        if self.AD.starttime is not None:
            tt = True
            unaware_now = datetime.datetime.strptime(self.AD.starttime, "%Y-%m-%d %H:%M:%S")
            aware_now = self.AD.tz.localize(unaware_now)
            self.now = aware_now.astimezone(pytz.utc)
        else:
            self.now = pytz.utc.localize(datetime.datetime.utcnow())

        if self.AD.tick != self.AD.interval:
            tt = True

        return tt


    def stop(self):
        self.logger.debug("stop() called for scheduler")
        self.stopping = True

    def cancel_timer(self, name, handle):
        self.logger.debug("Canceling timer for %s", name)
        with self.schedule_lock:
            if name in self.schedule and handle in self.schedule[name]:
                del self.schedule[name][handle]
                self.AD.thread_async.call_async_no_wait(self.AD.state.remove_entity, "admin", "scheduler_callback.{}".format(handle))
            if name in self.schedule and self.schedule[name] == {}:
                del self.schedule[name]

    # noinspection PyBroadException
    async def exec_schedule(self, name, entry, args, uuid_):
        try:
            # Locking performed in calling function
            if "inactive" in args:
                return
            # Call function
            with self.AD.app_management.objects_lock:
                if "__entity" in args["kwargs"]:
                    await self.AD.threading.dispatch_worker(name, {
                        "id": uuid_,
                        "name": name,
                        "objectid": self.AD.app_management.objects[name]["id"],
                        "type": "state",
                        "function": args["callback"],
                        "attribute": args["kwargs"]["__attribute"],
                        "entity": args["kwargs"]["__entity"],
                        "new_state": args["kwargs"]["__new_state"],
                        "old_state": args["kwargs"]["__old_state"],
                        "pin_app": args["pin_app"],
                        "pin_thread": args["pin_thread"],
                        "kwargs": args["kwargs"],
                    })
                else:
                    await self.AD.threading.dispatch_worker(name, {
                        "id": uuid_,
                        "name": name,
                        "objectid": self.AD.app_management.objects[name]["id"],
                        "type": "scheduler",
                        "function": args["callback"],
                        "pin_app": args["pin_app"],
                        "pin_thread": args["pin_thread"],
                        "kwargs": args["kwargs"],
                    })
            # If it is a repeating entry, rewrite with new timestamp
            if args["repeat"]:
                if args["type"] == "next_rising" or args["type"] == "next_setting":
                    # It's sunrise or sunset - if the offset is negative we
                    # won't know the next rise or set time yet so mark as inactive
                    # So we can adjust with a scan at sun rise/set
                    if args["offset"] < 0:
                        args["inactive"] = 1
                    else:
                        # We have a valid time for the next sunrise/set so use it
                        c_offset = self.get_offset(args)
                        args["timestamp"] = self.sun[args["type"]] + timedelta(seconds=c_offset)
                        args["offset"] = c_offset
                else:
                    # Not sunrise or sunset so just increment
                    # the timestamp with the repeat interval
                    args["basetime"] += timedelta(seconds = args["interval"])
                    args["timestamp"] = args["basetime"] + timedelta(seconds=self.get_offset(args))
                # Update entity

                await self.AD.state.set_state("_scheduler", "admin", "scheduler_callback.{}".format(entry), execution_time = utils.dt_to_str(args["timestamp"].replace(microsecond=0), self.AD.tz))
            else:
                # Otherwise just delete
                await self.AD.state.remove_entity("admin", "scheduler_callback.{}".format(entry))

                del self.schedule[name][entry]

        except:
            error_logger = logging.getLogger("Error.{}".format(name))
            error_logger.warning('-' * 60)
            error_logger.warning("Unexpected error during exec_schedule() for App: %s", name)
            error_logger.warning("Args: %s", args)
            error_logger.warning('-' * 60)
            error_logger.warning(traceback.format_exc())
            error_logger.warning('-' * 60)
            if self.AD.logging.separate_error_log() is True:
                self.logger.warning("Logged an error to %s", self.AD.logging.get_filename("error_log"))
            error_logger.warning("Scheduler entry has been deleted")
            error_logger.warning('-' * 60)
            await self.AD.state.remove_entity("admin", "scheduler_callback.{}".format(entry))
            del self.schedule[name][entry]

    def process_sun(self, action):
        self.logger.debug("Process sun: %s, next sunrise: %s, next sunset: %s", action, self.sun["next_rising"], self.sun["next_setting"])
        with self.schedule_lock:
            for name in self.schedule.keys():
                for entry in sorted(
                        self.schedule[name].keys(),
                        key=lambda uuid_: self.schedule[name][uuid_]["timestamp"]
                ):
                    schedule = self.schedule[name][entry]
                    if schedule["type"] == action and "inactive" in schedule:
                        del schedule["inactive"]
                        c_offset = self.get_offset(schedule)
                        schedule["timestamp"] = self.sun[action] + timedelta(seconds=c_offset)
                        schedule["offset"] = c_offset

    def init_sun(self):
        latitude = self.AD.latitude
        longitude = self.AD.longitude

        if -90 > latitude < 90:
            raise ValueError("Latitude needs to be -90 .. 90")

        if -180 > longitude < 180:
            raise ValueError("Longitude needs to be -180 .. 180")

        elevation = self.AD.elevation

        self.location = astral.Location((
            '', '', latitude, longitude, self.AD.tz.zone, elevation
        ))

    def update_sun(self):

        mod = -1
        while True:
            try:
                next_rising_dt = self.location.sunrise(
                    (self.now + datetime.timedelta(days=mod)).date(), local=False
                )
                if next_rising_dt > self.now:
                    break
            except astral.AstralError:
                pass
            mod += 1

        mod = -1
        while True:
            try:
                next_setting_dt = self.location.sunset(
                    (self.now + datetime.timedelta(days=mod)).date(), local=False
                )
                if next_setting_dt > self.now:
                    break
            except astral.AstralError:
                pass
            mod += 1

        with self.sun_lock:
            old_next_rising_dt = self.sun.get("next_rising")
            old_next_setting_dt = self.sun.get("next_setting")
            self.sun["next_rising"] = next_rising_dt
            self.sun["next_setting"] = next_setting_dt

            if old_next_rising_dt is not None and old_next_rising_dt != self.sun["next_rising"]:
                # dump_schedule()
                self.process_sun("next_rising")
                # dump_schedule()
            if old_next_setting_dt is not None and old_next_setting_dt != self.sun["next_setting"]:
                # dump_schedule()
                self.process_sun("next_setting")
                # dump_schedule()

                self.logger.debug("Update sun: next sunrise: %s, next sunset: %s", self.sun["next_rising"], self.sun["next_setting"])


    def get_offset(self, kwargs):
        if "offset" in kwargs["kwargs"]:
            if "random_start" in kwargs["kwargs"] \
                    or "random_end" in kwargs["kwargs"]:
                raise ValueError(
                    "Can't specify offset as well as 'random_start' or "
                    "'random_end' in 'run_at_sunrise()' or 'run_at_sunset()'"
                )
            else:
                offset = kwargs["kwargs"]["offset"]
        else:
            rbefore = kwargs["kwargs"].get("random_start", 0)
            rafter = kwargs["kwargs"].get("random_end", 0)
            offset = random.randint(rbefore, rafter)
            self.logger.debug("sun: offset = %s", offset)
        return offset

    def insert_schedule(self, name, aware_dt, callback, repeat, type_, **kwargs):

        #aware_dt will include a timezone of some sort - convert to utc timezone
        utc = aware_dt.astimezone(pytz.utc)

        # Round to nearest tick

        utc = self.my_dt_round(utc, base=self.AD.tick)

        with self.AD.app_management.objects_lock:
            if "pin" in kwargs:
                pin_app = kwargs["pin"]
            else:
                pin_app = self.AD.app_management.objects[name]["pin_app"]

            if "pin_thread" in kwargs:
                pin_thread = kwargs["pin_thread"]
                pin_app = True
            else:
                pin_thread = self.AD.app_management.objects[name]["pin_thread"]

        with self.schedule_lock:
            if name not in self.schedule:
                self.schedule[name] = {}
            handle = uuid.uuid4().hex
            c_offset = self.get_offset({"kwargs": kwargs})
            ts = utc + timedelta(seconds=c_offset)
            interval = kwargs.get("interval", 0)

            with self.AD.app_management.objects_lock:
                self.schedule[name][handle] = {
                    "name": name,
                    "id": self.AD.app_management.objects[name]["id"],
                    "callback": callback,
                    "timestamp": ts,
                    "interval": interval,
                    "basetime": utc,
                    "repeat": repeat,
                    "offset": c_offset,
                    "type": type_,
                    "pin_app": pin_app,
                    "pin_thread": pin_thread,
                    "kwargs": kwargs
                }

        self.AD.thread_async.call_async_no_wait(self.AD.state.add_entity, "admin", "scheduler_callback.{}".format(handle), "active",
                                                                         {
                                                                             "app": name,
                                                                             "execution_time": utils.dt_to_str(ts.replace(microsecond=0), self.AD.tz),
                                                                             "repeat": str(datetime.timedelta(seconds=interval)),
                                                                             "function": callback.__name__,
                                                                             "pinned": pin_app,
                                                                             "pinned_thread": pin_thread,
                                                                             "fired": 0,
                                                                             "executed": 0,
                                                                             "kwargs": kwargs
                                                                         })
                # verbose_log(conf.logger, "INFO", conf.schedule[name][handle])

        return handle

    def terminate_app(self, name):
        with self.schedule_lock:
            if name in self.schedule:
                for id in self.schedule[name]:
                    self.AD.thread_async.call_async_no_wait(self.AD.state.remove_entity, "admin", "scheduler_callback.{}".format(id))
                del self.schedule[name]

    def is_realtime(self):
        return self.realtime

    #
    # Timer
    #
    async def do_every(self):
        #
        # We already set self.now for DST calculation and initial sunset,
        # but lets reset it at the start of the timer loop to avoid an initial clock skew
        #

        self.set_start_time()

        t = self.myround(self.get_now_ts(), base=self.AD.tick)
        count = 0
        t_ = self.myround(time.time(), base=self.AD.tick)
        while not self.stopping:
            count += 1
            delay = max(t_ + count * self.AD.tick - time.time(), 0)
            await asyncio.sleep(delay)
            t = self.myround(t + self.AD.interval, base=self.AD.tick)
            utc = datetime.datetime.fromtimestamp(t, pytz.utc)
            r = await self.do_every_tick(utc)
            if r is not None and r.timestamp() != t:
                t = r.timestamp()
                t_ = r.timestamp()
                count = 0


    #
    # Scheduler Loop
    #

    # noinspection PyBroadException,PyBroadException

    async def do_every_tick(self, utc):
        try:
            start_time = datetime.datetime.now().timestamp()
            self.now = utc

            # If we have reached endtime bail out

            if self.endtime is not None and self.now >= self.AD.endtime:
                self.logger.info("End time reached, exiting")
                if self.AD.stop_function is not None:
                    self.AD.stop_function()
                else:
                    #
                    # We aren't in a standalone environment so the best we can do is terminate the AppDaemon parts
                    #
                    self.stop()

            if self.realtime:
                real_now = pytz.utc.localize(datetime.datetime.utcnow())
                delta = abs((utc - real_now).total_seconds())
                if delta > self.AD.max_clock_skew:
                    self.logger.warning("Scheduler clock skew detected - delta = %s - resetting", delta)
                    return real_now

            # Update sunrise/sunset etc.

            self.update_sun()

            # Check if we have entered or exited DST - if so, reload apps
            # to ensure all time callbacks are recalculated

            now_dst = self.is_dst()
            if now_dst != self.was_dst:
                self.logger.info("INFO", "Detected change in DST from %s to %s - reloading all modules", self.was_dst, now_dst)

                self.logger.info("-" * 40)
                await utils.run_in_executor(self.AD.loop, self.AD.executor, self.AD.app_management.check_app_updates, "__ALL__")
            self.was_dst = now_dst

            # Process callbacks

            with self.schedule_lock:
                for name in self.schedule.keys():
                    for entry in sorted(
                            self.schedule[name].keys(),
                            key=lambda uuid_: self.schedule[name][uuid_]["timestamp"]
                    ):

                        if self.schedule[name][entry]["timestamp"] <= utc:
                            await self.exec_schedule(name, entry, self.schedule[name][entry], entry)
                        else:
                            break
                for k, v in list(self.schedule.items()):
                    if v == {}:
                        del self.schedule[k]

            end_time = datetime.datetime.now().timestamp()

            loop_duration = end_time - start_time
            self.logger.debug("Scheduler loop compute time: %ss", loop_duration)

            if self.realtime is True and loop_duration > self.AD.tick * 0.9:
                self.logger.warning("Excessive time spent in scheduler loop: %ss", loop_duration)

            return utc

        except:
            self.logger.warning('-' * 60)
            self.logger.warning("Unexpected error during do_every_tick()")
            self.logger.warning('-' * 60)
            self.logger.warning(traceback.format_exc())
            self.logger.warning('-' * 60)


    #
    # App API Calls
    #

    def sun_up(self):
        with self.sun_lock:
            return self.sun["next_rising"] > self.sun["next_setting"]

    def sun_down(self):
        with self.sun_lock:
            return self.sun["next_rising"] < self.sun["next_setting"]

    def info_timer(self, handle, name):
        with self.schedule_lock:
            if name in self.schedule and handle in self.schedule[name]:
                callback = self.schedule[name][handle]
                return (
                    self.make_naive(callback["timestamp"]),
                    callback["interval"],
                    self.sanitize_timer_kwargs(self.AD.app_management.objects[name]["object"], callback["kwargs"])
                )
            else:
                raise ValueError("Invalid handle: %s", handle)


    def get_scheduler_entries(self):
        schedule = {}
        with self.schedule_lock:
            for name in self.schedule.keys():
                schedule[name] = {}
                for entry in sorted(
                        self.schedule[name].keys(),
                        key=lambda uuid_: self.schedule[name][uuid_]["timestamp"]
                ):
                    schedule[name][str(entry)] = {}
                    schedule[name][str(entry)]["timestamp"] = str(self.AD.sched.make_naive(self.schedule[name][entry]["timestamp"]))
                    schedule[name][str(entry)]["type"] = self.schedule[name][entry]["type"]
                    schedule[name][str(entry)]["name"] = self.schedule[name][entry]["name"]
                    schedule[name][str(entry)]["basetime"] = str(self.AD.sched.make_naive(self.schedule[name][entry]["basetime"]))
                    schedule[name][str(entry)]["repeat"] = self.schedule[name][entry]["repeat"]
                    if self.schedule[name][entry]["type"] == "next_rising":
                        schedule[name][str(entry)]["interval"] = "sunrise:{}".format(utils.format_seconds(self.schedule[name][entry]["offset"]))
                    elif self.schedule[name][entry]["type"] == "next_setting":
                        schedule[name][str(entry)]["interval"] = "sunset:{}".format(utils.format_seconds(self.schedule[name][entry]["offset"]))
                    elif self.schedule[name][entry]["repeat"] is True:
                        schedule[name][str(entry)]["interval"] = utils.format_seconds(self.schedule[name][entry]["interval"])
                    else:
                        schedule[name][str(entry)]["interval"] = "None"

                    schedule[name][str(entry)]["offset"] = self.schedule[name][entry]["offset"]
                    schedule[name][str(entry)]["kwargs"] = ""
                    for kwarg in self.schedule[name][entry]["kwargs"]:
                        schedule[name][str(entry)]["kwargs"] = utils.get_kwargs(self.schedule[name][entry]["kwargs"])
                    schedule[name][str(entry)]["callback"] = self.schedule[name][entry]["callback"].__name__
                    schedule[name][str(entry)]["pin_thread"] = self.schedule[name][entry]["pin_thread"] if self.schedule[name][entry]["pin_thread"] != -1 else "None"
                    schedule[name][str(entry)]["pin_app"] = "True" if self.schedule[name][entry]["pin_app"] is True else "False"

        # Order it

        ordered_schedule = OrderedDict(sorted(schedule.items(), key=lambda x: x[0]))

        return ordered_schedule

    def is_dst(self):
        return self.now.astimezone(self.AD.tz).dst() != datetime.timedelta(0)

    def get_now(self):
        return self.now

    def get_now_ts(self):
        return self.now.timestamp()

    def get_now_naive(self):
        return self.make_naive(self.now)

    def now_is_between(self, start_time_str, end_time_str, name=None):
        start_time = self._parse_time(start_time_str, name)["datetime"]
        end_time = self._parse_time(end_time_str, name)["datetime"]
        now = self.get_now().astimezone(self.AD.tz)
        start_date = now.replace(
            hour=start_time.hour, minute=start_time.minute,
            second=start_time.second
        )
        end_date = now.replace(
            hour=end_time.hour, minute=end_time.minute, second=end_time.second
        )
        if end_date < start_date:
            # Spans midnight
            if now < start_date and now < end_date:
                now = now + datetime.timedelta(days=1)
            end_date = end_date + datetime.timedelta(days=1)
        return start_date <= now <= end_date

    def sunset(self, aware):
        if aware is True:
            return self.sun["next_setting"].astimezone(self.AD.tz)
        else:
            return self.make_naive(self.sun["next_setting"].astimezone(self.AD.tz))

    def sunrise(self, aware):
        if aware is True:
            return self.sun["next_rising"].astimezone(self.AD.tz)
        else:
            return self.make_naive(self.sun["next_rising"].astimezone(self.AD.tz))

    def parse_time(self, time_str, name=None, aware=False):
        if aware is True:
            return self._parse_time(time_str, name)["datetime"].astimezone(self.AD.tz).time()
        else:
            return self.make_naive(self._parse_time(time_str, name)["datetime"]).time()

    def parse_datetime(self, time_str, name=None, aware=False):
        if aware is True:
            return self._parse_time(time_str, name)["datetime"].astimezone(self.AD.tz)
        else:
            return self.make_naive(self._parse_time(time_str, name)["datetime"])


    def _parse_time(self, time_str, name=None):
        parsed_time = None
        sun = None
        offset = 0
        parts = re.search('^(\d+)-(\d+)-(\d+)\s+(\d+):(\d+):(\d+)$', time_str)
        if parts:
            this_time = datetime.datetime(int(parts.group(1)), int(parts.group(2)), int(parts.group(3)), int(parts.group(4)), int(parts.group(5)), int(parts.group(6)), 0)
            parsed_time = self.AD.tz.localize(this_time)
        else:
            parts = re.search('^(\d+):(\d+):(\d+)$', time_str)
            if parts:
                today = self.now.astimezone(self.AD.tz)
                time = datetime.time(
                    int(parts.group(1)), int(parts.group(2)), int(parts.group(3)), 0
                )
                parsed_time = today.replace(hour=time.hour, minute=time.minute, second=time.second, microsecond=0)

            else:
                if time_str == "sunrise":
                    parsed_time = self.sunrise(True)
                    sun = "sunrise"
                    offset = 0
                elif time_str == "sunset":
                    parsed_time = self.sunset(True)
                    sun = "sunset"
                    offset = 0
                else:
                    parts = re.search(
                        '^sunrise\s*([+-])\s*(\d+):(\d+):(\d+)$', time_str
                    )
                    if parts:
                        sun = "sunrise"
                        if parts.group(1) == "+":
                            td = datetime.timedelta(
                                hours=int(parts.group(2)), minutes=int(parts.group(3)),
                                seconds=int(parts.group(4))
                            )
                            offset = td.total_seconds()
                            parsed_time = (self.sunrise(True) + td)
                        else:
                            td = datetime.timedelta(
                                hours=int(parts.group(2)), minutes=int(parts.group(3)),
                                seconds=int(parts.group(4))
                            )
                            offset = td.total_seconds() * -1
                            parsed_time = (self.sunrise(True) - td)
                    else:
                        parts = re.search(
                            '^sunset\s*([+-])\s*(\d+):(\d+):(\d+)$', time_str
                        )
                        if parts:
                            sun = "sunset"
                            if parts.group(1) == "+":
                                td = datetime.timedelta(
                                    hours=int(parts.group(2)), minutes=int(parts.group(3)),
                                    seconds=int(parts.group(4))
                                )
                                offset = td.total_seconds()
                                parsed_time = (self.sunset(True) + td)
                            else:
                                td = datetime.timedelta(
                                    hours=int(parts.group(2)), minutes=int(parts.group(3)),
                                    seconds=int(parts.group(4))
                                )
                                offset = td.total_seconds() * -1
                                parsed_time = (self.sunset(True) - td)
        if parsed_time is None:
            if name is not None:
                raise ValueError(
                    "%s: invalid time string: %s", name, time_str)
            else:
                raise ValueError("invalid time string: %s", time_str)
        return {"datetime": parsed_time, "sun": sun, "offset": offset}

    #
    # Diagnostics
    #

    def dump_sun(self):
        self.diag.info("--------------------------------------------------")
        self.diag.info("Sun")
        self.diag.info("--------------------------------------------------")
        self.diag.info(self.sun)
        self.diag.info("--------------------------------------------------")

    def dump_schedule(self):
        if self.schedule == {}:
            self.diag.info("Scheduler Table is empty")
        else:
            self.diag.info("--------------------------------------------------")
            self.diag.info("Scheduler Table")
            self.diag.info("--------------------------------------------------")
            for name in self.schedule.keys():
                self.diag.info("%s:", name)
                for entry in sorted(
                        self.schedule[name].keys(),
                        key=lambda uuid_: self.schedule[name][uuid_]["timestamp"]
                ):
                    self.diag.info(" Next Event Time: %s - data: %s", self.make_naive(self.schedule[name][entry]["timestamp"]), self.schedule[name][entry])
            self.diag.info("--------------------------------------------------")

    #
    # Utilities
    #

    def sanitize_timer_kwargs(self, app, kwargs):
        kwargs_copy = kwargs.copy()
        return utils._sanitize_kwargs(kwargs_copy, [
            "interval", "constrain_days", "constrain_input_boolean", "_pin_app", "_pin_thread"
        ] + app.list_constraints())


    def myround(self, x, base=1, prec=10):
        if base == 0:
            return x
        else:
            return round(base * round(float(x) / base), prec)

    def my_dt_round(self, dt, base=1, prec=10):
        if base == 0:
            return dt
        else:
            ts = dt.timestamp()
            rounded = round(base * round(float(ts) / base), prec)
            result = datetime.datetime.utcfromtimestamp(rounded)
            aware_result = pytz.utc.localize(result)
            return aware_result


    def convert_naive(self, dt):
        # Is it naive?
        result = None
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            #Localize with the configured timezone
            result = self.AD.tz.localize(dt)
        else:
            result = dt

        return result

    def make_naive(self, dt):
        local = dt.astimezone(self.AD.tz)
        return datetime.datetime(local.year, local.month, local.day,local.hour, local.minute, local.second, local.microsecond)
