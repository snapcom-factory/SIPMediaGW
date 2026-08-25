#!/usr/bin/env python
import json
import logging
import math
import time
import datetime as dt
import dateutil.parser as du

logger = logging.getLogger(__name__)

# An instance that never got a public address within this delay is considered
# broken and destroyed.
_STALE_INSTANCE_SECONDS = 600

_DEFAULT_CREATE_TIMEOUT = 300

# A single iteration asks the provider for its instance list several times, for
# the cleanup, the pending capacity and the orphan report. They all want the same
# snapshot, so it is fetched once and reused for a few seconds.
_INSTANCE_CACHE_TTL = 30


def getSeconds(stringHMS):
    timedeltaObj = dt.datetime.strptime(stringHMS, "%H:%M:%S") - dt.datetime(1900, 1, 1)
    return timedeltaObj.total_seconds()


class Scaler:
    def __init__(self, cspObj):
        self.csp = cspObj
        self._instanceCache = None
        self._instanceCacheAt = 0.0

    def configure(self, configFile):
        with open(configFile) as f:
            self.config = json.load(f)

    def _createTimeout(self):
        return int(self.config.get('create_timeout_seconds', _DEFAULT_CREATE_TIMEOUT))

    def _enumerateInstances(self):
        """Provider instance list, memoised for `_INSTANCE_CACHE_TTL` seconds."""
        now = time.monotonic()
        if (self._instanceCache is None
                or (now - self._instanceCacheAt) > _INSTANCE_CACHE_TTL):
            self._instanceCache = self.csp.enumerateInstances() or []
            self._instanceCacheAt = now
        return self._instanceCache

    @staticmethod
    def _instanceAge(inst, now):
        """Seconds since the instance was created, or None when unknown."""
        if not inst.get('start'):
            return None
        try:
            start = du.parse(inst['start'])
        except (ValueError, OverflowError, TypeError):
            return None
        if start.tzinfo is None:
            start = start.replace(tzinfo=dt.timezone.utc)
        return (now - start).total_seconds()

    def reconcile(self):
        """
        Let the provider settle what the previous iteration left in flight.

        Runs before anything else in an iteration, so the cleanup and the scaling
        decision both look at a fleet that is as settled as it can be.
        """
        self._instanceCache = None
        self.csp.reconcile(timeoutSeconds=self._createTimeout())

    # Upscale function
    def upScale(self, numGW):
        """Create `numGW` gateways and return how many were actually created."""
        if numGW <= 0:
            return 0

        numCPU = str(self.config['cpu_per_gw'])
        gigaRAM = str(self.config['ram_per_gw'])
        createTimeoutSeconds = int(self.config.get('create_timeout_seconds', 300))
        logger.info(
            "[UPSCALE] requested_gw=%s per_instance=%svCPU/%sG timeout=%ss",
            numGW, numCPU, gigaRAM, createTimeoutSeconds,
        )

        created = self.csp.createInstancesParallel(
            int(numGW),
            numCPU,
            gigaRAM,
            self.config['gw_name_prefix'],
            timeoutSeconds=createTimeoutSeconds
        )
        createdNum = len(created or [])
        if createdNum < numGW:
            logger.warning(
                "[UPSCALE] partial: requested=%s created=%s", numGW, createdNum
            )
        return createdNum

    # Downscale function
    def downScale(self, numGW):
       pass

    # Cleanup stale instances
    def cleanup(self):
        blacklist = set(self.config.get('cleaner_blacklist') or [])
        now = dt.datetime.now(dt.timezone.utc)
        runningCpuCount = 0
        for inst in self._enumerateInstances():
            addr = inst.get('addr') or {}
            if addr.get('priv') in blacklist or addr.get('pub') in blacklist:
                continue
            runningCpuCount += inst['cpu_count']
            if not addr.get('pub') and addr.get('priv'):
                age = self._instanceAge(inst, now)
                if age is not None and age > _STALE_INSTANCE_SECONDS:
                    logger.info(
                        "Destroying instance %s, still without public IP after %ss",
                        addr['priv'], _STALE_INSTANCE_SECONDS,
                    )
                    self.csp.destroyInstances([addr['priv']])
        logger.info("Number of running CPUs: %s", runningCpuCount)

    # Get current available capacity
    def getCurrentCapacity(self):
       pass

    # Get Ready to run capacity
    def getReadyToRunCapacity(self):
        pass

    def getPendingCapacity(self):
        """
        Capacity ordered but not usable yet. Zero unless the subclass knows better.
        """
        return 0

    def _countPendingInstances(self, knownIps):
        """
        Count the instances the provider has but the load source ignores.

        Creations no longer block, so a gateway that is still booting or has not
        registered yet has to be counted somewhere: without it the next iteration
        would see the same shortage and order the very same instances again.

        Instances older than the creation timeout are left out. They are not
        coming up any more, and keeping them in the count would stop the scaler
        from ever replacing them.
        """
        threshold = self._createTimeout()
        blacklist = set(self.config.get('cleaner_blacklist') or [])
        now = dt.datetime.now(dt.timezone.utc)
        pending = 0

        for inst in self._enumerateInstances():
            addr = inst.get('addr') or {}
            priv, pub = addr.get('priv'), addr.get('pub')
            if priv in knownIps or pub in knownIps:
                continue
            if priv in blacklist or pub in blacklist:
                continue
            age = self._instanceAge(inst, now)
            if age is None or age <= threshold:
                pending += 1

        return pending

    def _resolveThresholdTimeLine(self):
        """Pick the right time-based schedule for today's day of the week."""
        raw = self.config['auto_scale_threshold']

        # The schedule comes in two shapes: either time slots directly, or slots
        # grouped by day of the week. A first key that looks like a time slot
        # tells the flat shape apart from the grouped one.
        firstKey = next(iter(raw))
        if ':' in firstKey and isinstance(raw[firstKey], dict) and 'maxGw' in raw[firstKey]:
            return raw

        today = dt.datetime.now().strftime("%A").lower()
        for dayKey, schedule in raw.items():
            if dayKey == "default":
                continue
            days = [d.strip().lower() for d in dayKey.split(',')]
            if today in days:
                logger.info("[SCALE] day=%s matched key='%s'", today, dayKey)
                return schedule

        logger.info("[SCALE] day=%s using default schedule", today)
        return raw.get('default', raw)

    @staticmethod
    def _resolveSlot(thresholdTimeLine, scaleTime):
        """
        Return the schedule key in effect at `scaleTime`, i.e. the latest slot
        that has already started. Falls back to the last slot of the day when
        the schedule has no slot starting before `scaleTime`.
        """
        if not thresholdTimeLine:
            raise ValueError("auto_scale_threshold defines no time slot")

        started = [key for key in thresholdTimeLine if key <= scaleTime]
        if started:
            return max(started, key=getSeconds)

        # No slot started yet today: the one still in effect is the last of the
        # previous day, i.e. the latest key of the schedule.
        fallback = max(thresholdTimeLine, key=getSeconds)
        logger.info(
            "[SCALE] no slot before %s, falling back to '%s'", scaleTime, fallback
        )
        return fallback

    # Scaling logic based on current load and time of the day
    def scale(self, scaleTime=None, incallsNum=None):
        thresholdTimeLine = self._resolveThresholdTimeLine()
        if not scaleTime:
            scaleTime = dt.datetime.now().strftime("%H:%M:%S")
        th = self._resolveSlot(thresholdTimeLine, scaleTime)

        registeredCapacity = self.getCurrentCapacity()
        pendingCapacity = self.getPendingCapacity()
        # Instances already ordered count as capacity, otherwise every iteration
        # would order them again while they boot.
        currentCapacity = registeredCapacity + pendingCapacity
        readyToRunNum = self.getReadyToRunCapacity()

        unlockedMin = thresholdTimeLine[th]['unlockedMin']
        loadMax = thresholdTimeLine[th]['loadMax']
        maxGw = thresholdTimeLine[th]['maxGw']

        # A booting gateway takes no call, so the load is measured on the
        # registered ones only, and diluted over the capacity on its way.
        inCallNum = incallsNum if incallsNum else (registeredCapacity - readyToRunNum)
        loadRatio = (inCallNum / currentCapacity) if currentCapacity > 0 else 0.0

        logger.info(
            "[SCALE] slot=%s now=%s current=%s registered=%s pending=%s ready=%s "
            "incall=%s load=%.0f%% unlockedMin=%s loadMax=%s maxGw=%s",
            th, scaleTime, currentCapacity, registeredCapacity, pendingCapacity,
            readyToRunNum, inCallNum, loadRatio * 100, unlockedMin, loadMax, maxGw,
        )

        orderedNum = 0

        # Phase 1: ensure base provisioned capacity (unlockedMin is a floor)
        if currentCapacity < unlockedMin:
            floorTarget = min(unlockedMin, maxGw)
            capacityIncrease = math.ceil(floorTarget - currentCapacity)
            logger.info(
                "[SCALE] phase=floor target=%s delta=+%sgw", floorTarget, capacityIncrease
            )
            if capacityIncrease > 0:
                orderedNum = self.upScale(capacityIncrease)
                currentCapacity += orderedNum

        # Phase 2: scale up if load exceeds loadMax threshold
        if currentCapacity > 0 and loadRatio > loadMax:
            loadTarget = min(maxGw, math.ceil(inCallNum / loadMax))
            capacityIncrease = math.ceil(loadTarget - currentCapacity)
            logger.info(
                "[SCALE] phase=load target=%s delta=+%sgw", loadTarget, capacityIncrease
            )
            if capacityIncrease > 0:
                justOrdered = self.upScale(capacityIncrease)
                orderedNum += justOrdered
                currentCapacity += justOrdered

        # Phase 3: scale down if over-provisioned (never below unlockedMin).
        # Held back while the fleet is still converging: releasing a registered
        # gateway now would only be undone by the instance about to register.
        if pendingCapacity or orderedNum:
            logger.info(
                "[SCALE] phase=downscale skipped, %s instance(s) still coming up",
                pendingCapacity + orderedNum,
            )
            return 0

        if inCallNum > 0:
            sustainTarget = max(unlockedMin, math.ceil(inCallNum / loadMax))
        else:
            sustainTarget = unlockedMin
        sustainTarget = min(sustainTarget, maxGw)
        capacityDecrease = currentCapacity - sustainTarget
        if capacityDecrease > 0:
            logger.info(
                "[SCALE] phase=downscale target=%s delta=-%sgw",
                sustainTarget, capacityDecrease,
            )
            self.downScale(capacityDecrease)

        return 0
