"""Wall-clock exhibition phases; independent of sensor/model monotonic time."""
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "Asia/Seoul")


class ExhibitionSchedule:
    def __init__(self, config):
        if config.get("timezone") != "Asia/Seoul":
            raise ValueError("exhibition timezone must be Asia/Seoul")
        try:
            hour, minute = (int(x) for x in config["anchor"].split(":"))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError("anchor must be HH:MM") from exc
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            raise ValueError("invalid exhibition anchor")
        for key in ("activate_minutes", "sleep_minutes"):
            if type(config.get(key)) is not int or not 1 <= config[key] <= 1440:
                raise ValueError(f"invalid {key}")
        self.hour, self.minute = hour, minute
        self.active = config["activate_minutes"]*60
        self.cycle = self.active+config["sleep_minutes"]*60
        # Every day has the same boundaries. No implicit closing/off state.
        if 86400 % self.cycle:
            raise ValueError("cycle must divide a day")

    def at(self, now=None):
        now = datetime.now(KST) if now is None else now
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("schedule requires a timezone-aware datetime")
        now = now.astimezone(KST)
        anchor = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if now < anchor:
            anchor -= timedelta(days=1)
        elapsed = (now-anchor).total_seconds()
        phase = elapsed % self.cycle
        active = phase < self.active
        remaining = (self.active if active else self.cycle)-phase
        return {"state": "activate" if active else "sleep", "mode": "scheduled",
                "timezone": "Asia/Seoul", "at": now.isoformat(),
                "anchor": anchor.isoformat(), "cycle_index": int(elapsed//self.cycle),
                "phase_seconds": phase, "remaining_seconds": remaining,
                "next_transition": (now+timedelta(seconds=remaining)).isoformat()}


class StateController:
    """Scheduled mode ignores stale manual files; explicit manual mode retains them."""
    def __init__(self, schedule, *, mode="scheduled", state_file=None, initial_state=None):
        if mode not in ("scheduled", "manual") or (mode == "scheduled" and initial_state):
            raise ValueError("fixed --state requires manual mode")
        self.schedule, self.mode, self.path = schedule, mode, state_file
        self.state = initial_state or "activate"
        if self.state not in ("activate", "sleep"):
            raise ValueError("invalid initial state")
        if mode == "manual":
            if state_file is None:
                raise ValueError("manual mode requires a state file")
            state_file.parent.mkdir(parents=True, exist_ok=True)
            if initial_state or not state_file.exists():
                state_file.write_text(self.state, encoding="utf-8")

    def at(self, now=None):
        if self.mode == "scheduled":
            return self.schedule.at(now)
        error = None
        try:
            state = self.path.read_text(encoding="utf-8-sig").strip()
            if state not in ("activate", "sleep"):
                raise ValueError("state file must contain activate or sleep")
            self.state = state
        except (OSError, ValueError) as exc:
            error = str(exc)
        return {"state": self.state, "mode": "manual", "next_transition": None,
                "state_file_error": error}
