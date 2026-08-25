#!/usr/bin/env python
import logging
import datetime as dt
import dateutil.parser as du
from contextlib import closing
import mysql.connector as mysqlcon
from Scaler import Scaler

logger = logging.getLogger(__name__)

# Gateways Kamailio currently knows about, whatever their state.
_SQL_REGISTERED_VMS = '''SELECT DISTINCT
                            SUBSTRING_INDEX(SUBSTRING_INDEX(received,'sip:',-1),':',1) AS vm
                        FROM location
                        WHERE username LIKE CONCAT('%',%s,'%')
                        ;'''


class ScalerSIP(Scaler):

    def _connect(self):
        """Open a connection to the Kamailio database, to be used as a context manager."""
        return closing(mysqlcon.connect(host=self.config['sip_db']['host'],
                                        database='kamailio',
                                        user='root',
                                        password=self.config['sip_db']['root_password']))

    # Downscale function
    def downScale(self, numGW):
        sqlLockedElsewhere = '''SELECT id
                        FROM location AS loc2
                        WHERE loc1.vm =
                            SUBSTRING_INDEX(SUBSTRING_INDEX(loc2.received,'sip:',-1),':',1) AND
                            loc2.locked = 1'''
        sqlInDialog = '''SELECT  callee_contact
                        FROM dialog
                        WHERE loc1.vm =
                            SUBSTRING_INDEX(SUBSTRING_INDEX(callee_contact,'alias=',-1),'~',1)'''
        ipList = []
        try:
            with self._connect() as con:
                with closing(con.cursor(dictionary=True)) as cursor:
                    cursor.execute('''SELECT vm, COUNT(username) as count
                                    FROM
                                        (SELECT *,
                                            SUBSTRING_INDEX(SUBSTRING_INDEX(received,'sip:',-1),':',1) AS vm
                                                FROM location) AS loc1
                                    WHERE
                                        loc1.locked = 0 AND
                                        loc1.username LIKE CONCAT('%',%s,'%') AND
                                        NOT EXISTS ('''+sqlLockedElsewhere+''') AND
                                        NOT EXISTS ('''+sqlInDialog+''')
                                    GROUP BY vm
                                    ORDER BY count DESC;''',(self.config['gw_name_prefix'],))
                    vmList = cursor.fetchall()

                    for vm in vmList:
                        if vm['count'] <= numGW:
                            cursor.execute('''UPDATE location SET locked = 1, to_stop = 1
                                            WHERE
                                                locked = 0 AND
                                                SUBSTRING_INDEX(SUBSTRING_INDEX(location.received,'sip:',-1),':',1)=%s''',
                                            (vm['vm'],))
                            if cursor.rowcount == vm['count']:
                                con.commit()
                                ipList.append(vm['vm'])
                                numGW -= vm['count']
                            else:
                                con.rollback()

        except mysqlcon.Error as err:
            logger.error("Mysql error while downscaling: %s", err)

        if ipList:
            self.csp.destroyInstances(ipList)

    # Cleanup stale instances
    def cleanup(self):
        registeredIps = set()
        vmList = []
        try:
            with self._connect() as con:
                with closing(con.cursor(dictionary=True)) as cursor:
                    cursor.execute('''SELECT SUBSTRING_INDEX(SUBSTRING_INDEX(received,'sip:',-1),':',1) AS vm
                                    FROM location
                                    WHERE
                                        username LIKE CONCAT('%',%s,'%') AND to_stop = 1
                                    GROUP BY vm;''',(self.config['gw_name_prefix'],))
                    vmList = cursor.fetchall()
                    cursor.execute(_SQL_REGISTERED_VMS, (self.config['gw_name_prefix'],))
                    for row in cursor.fetchall():
                        if row.get('vm'):
                            registeredIps.add(row['vm'])
        except mysqlcon.Error as err:
            logger.error("Mysql error while cleaning up: %s", err)
            return

        ipList = [vm['vm'] for vm in vmList]
        if ipList:
            self.csp.destroyInstances(ipList)

        self._reapOrphanInstances(registeredIps)
        super().cleanup()

    def getPendingCapacity(self):
        """Gateways created at the provider that Kamailio has not seen register yet."""
        try:
            with self._connect() as con:
                with closing(con.cursor(dictionary=True)) as cursor:
                    cursor.execute(_SQL_REGISTERED_VMS, (self.config['gw_name_prefix'],))
                    registeredIps = {
                        row['vm'] for row in cursor.fetchall() if row.get('vm')
                    }
        except mysqlcon.Error as err:
            # Without the registered set every instance would look pending, which
            # would wrongly hold back any scale-up, so report none.
            logger.error("Mysql error while reading registered gateways: %s", err)
            return 0

        return self._countPendingInstances(registeredIps)

    def _reapOrphanInstances(self, registeredIps):
        """
        Destroy managed instances that Kamailio has never registered, once they
        are older than cleanup_threshold_seconds.

        Nothing is deleted when Kamailio reports no gateway at all: an empty
        location table would otherwise make the whole fleet look orphaned, for
        instance while the registrar is restarting.
        """
        threshold = int(self.config.get('cleanup_threshold_seconds', 600))
        blacklist = set(self.config.get('cleaner_blacklist') or [])
        now = dt.datetime.now(dt.timezone.utc)
        instList = self._enumerateInstances()
        orphans = []

        for inst in instList:
            priv = (inst.get('addr') or {}).get('priv')
            pub = (inst.get('addr') or {}).get('pub')
            if priv in registeredIps or pub in registeredIps:
                continue
            if (priv and priv in blacklist) or (pub and pub in blacklist):
                continue
            age = self._instanceAge(inst, now)
            wouldDelete = age is not None and age > threshold
            orphans.append((priv, pub, age, wouldDelete))

        logger.info(
            "[ORPHAN] csp=%s kamailio=%s candidates=%s",
            len(instList), len(registeredIps), len(orphans),
        )
        for priv, pub, age, wouldDelete in orphans:
            logger.info(
                "[ORPHAN] would_delete=%s age=%ss priv=%s pub=%s",
                "yes" if wouldDelete else "no",
                int(age) if age is not None else "?",
                priv,
                pub,
            )

        stale = [priv or pub for priv, pub, _, wouldDelete in orphans if wouldDelete]
        stale = [ip for ip in stale if ip]
        if not stale:
            return
        if not registeredIps:
            logger.warning(
                "[ORPHAN] Kamailio reports no registered gateway, skipping deletion of %s",
                stale,
            )
            return

        logger.info("[ORPHAN] destroying stale unregistered instance(s): %s", stale)
        self.csp.destroyInstances(stale)
        self._instanceCache = None

    # Get current available capacity
    def getCurrentCapacity(self):
        try:
            with self._connect() as con:
                with closing(con.cursor()) as cursor:
                    cursor.execute('''SELECT COUNT(username) FROM location
                                    WHERE
                                        username LIKE CONCAT('%',%s,'%') AND
                                        to_stop = 0
                                        ;''',(self.config['gw_name_prefix'],))
                    contactList = cursor.fetchall()
                    currentCapacity = contactList[0][0]
                    return currentCapacity
        except mysqlcon.Error as err:
            logger.error("Mysql error while reading current capacity: %s", err)
            return 0

    # Get Ready to run capacity
    def getReadyToRunCapacity(self):
        try:
            with self._connect() as con:
                with closing(con.cursor()) as cursor:
                    cursor.execute('''SELECT COUNT(username) FROM location
                                    WHERE
                                        locked = 0 AND
                                        username LIKE CONCAT('%',%s,'%') AND
                                    NOT EXISTS (
                                        SELECT callee_contact
                                        FROM dialog
                                        WHERE callee_contact LIKE CONCAT('%',location.username,'%')
                                    );''',(self.config['gw_name_prefix'],))
                    contactList = cursor.fetchall()
                    readyToCallNum = contactList[0][0]
                    return readyToCallNum
        except mysqlcon.Error as err:
            logger.error("Mysql error while reading ready-to-run capacity: %s", err)
            return 0
