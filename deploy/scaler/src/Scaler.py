#!/usr/bin/env python
import math
import datetime as dt
import dateutil.parser as du
import json


def getSeconds(stringHMS):
   timedeltaObj = dt.datetime.strptime(stringHMS, "%H:%M:%S") - dt.datetime(1900,1,1)
   return timedeltaObj.total_seconds()

class Scaler:
    def __init__(self, cspObj):
        self.csp = cspObj

    def configure(self, configFile):
        f = open(configFile)
        self.config = json.load(f)
        f.close()

    # Upscale function
    def upScale(self, numGW):
        # Simplified behavior: create exactly numGW instances.
        numCPU = str(self.config['cpu_per_gw'])
        gigaRAM = str(self.config['ram_per_gw'])
        createTimeoutSeconds = int(self.config.get('create_timeout_seconds', 300))
        print(
            "[UPSCALE] requested_gw={} per_instance={}vCPU/{}G timeout={}s".format(
                numGW, numCPU, gigaRAM, createTimeoutSeconds
            ),
            flush=True,
        )
        if numGW <= 0:
            return
        if hasattr(self.csp, "createInstancesParallel"):
            print("[UPSCALE] count={} mode=parallel".format(numGW), flush=True)
            self.csp.createInstancesParallel(
                int(numGW),
                numCPU,
                gigaRAM,
                self.config['gw_name_prefix'],
                timeoutSeconds=createTimeoutSeconds
            )
        else:
            print("[UPSCALE] count={} mode=sequential".format(numGW), flush=True)
            for _ in range(int(numGW)):
                self.csp.createInstance(numCPU, gigaRAM, self.config['gw_name_prefix'])

    # Downscale function
    def downScale(self, numGW):
       pass

    # Cleanup stale instances
    def cleanup(self):
        instList = self.csp.enumerateInstances()
        runningCpuCount = 0
        if instList :
            for inst in instList:
                if inst in self.config['cleaner_blacklist']:
                    continue
                runningCpuCount+= inst['cpu_count']
                if not inst['addr']['pub'] and inst['addr']['priv']:
                    now = dt.datetime.now(dt.timezone.utc)
                    start = du.parse(inst['start'])
                    if (now-start).total_seconds() > 600:
                        self.csp.destroyInstances([inst['addr']['priv']])
        print('Number of running CPUs: {} \n'.format(runningCpuCount), flush=True)

    # Get current available capacity
    def getCurrentCapacity(self):
       pass

    # Get Ready to run capacity
    def getReadyToRunCapacity(self):
        pass

    def _resolveThresholdTimeLine(self):
        """Pick the right time-based schedule for today's day of the week."""
        raw = self.config['auto_scale_threshold']

        first_key = next(iter(raw))
        if ':' in first_key and isinstance(raw[first_key], dict) and 'maxGw' in raw[first_key]:
            return raw

        today = dt.datetime.now().strftime("%A").lower()
        for day_key, schedule in raw.items():
            if day_key == "default":
                continue
            days = [d.strip().lower() for d in day_key.split(',')]
            if today in days:
                print("[SCALE] day={} matched key='{}'".format(today, day_key), flush=True)
                return schedule

        print("[SCALE] day={} using default schedule".format(today), flush=True)
        return raw.get('default', raw)

    # Scaling logic based on current load and time of the day
    def scale(self, scaleTime=None, incallsNum=None):
        thresholdTimeLine = self._resolveThresholdTimeLine()
        if not scaleTime:
            scaleTime = dt.datetime.now().strftime("%H:%M:%S")
        th = min([ i for i in list(thresholdTimeLine.keys()) if i <= scaleTime],
                key=lambda x:abs(getSeconds(x)-getSeconds(scaleTime)))

        # Get current capacity and ready to run capacity
        currentCapacity = self.getCurrentCapacity()
        readyToRunNum  = self.getReadyToRunCapacity()


        inCallNum = incallsNum if incallsNum else (currentCapacity - readyToRunNum )
        minCapacity = thresholdTimeLine[th]['unlockedMin'] + inCallNum
        print(
            "[SCALE] slot={} now={} current={} ready={} incall={} unlockedMin={} loadMax={} maxGw={}".format(
                th,
                scaleTime,
                currentCapacity,
                readyToRunNum,
                inCallNum,
                thresholdTimeLine[th]['unlockedMin'],
                thresholdTimeLine[th]['loadMax'],
                thresholdTimeLine[th]['maxGw'],
            ),
            flush=True,
        )
        if readyToRunNum < thresholdTimeLine[th]['unlockedMin']:
            targetCapacity = min((currentCapacity + thresholdTimeLine[th]['unlockedMin']
                                  - readyToRunNum),
                                  thresholdTimeLine[th]['maxGw'])
            capacityIncrease = math.ceil(targetCapacity - currentCapacity)
            print(
                "[SCALE] phase=unlock target={} delta={}gw".format(
                    targetCapacity, capacityIncrease
                ),
                flush=True,
            )
            if capacityIncrease > 0:
                print(
                    "[UPSCALE] +{}gw ({} cpu) reason=unlock_min".format(
                        capacityIncrease,
                        math.ceil(capacityIncrease*self.config['cpu_per_gw'])
                    ),
                    flush=True,
                )
                self.upScale(capacityIncrease)
                currentCapacity = currentCapacity + capacityIncrease

        targetCapacity = min(thresholdTimeLine[th]['maxGw'],
                             max(minCapacity, inCallNum/thresholdTimeLine[th]['loadMax']))
        capacityIncrease = math.ceil(targetCapacity - currentCapacity)
        print(
            "[SCALE] phase=load target={} delta={}gw".format(
                targetCapacity, capacityIncrease
            ),
            flush=True,
        )

        if capacityIncrease > 0:
            # Upscale
            print(
                "[UPSCALE] +{}gw ({} cpu) reason=load".format(
                    capacityIncrease,
                    math.ceil(capacityIncrease*self.config['cpu_per_gw'])
                ),
                flush=True,
            )
            self.upScale(capacityIncrease)
        if capacityIncrease < 0:
            # Downscale
            print(
                "[DOWNSCALE] -{}gw reason=load".format(abs(capacityIncrease)),
                flush=True,
            )
            self.downScale(abs(capacityIncrease))
        return 0
