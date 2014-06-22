#
# INTEL CONFIDENTIAL
#
# Copyright 2013-2014 Intel Corporation All Rights Reserved.
#
# The source code contained or described herein and all documents related
# to the source code ("Material") are owned by Intel Corporation or its
# suppliers or licensors. Title to the Material remains with Intel Corporation
# or its suppliers and licensors. The Material contains trade secrets and
# proprietary and confidential information of Intel or its suppliers and
# licensors. The Material is protected by worldwide copyright and trade secret
# laws and treaty provisions. No part of the Material may be used, copied,
# reproduced, modified, published, uploaded, posted, transmitted, distributed,
# or disclosed in any way without Intel's prior express written permission.
#
# No license under any patent, copyright, trade secret or other intellectual
# property right is granted to or conferred upon you by disclosure or delivery
# of the Materials, either expressly, by implication, inducement, estoppel or
# otherwise. Any license under such intellectual property rights must be
# express and approved by Intel in writing.


import os
import re
import subprocess
from tempfile import mktemp
from collections import defaultdict

from chroma_agent.utils import Mounts, BlkId
from chroma_agent import shell
import chroma_agent.lib.normalize_device_path as ndp


class LocalTargets():
    '''
    Allows local targets to be examined. Not the targets are only examined once with the results cached. Detecting change
    therefore requires a new instance to be created and queried.
    '''

    def __init__(self):
        self.targets = self._get_targets()

    def _get_targets(self):
        # Working set: accumulate device paths for each (uuid, name).  This is
        # necessary because in multipathed environments we will see the same
        # lustre target on more than one block device.  The reason we use name
        # as well as UUID is that two logical targets can have the same UUID
        # when we see a combined MGS+MDT
        uuid_name_to_target = {}

        blkid_devices = BlkId()

        for blkid_device in blkid_devices.itervalues():
            dev = ndp.normalized_device_path(blkid_device['path'])

            # If a more normalized block device exists, then use that. Sometimes the normalized path
            # isn't a block device in which case we can't use it.
            try:
                blkid_device = blkid_devices[dev]
            except KeyError:
                pass

            rc, tunefs_text, stderr = shell.run(["tunefs.lustre", "--dryrun", dev])
            if rc != 0:
                # Not lustre
                continue

            # For a Lustre block device, extract name and params
            # ==================================================
            name = re.search("Target:\\s+(.*)\n", tunefs_text).group(1)
            flags = int(re.search("Flags:\\s+(0x[a-fA-F0-9]+)\n", tunefs_text).group(1), 16)
            params_re = re.search("Parameters:\\ ([^\n]+)\n", tunefs_text)
            if params_re:
                # Dictionary of parameter name to list of instance values
                params = {}
                # FIXME: naive parse: can these lines be quoted/escaped/have spaces?
                for param, value in [t.split('=') for t in params_re.group(1).split()]:
                    if not param in params:
                        params[param] = []
                    params[param].append(value)
            else:
                params = {}

            if name.find("ffff") != -1:
                # Do not report unregistered lustre targets
                continue

            mounted = ndp.normalized_device_path(blkid_device['path']) in set([ndp.normalized_device_path(path) for path, _, _ in Mounts().all()])

            if flags & 0x0005 == 0x0005:
                # For combined MGS/MDT volumes, synthesise an 'MGS'
                names = ["MGS", name]
            else:
                names = [name]

            for name in names:
                try:
                    target_dict = uuid_name_to_target[(blkid_device['uuid'], name)]
                    target_dict['devices'].append(dev)
                except KeyError:
                    target_dict = {"name": name,
                                   "uuid": blkid_device['uuid'],
                                   "params": params,
                                   "devices": [dev],
                                   "mounted": mounted}
                    uuid_name_to_target[(blkid_device['uuid'], name)] = target_dict

        return uuid_name_to_target.values()


class MgsTargets(object):
    TARGET_NAME_REGEX = "([\w-]+)-(MDT|OST)\w+"

    def __init__(self, local_targets):
        super(MgsTargets, self).__init__()
        self.filesystems = defaultdict(lambda: [])
        self.conf_params = defaultdict(lambda: defaultdict(lambda: {}))

        self._get_targets(local_targets)

    def _get_targets(self, local_targets):
        """If there is an MGS in the local targets, use debugfs to
           get a list of targets.  Return a dict of filesystem->(list of targets)"""

        mgs_target = None

        for t in local_targets:
            if t["name"] == "MGS" and t['mounted']:
                mgs_target = t

        if not mgs_target:
            return

        dev = mgs_target["devices"][0]

        ls = shell.try_run(["debugfs", "-c", "-R", "ls -l CONFIGS/", dev])
        filesystems = []
        targets = []
        for line in ls.split("\n"):
            try:
                name = line.split()[8]

                match = re.search("([\w-]+)-client", name)
                if match is not None:
                    filesystems.append(match.group(1).__str__())

                match = re.search(self.TARGET_NAME_REGEX, name)
                if match is not None:
                    targets.append(match.group(0).__str__())
            except IndexError:
                pass

        # Read config log "<fsname>-client" for each filesystem
        for fs in filesystems:
            self._read_log("filesystem", fs, "%s-client" % fs, dev)
            self._read_log("filesystem", fs, "%s-param" % fs, dev)

        # Read config logs "testfs-MDT0000" etc
        for target in targets:
            self._read_log("target", target, target, dev)

    def _read_log(self, conf_param_type, conf_param_name, log_name, dev):
        # NB: would use NamedTemporaryFile if we didn't support python 2.4
        """
        Uses debugfs to parse information about the filesystem on a device. Return any mgs info
        and config parameters about that device.

        :param conf_param_type: The type of configuration parameter to store
        :type conf_param_type: str
        :param conf_param_name: The name of the configuration parameter to store
        :type conf_param_name: dict
        :param log_name: The log name to dump the information about dev into
        :type log_name: str
        :param dev: The dev[vice] to parse for log information
        :type dev: str

        Returns: MgsTargetInfo containing targets and conf found.
        """

        tmpfile = mktemp()

        try:
            shell.try_run(["debugfs", "-c", "-R", "dump CONFIGS/%s %s" % (log_name, tmpfile), dev])
            if not os.path.exists(tmpfile) or os.path.getsize(tmpfile) == 0:
                # debugfs returns 0 whether it succeeds or not, find out whether
                # dump worked by looking for output file of some length. (LU-632)
                return

            client_log = subprocess.Popen(["llog_reader", tmpfile], stdout=subprocess.PIPE).stdout.read()

            entries = client_log.split("\n#")[1:]
            for entry in entries:
                tokens = entry.split()
                # ([\w=]+) covers all possible token[0] from
                # lustre/utils/llog_reader.c @ 0f8dca08a4f68cba82c2c822998ecc309d3b7aaf
                (code, action) = re.search("^\\((\d+)\\)([\w=]+)$", tokens[1]).groups()
                if conf_param_type == 'filesystem' and action == 'setup':
                    # e.g. entry="#09 (144)setup     0:flintfs-MDT0000-mdc  1:flintfs-MDT0000_UUID  2:192.168.122.105@tcp"
                    label = re.search("0:([\w-]+)-\w+", tokens[2]).group(1)
                    fs_name = label.rsplit("-", 1)[0]
                    uuid = re.search("1:(.*)", tokens[3]).group(1)
                    nid = re.search("2:(.*)", tokens[4]).group(1)

                    self.filesystems[fs_name].append({
                        "uuid": uuid,
                        "name": label,
                        "nid": nid})
                elif action == "param" or (action == 'SKIP' and tokens[2] == 'param'):
                    if action == 'SKIP':
                        clear = True
                        tokens = tokens[1:]
                    else:
                        clear = False

                    # e.g. entry="#29 (112)param 0:flintfs-client  1:llite.max_cached_mb=247.9"
                    # has conf_param name "flintfs.llite.max_cached_mb"
                    object = tokens[2][2:]
                    if len(object) == 0:
                        # e.g. "0: 1:sys.at_max=1200" in an OST log: it is a systemwide
                        # setting
                        param_type = conf_param_type
                        param_name = conf_param_name
                    elif re.search(self.TARGET_NAME_REGEX, object):
                        # Identify target params
                        param_type = 'target'
                        param_name = re.search(self.TARGET_NAME_REGEX, object).group(0)
                    else:
                        # Fall through here for things like 0:testfs-llite, 0:testfs-clilov
                        param_type = conf_param_type
                        param_name = conf_param_name

                    if tokens[3][2:].find("=") != -1:
                        key, val = tokens[3][2:].split("=")
                    else:
                        key = tokens[3][2:]
                        val = True

                    if clear:
                        val = None

                    self.conf_params[param_type][param_name][key] = val
        finally:
            if os.path.exists(tmpfile):
                os.unlink(tmpfile)


def detect_scan():
    local_targets = LocalTargets()
    mgs_targets = MgsTargets(local_targets.targets)

    return {"local_targets": local_targets.targets,
            "mgs_targets": mgs_targets.filesystems,
            "mgs_conf_params": mgs_targets.conf_params}


ACTIONS = [detect_scan]
CAPABILITIES = []
