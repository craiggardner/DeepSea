#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Runner to remove and deploy OSDs
"""

from __future__ import absolute_import
from __future__ import print_function
import logging
import pprint
import time
# pylint: disable=import-error,3rd-party-module-not-gated,redefined-builtin
import salt.client
import salt.runner

log = logging.getLogger(__name__)


def help_():
    """
    Usage
    """
    usage = ('salt-run rebuild.node [TARGET...]\n'
             'salt-run rebuild.nodes [TARGET...]:\n\n'
             '    Removes and deploys OSDs\n'
             'salt-run rebuild.check [TARGET...]:\n\n'
             '    Checks available space\n'
             '\n\n')
    print(usage)
    return ""


def master_minion():
    """
    Load the master modules
    """
    __master_opts__ = salt.config.client_config("/etc/salt/master")
    __master_utils__ = salt.loader.utils(__master_opts__)
    __salt_master__ = salt.loader.minion_mods(
        __master_opts__, utils=__master_utils__)

    return __salt_master__["master.minion"]()


class Rebuild(object):
    """ Removes and deploys OSDs for a list of minions  """

    def __init__(self, *args):
        """ Sets clients, assigns minions """
        self.master_minion = master_minion()
        self.local = salt.client.LocalClient()
        self.runner = salt.runner.RunnerClient(__opts__)
        __master_opts__ = salt.config.client_config("/etc/salt/master")
        __master_opts__['quiet'] = True
        self.qrunner = salt.runner.RunnerClient(__master_opts__)
        self.minions = self._minions(*args)
        self.skipped = []

    def _minions(self, *args):
        """ Return expanded Salt target """
        minions = set()
        for arg in args:
            results = self.local.cmd(arg, 'grains.get', ['id'], tgt_type="compound")
            minions.update(results)
        log.info("minions: {}".format(list(minions)))
        return sorted(minions)

    def _osd_list(self, minion):
        """ Return the list of OSD IDs """
        osd_ret = self.local.cmd(minion, 'osd.list', [], tgt_type="compound")
        log.debug("osd.list returned: {}".format(osd_ret))
        if minion in osd_ret:
            return osd_ret[minion]
        return None

    def _validate_osd_df(self, osd_df):
        """ Validate osd.df output """
        if self.master_minion not in osd_df:
            log.error("salt {} osd.df failed".format(self.master_minion))
            return False

        if 'nodes' not in osd_df[self.master_minion]:
            log.error("Mangled output: missing 'nodes' from osd.df")
            return False

        if 'summary' not in osd_df[self.master_minion]:
            log.error("Mangled output: missing 'summary' from osd.df")
            return False

        if 'total_kb_avail' not in osd_df[self.master_minion]['summary']:
            log.error("Mangled output: missing 'total_kb_avail' from osd.df")
            return False
        return True

    def safe(self, osds):
        """ Check OSD used capacity does not exceed available cluster space """
        osd_df = self.local.cmd(self.master_minion, 'osd.df', [], tgt_type="compound")
        log.debug("osd_df: {}".format(pprint.pformat(osd_df)))
        if not self._validate_osd_df(osd_df):
            return False

        used = 0
        for osd in osd_df[self.master_minion]['nodes']:
            if str(osd['id']) in osds:
                used += osd['kb_used']

        available = osd_df[self.master_minion]['summary']['total_kb_avail']
        log.info("Used: {} KB  Available: {} KB".format(used, available))
        if used > available:
            log.critical("OSDs exceed available free space")
            return False
        return True

    def _busy_wait(self, osds):
        """ Wait until OSDs are safe to stop """
        delay = 3
        while True:
            stop_ret = self.qrunner.cmd('osd.ok_to_stop_osds', osds, kwarg={'human': False})
            log.debug("stop_ret: {}".format(stop_ret))
            if stop_ret:
                break
            print("OSDs cannot be stopped safely.... waiting")
            time.sleep(delay)
            if delay < 60:
                delay += 3

    def _disengaged(self):
        """ Return safety setting"""
        return self.qrunner.cmd('disengage.check')

    def _check_return(self, ret, minion):
        """ Check for failures """
        if isinstance(ret, str) or not all(ret.values()):
            log.error("Failed to remove OSD(s)... skipping {}".format(minion))
            self.skipped.append(minion)
            return True
        return False

    def _skipped_summary(self):
        """ Print summary of skipped minions """
        if self.skipped:
            print("The following minions were skipped:\n{}".format("\n".join(self.skipped)),
                  "\n\nResolve any issues and run\n",
                  "salt-run rebuild.nodes {}".format(" ".join(self.skipped)))

    def run(self, checkonly=False):
        """
        For each minion
           Retrieve the list of osds
           Verify that the storage minion is safe to rebuild
           Remove the OSDs
           Deploy the minion
        """
        if not self._disengaged() and not checkonly:
            log.error('Safety engaged...run "salt-run disengage.safety"')
            return ""

        for minion in self.minions:
            log.info("minion: {}".format(minion))
            osds = self._osd_list(minion)
            if osds:
                log.info("osds: {}".format(osds))
                if not self.safe(osds):
                    log.critical("Aborting...")
                    return ""
                if checkonly:
                    print("Sufficient space to rebuild minion {}".format(minion))
                    continue

                self._busy_wait(osds)
                replace_ret = self.runner.cmd('osd.remove', osds)
                log.debug("replace_ret: {}".format(replace_ret))

                if self._check_return(replace_ret, minion):
                    continue

            deploy_ret = self.runner.cmd('disks.deploy', kwarg={'target': minion})
            pprint.pprint(deploy_ret)
        self._skipped_summary()
        return ""


def node(*args):
    """ Rebuild the minions provided """
    if not args:
        help_()
        return ""

    rebuild = Rebuild(*args)
    rebuild.run()
    return ""


def check(*args):
    """ Check the space of the minions provided """
    if not args:
        help_()
        return ""

    rebuild = Rebuild(*args)
    rebuild.run(checkonly=True)
    return ""


__func_alias__ = {
                 'help_': 'help',
                 'nodes': 'node',
                 }
