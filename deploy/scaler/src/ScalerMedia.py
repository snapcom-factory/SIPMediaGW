#!/usr/bin/env python
import logging
import datetime as dt
import dateutil.parser as du
import redis

from Scaler import Scaler

logger = logging.getLogger(__name__)

# Each "gateway:*" Redis entry is a pipe-separated record laid out as below.
GATEWAY_IP = 0
GATEWAY_STATE = 1
GATEWAY_UPDATED_AT = 4
GATEWAY_FIELDS = 5


def _parseUpdatedAt(value):
    """
    Parse a gateway timestamp into a UTC-aware datetime, or None when unusable.

    Entries written before timestamps became timezone-aware carry local time, so
    a naive value is interpreted as local rather than as UTC.
    """
    try:
        parsed = du.parse(value)
    except (ValueError, OverflowError, TypeError) as exc:
        logger.warning("Ignoring unparsable gateway timestamp '%s': %s", value, exc)
        return None
    return parsed.astimezone(dt.timezone.utc)


# Media scaler using Redis to track room assignments and gateway states.
class ScalerMedia(Scaler):

    def configure(self, configFile):
        super().configure(configFile)
        self.redisClient = redis.Redis(host=self.config["redis"]["host"], port=self.config["redis"]["port"], decode_responses=True)

    def _gatewayParts(self, key):
        """
        Read a gateway entry and return its fields, padded to GATEWAY_FIELDS.

        Returns None when the key disappeared between the scan and the read,
        which happens routinely since both are separate Redis round-trips.
        """
        value = self.redisClient.get(key)
        if value is None:
            return None
        parts = value.split("|")
        if len(parts) < GATEWAY_FIELDS:
            parts += [""] * (GATEWAY_FIELDS - len(parts))
        return parts

    # Downscale function
    def downScale(self, numGW):
        ipList = []
        for key in self.redisClient.scan_iter(match="gateway:*"):
            if numGW <= 0:
                break
            parts = self._gatewayParts(key)
            if parts is None:
                continue
            gwIp = parts[GATEWAY_IP]
            if parts[GATEWAY_STATE] in ["started", "stopped"]:
                # No rooms assigned, can downscale
                ipList.append(gwIp)
                parts[GATEWAY_STATE] = "stopping"
                parts[GATEWAY_UPDATED_AT] = dt.datetime.now(dt.timezone.utc).isoformat()

                self.redisClient.set(key, "|".join(parts))
                numGW -= 1
        if ipList:
            logger.info("Downscaling gateways: %s", ipList)
            self.csp.destroyInstances(ipList)

    # Cleanup stale instances
    def cleanup(self):
        # Check for gateways in 'stopping' state for more than threshold time
        thresholdSeconds = self.config.get('cleanup_threshold_seconds', 600)
        now = dt.datetime.now(dt.timezone.utc)
        ipList = []
        for key in self.redisClient.scan_iter(match="gateway:*"):
            parts = self._gatewayParts(key)
            if parts is None:
                continue
            gwIp = parts[GATEWAY_IP]
            state = parts[GATEWAY_STATE]
            lastUpdateStr = parts[GATEWAY_UPDATED_AT]
            if state == "stopping" and lastUpdateStr:
                lastUpdate = _parseUpdatedAt(lastUpdateStr)
                if lastUpdate is None:
                    continue
                if (now - lastUpdate).total_seconds() > thresholdSeconds:
                    ipList.append(gwIp)
        if ipList:
            logger.info("Cleaning up stale gateways: %s", ipList)
            self.csp.destroyInstances(ipList)

        super().cleanup()

    # Get current available capacity
    def getCurrentCapacity(self):
        registeredGateways = 0
        for _ in self.redisClient.scan_iter(match="gateway:*"):
            registeredGateways += 1
        return registeredGateways

    def getPendingCapacity(self):
        """Gateways created at the provider that have not announced themselves yet."""
        knownIps = set()
        for key in self.redisClient.scan_iter(match="gateway:*"):
            parts = self._gatewayParts(key)
            if parts is not None and parts[GATEWAY_IP]:
                knownIps.add(parts[GATEWAY_IP])
        return self._countPendingInstances(knownIps)

    # Get Ready to run capacity
    def getReadyToRunCapacity(self):
        readyToRun = 0
        for key in self.redisClient.scan_iter(match="gateway:*"):
            parts = self._gatewayParts(key)
            if parts is not None and parts[GATEWAY_STATE] == "started":
                readyToRun += 1
        return readyToRun
