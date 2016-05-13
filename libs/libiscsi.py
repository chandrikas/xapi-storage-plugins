#!/usr/bin/env python

import uuid
import sqlite3
import os, sys
import time
import urlparse
import stat
from xapi.storage.common import call
from xapi.storage import log
import xapi.storage.libs.poolhelper
import xcp.environ
import XenAPI
import socket
import scsiutil
import xml.dom.minidom
import fcntl

DEFAULT_PORT = 3260
ISCSI_REFDIR = '/var/run/sr-ref'
RETRY_MAX = 20 # retries
RETRY_PERIOD = 1.0 # seconds
DEV_PATH_ROOT = '/dev/disk/by-id/scsi-'

# TODO: File locking logic can be factored out into a util lib
#Opens and locks a file, returns filehandle
def lock_file(dbg, filename, mode="a+"):
    try:
        f = open(filename, mode)
    except:
        raise xapi.storage.api.volume.Unimplemented(
                  "Couldn't open refcount file: %s" % filename)
    retries = 0
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except IOError as e:
            # raise on unrelated IOErrors
            if e.errno != errno.EAGAIN:
                raise

        if retries >= RETRY_MAX:
            raise xapi.storage.api.volume.Unimplemented(
                  "Couldn't lock refcount file: %s" % filename)
        time.sleep(RETRY_PERIOD)

    return f


#Unlocks and closes file
def unlock_file(dbg, filehandle):
    fcntl.flock(filehandle, fcntl.LOCK_UN)
    filehandle.close()


def queryLUN(dbg, path, id):
    vendor = scsiutil.getmanufacturer(dbg, path)
    serial = scsiutil.getserial(dbg, path)
    size = scsiutil.getsize(dbg, path)
    SCSIid = scsiutil.getSCSIid(dbg, path)
    return (id, vendor, serial, size, SCSIid)


# This function takes an ISCSI device and populate it with
# a dictionary of available LUNs on that target.
def discoverLuns(dbg, path):
    lunMap = []
    if os.path.exists(path):
        # FIXME: Don't display dom0 disks
        # dom0_disks = util.dom0_disks()
        for file in os.listdir(path):
            if file.find("LUN") != -1 and file.find("_") == -1:
                lun_path = os.path.join(path,file)
                # FIXME: Don't display dom0 disks
                #if os.path.realpath(vdi_path) in dom0_disks:
                #    util.SMlog("Hide dom0 boot disk LUN")
                #else:
                LUNid = file.replace("LUN","")
                lunMap.append(queryLUN(dbg, lun_path, LUNid))
    return lunMap


def parse_node_output(text):
    """helper function - parses the output of iscsiadm for discovery and
    get_node_records"""
    def dotrans(x):
        (rec,iqn) = x.split()
        (portal,tpgt) = rec.split(',')
        return (portal,tpgt,iqn)
    return map(dotrans,(filter(lambda x: x != '', text.split('\n'))))


def discoverIQN(dbg, keys, interfaceArray=["default"]):
    """Run iscsiadm in discovery mode to obtain a list of the 
    TargetIQNs available on the specified target and port. Returns
    a list of triples - the portal (ip:port), the tpgt (target portal
    group tag) and the target name"""

    #FIXME: Important: protect against resetting boot disks on the same 
    # target
        
    cmd_base = ["-t", "st", "-p", keys['target']]
    for interface in interfaceArray:
        cmd_base.append("-I")
        cmd_base.append(interface)
    cmd_disc = ["iscsiadm", "-m", "discovery"] + cmd_base
    cmd_discdb = ["iscsiadm", "-m", "discoverydb"] + cmd_base
    auth_args =  ["-n", "discovery.sendtargets.auth.authmethod", "-v", "CHAP",
                  "-n", "discovery.sendtargets.auth.username", "-v", keys['username'],
                  "-n", "discovery.sendtargets.auth.password", "-v", keys['password']]
    fail_msg = "Discovery failed. Check target settings and " \
               "username/password (if applicable)"
    try:    
        if keys['username'] != None:
            # Unfortunately older version of iscsiadm won't fail on new modes
            # it doesn't recognize (rc=0), so we have to test it out
            support_discdb = "discoverydb" in util.pread2(["iscsiadm", "-h"])
            if support_discdb:
                exn_on_failure(cmd_discdb + ["-o", "new"], fail_msg)
                exn_on_failure(cmd_discdb + ["-o", "update"] + auth_args, fail_msg)
                cmd = cmd_discdb + ["--discover"]
            else:
                cmd = cmd_disc + ["-X", keys['username'], "-x", keys['password']]
        else:
            cmd = cmd_disc
        stdout = call(dbg, cmd)
    except:
        raise xapi.storage.api.volume.Unimplemented(
            "Error logging into: %s" % keys['target'])

    return parse_node_output(stdout)


def set_chap_settings(dbg, portal, target, username, password):
    cmd = ["/usr/sbin/iscsiadm", "-m", "node", "-T", iqn, "--portal", 
           portal, "--op", "update", "-n", "node.session.auth.authmethod", 
           "-v", "CHAP"]
    output = call(dbg, cmd)
    log.debug("%s: output = %s" % (dbg, output))
    cmd = ["/usr/sbin/iscsiadm", "-m", "node", "-T", iqn, "--portal", 
           portal, "--op", "update", "-n", "node.session.auth.username", 
           "-v", username]
    output = call(dbg, cmd)
    log.debug("%s: output = %s" % (dbg, output))
    cmd = ["/usr/sbin/iscsiadm", "-m", "node", "-T", iqn, "--portal", 
           portal, "--op", "update", "-n", "node.session.auth.password", 
           "-v", password]
    output = call(dbg, cmd)
    log.debug("%s: output = %s" % (dbg, output))


def get_device_path(dbg, uri):
    keys = decomposeISCSIuri(dbg, uri)
    dev_path = DEV_PATH_ROOT + keys['scsiid']
    return dev_path


def login(dbg, ref_str, keys):
    iqn_map = discoverIQN(dbg, keys)
    output = iqn_map[0] 
    # FIXME: only take the first one returned. 
    # This might not always be the one we want.
    log.debug("%s: output = %s" % (dbg, output))
    portal = output[0]
    # FIXME: error handling

   # Provide authentication details if necessary
    if keys['username'] != None:
        set_chap_settings(dbg, portal, keys['target'], 
                          keys['username'], keys['password'])

    # Lock refcount file before login
    if not os.path.exists(ISCSI_REFDIR):
        os.mkdir(ISCSI_REFDIR)
    filename = os.path.join(ISCSI_REFDIR, keys['iqn'])

    f = lock_file(dbg, filename, "a+")

    current_sessions = listSessions(dbg)
    log.debug("%s: current iSCSI sessions are %s" % (dbg, current_sessions))
    sessionid = findMatchingSession(dbg, portal, keys['iqn'], current_sessions)
    if sessionid:
        # If there's an existing session, rescan it 
        # in case new LUNs have appeared in it
        log.debug("%s: rescanning session %d for %s on %s" % 
                   (dbg, sessionid, keys['iqn'], keys['target']))
        rescanSession(dbg, sessionid)
    else:
        # Otherwise, perform a fresh login
        cmd = ["/usr/sbin/iscsiadm", "-m", "node", "-T", keys['iqn'], 
               "--portal", portal, "-l"]
        output = call(dbg, cmd)
        log.debug("%s: output = %s" % (dbg, output))
        # FIXME: check for success

   # Increment refcount
    found = False
    for line in f.readlines():
        if line.find(ref_str) != -1:
            found = True
    if not found:
        f.write("%s\n" % ref_str)

    unlock_file(dbg, f)
    waitForDevice(dbg)

    # Return path to logged in target
    target_path = "/dev/iscsi/%s/%s" % (keys['iqn'], portal)
    return target_path


def logout(dbg, ref_str, iqn):
    filename = os.path.join(ISCSI_REFDIR, iqn)
    if not os.path.exists(filename):
        return 

    f = lock_file(dbg, filename, "r+")

    refcount = 0
    file_content = f.readlines()
    f.seek(0,0)
    for line in file_content:
        if line.find(ref_str) == -1:
            f.write(line)
            refcount += 1
    f.truncate()

    if not refcount:
        os.unlink(filename)
        cmd = ["/usr/sbin/iscsiadm", "-m", "node", "-T", iqn, "-u"]
        call(dbg, cmd)

    unlock_file(dbg, f)
        

def waitForDevice(dbg):
    # Wait for new device(s) to appear
    cmd = ["/usr/sbin/udevadm", "settle"]
    call(dbg, cmd)

    # FIXME: For some reason, udevadm settle isn't sufficient 
    # to ensure the device is present. Why not?
    time.sleep(10)


def listSessions(dbg):
    '''Return a list of (sessionid, portal, targetIQN) pairs 
       representing logged-in iSCSI sessions.'''
    cmd = ["/usr/sbin/iscsiadm", "-m", "session"]
    output = call(dbg, cmd, error=False)  
    # if there are none, this command exits with rc 21
    # e.g. "tcp: [1] 10.71.153.28:3260,1 iqn.2009-01.xenrt.test:iscsi6da966ca 
    # (non-flash)"
    return [tuple([int(x.split(' ')[1].strip('[]')), x.split(' ')[2], 
            x.split(' ')[3]]) for x in output.split('\n') if x <> '']


def findMatchingSession(dbg, new_target, iqn, sessions):
    for (sessionid, portal, targetiqn) in sessions:
        # FIXME: only match on target IP address and IQN for now 
        # (not target port number)
        if portal.split(',')[0] == new_target and targetiqn == iqn:
            return sessionid
    return None


def rescanSession(dbg, sessionid):
    cmd = ["/usr/sbin/iscsiadm", "-m", "session", "-r", str(sessionid), 
           "--rescan"]
    output = call(dbg, cmd)
    log.debug("%s: output = '%s'" % (dbg, output))
    # FIXME: check for success


def getDesiredInitiatorName(dbg):
    # FIXME: for now, get this from xapi. In future, xapi will 
    # write this to a file we can read from.
    inventory = xcp.environ.readInventory()
    session = XenAPI.xapi_local()
    session.xenapi.login_with_password("root", "")
    this_host = session.xenapi.host.get_by_uuid(
                inventory.get("INSTALLATION_UUID"))
    return session.xenapi.host.get_other_config(this_host)['iscsi_iqn']

def setInitiatorName(dbg, iqn):
    with open('/etc/iscsi/initiatorname.iscsi', "w") as fd:
        fd.write('InitiatorName=%s\n' % (iqn))

def getCurrentInitiatorName(dbg):
    with open('/etc/iscsi/initiatorname.iscsi', "r") as fd:
        lines = fd.readlines()
        for line in lines:
            if not line.strip().startswith("#") and "InitiatorName" in line:
               return line.split('=')[1].strip()


def restartISCSIDaemon(dbg):
    cmd = ["/usr/bin/systemctl", "restart", "iscsid"]
    call(dbg, cmd)

 
def isISCSIDaemonRunning(dbg):
    cmd = ["/usr/bin/systemctl", "status", "iscsid"]
    (stdout, stderr, rc) = call(dbg, cmd, error=False, simple=False)
    return rc == 0

 
def configureISCSIDaemon(dbg):
    # Find out what the user wants the IQN to be
    iqn = getDesiredInitiatorName(dbg)
 
    # Make that the IQN, if possible
    if not isISCSIDaemonRunning(dbg):
        setInitiatorName(dbg, iqn)
        restartISCSIDaemon(dbg)
    else:
        cur_iqn = getCurrentInitiatorName(dbg)
        if iqn != cur_iqn:
            if len(listSessions(dbg)) > 0:
                raise xapi.storage.api.volume.Unimplemented(
                      "Daemon running with sessions from IQN '%s', "
                      "desired IQN '%s'" % (cur_iqn, iqn))
            else:
                setInitiatorName(dbg, iqn)
                restartISCSIDaemon(dbg)


def decomposeISCSIuri(dbg, uri):
    if (uri.scheme != "iscsi"):
        raise xapi.storage.api.volume.SR_does_not_exist(
              "The SR URI is invalid; please use \
               iscsi://<target>/<targetIQN>/<scsiID>")

    keys = {
           'target': None,
           'iqn': None,
           'scsiid': None,
           'username': None,
           'password': None
    }

    if uri.netloc:  
    	keys['target'] = uri.netloc
    if uri.path and '/' in uri.path:
        tokens = uri.path.split("/")
        if tokens[1] != '':
            keys['iqn'] = tokens[1]
        if len(tokens) > 2 and tokens[2] != '': 
            keys['scsiid'] = tokens[2]

    # If there's authentication required, the target will be i
    # of the form 'username%password@12.34.56.78'
    atindex = keys['target'].find('@')
    if atindex >= 0:
        [keys['username'], keys['password']] = keys['target'][0:atindex].split('%')
        keys['target'] = keys['target'][atindex+1:]

    return keys


def zoneInLUN(dbg, uri):
    log.debug("%s: zoneInLUN uri=%s" % (dbg, uri))

    u = urlparse.urlparse(uri)
    if u.scheme == 'iscsi':
        log.debug("%s: u = %s" % (dbg, u))
        keys = decomposeISCSIuri(dbg, u)
        if not keys['target'] or not keys['iqn'] or not keys['scsiid']:
            raise xapi.storage.api.volume.SR_does_not_exist(
                  "The SR URI is invalid; please use \
                   iscsi://<target>/<targetIQN>/<lun>")
        log.debug("%s: target = '%s', iqn = '%s', scsiid = '%s'" % 
                  (dbg, keys['target'], keys['iqn'], keys['scsiid']))


        usechap = False
        if keys['username'] != None: 
            usechap = True

        configureISCSIDaemon(dbg)

        log.debug("%s: logging into %s on %s" % 
                  (dbg, keys['iqn'], keys['target']))
        login(dbg, uri, keys)

        dev_path = DEV_PATH_ROOT + keys['scsiid']
    else:
        # FIXME: raise some sort of exception
        raise xapi.storage.api.volume.Unimplemented(
            "Not an iSCSI LUN: %s" % uri)

    # Verify it's a block device
    if not stat.S_ISBLK(os.stat(dev_path).st_mode):
        raise xapi.storage.api.volume.Unimplemented(
            "Not a block device: %s" % dev_path)

    # Switch to 'noop' scheduler
    sched_file = "/sys/block/%s/queue/scheduler" % (
                 os.path.basename(os.readlink(dev_path)))
    with open(sched_file, "w") as fd:
        fd.write("noop\n")

    return dev_path


def zoneOutLUN(dbg, uri):
    log.debug("%s: zoneOutLUN uri=%s" % (dbg, uri))

    u = urlparse.urlparse(uri)
    log.debug("%s: u = %s" % (dbg, u))
    if u.scheme == 'iscsi':
        keys = decomposeISCSIuri(dbg, u)
        log.debug("%s: iqn = %s" % (dbg, keys['iqn']))

        logout(dbg, uri, keys['iqn'])


