#!/usr/bin/env python

import os
import os.path
import sys
import errno
import uuid
import urlparse
import xapi.storage.api.volume
from xapi.storage.common import call
from xapi.storage.libs import libvhd
from xapi.storage.libs import libiscsi
from xapi.storage import log
import fcntl
import json
import xcp.environ
import XenAPI

# For a block device /a/b/c, we will mount it at <mountpoint_root>/a/b/c
mountpoint_root = "/var/run/sr-mount/"

def getSRpath(dbg, sr, check=True):
    uri = urlparse.urlparse(sr)

    if uri.scheme == 'iscsi':
        (target, iqn, scsiid) = libiscsi.decomposeISCSIuri(dbg, uri)
        sr_path = "/dev/disk/by-id/scsi-%s" % scsiid
    else:
        sr_path = uri.path

    if check:
        if not(os.path.isdir(sr_path)) or not(os.path.ismount(sr_path)):
            raise xapi.storage.api.volume.Sr_not_attached(sr_path)
    return sr_path

def getVOLpath(dbg, sr_path, key, check=True):
    vol_dir  = os.path.join(sr_path, key)
    vol_path = os.path.join(vol_dir, key)
    if check:
        if not(os.path.exists(vol_path)):
            raise xapi.storage.api.volume.Volume_does_not_exist(vol_path)
    return vol_path

def getFromSRMetadata(dbg, sr, key):
    value = None
    u = urlparse.urlparse(sr)
    if u.scheme == 'file':
        # Get the device path
        metapath = "%s/meta.json" % (u.path)
        log.debug("%s: metapath = %s" % (dbg, metapath))
        if os.path.exists(metapath):
            with open(metapath, "r") as fd:
                meta = json.load(fd)
                value = meta[key]
    log.debug("%s: SR metadata says '%s' -> '%s'" % (dbg, key, value))
    return value

def sanitise_name(dbg, name):
    sanitised = ""
    for c in name:
        if c == os.sep or c in [
                "<",
                ">",
                ":",
                "\"",
                "/",
                "|",
                "?",
                "*"]:
            sanitised = sanitised + "_"
        else:
            sanitised = sanitised + c
    if sanitised == "":
        sanitised = "unknown"
    return sanitised

def mount(dbg, dev_path):
    # FIXME: Ensure corosync+dlm are configured and running

    mnt_path = os.path.abspath(mountpoint_root + dev_path)
    try:
        os.makedirs(mnt_path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(mnt_path):
            pass
        else:
            raise
    if not os.path.ismount(mnt_path):
        cmd = ["/usr/sbin/modprobe", "gfs2"]
        call(dbg, cmd)

        cmd = ["/usr/bin/mount", "-t", "gfs2", "-o",
               "noatime,nodiratime", dev_path, mnt_path]
        call(dbg, cmd)
    return mnt_path

def mount_local(dbg, dev_path):
    mnt_path = os.path.abspath(mountpoint_root + dev_path)
    try:
        os.makedirs(mnt_path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(mnt_path):
            pass
        else:
            raise
    if not os.path.ismount(mnt_path):
        cmd = ["/usr/bin/mount", "-t", "gfs2", "-o",
               "noatime,nodiratime,lockproto=lock_nolock", dev_path, mnt_path]
        call(dbg, cmd)
    return mnt_path

def umount(dbg, mnt_path):
    cmd = ["/usr/bin/umount", mnt_path]
    call(dbg, cmd)

def plug_device(dbg, uri):
    u = urlparse.urlparse(uri)     
    if u.scheme == 'iscsi':
        dev_path = libiscsi.zoneInLUN(dbg, uri)
    else: 
        # Assume it's a local block device
        dev_path = "/%s%s" % (u.netloc, u.path)
        # FIXME: Why do we need to check if it is a mount
        # if not(os.path.exists(dev_path)) or not(os.path.ismount(dev_path)):
        if not(os.path.exists(dev_path)):
            raise xapi.storage.api.volume.Sr_not_attached(dev_path)
    return dev_path

def unplug_device(dbg, uri): 
    u = urlparse.urlparse(uri)     

    if u.scheme == 'iscsi':
        libiscsi.zoneOutLUN(dbg, uri)
    else:
        #do nothing for now
        pass

class Implementation(xapi.storage.api.volume.SR_skeleton):

    def probe(self, dbg, uri):
        raise AssertionError("not implemented")

        # TODO: Complete implementation

        if iqn == None:
            targetMap = discoverIQN(dbg, target, usechap, username, password)
            print_iqn_entries(targetMap)
            # FIXME: Suppress backtrace in a better way
            sys.tracebacklimit=0
            raise xapi.storage.api.volume.Unimplemented(
                  "Uri is missing target IQN information: %s" % uri)

        if scsiid == None:
            target_path = "/dev/iscsi/%s/%s:3260" % (iqn, target)
            lunMap = discoverLuns(dbg, target_path)
            print_lun_entries(lunMap)
            # FIXME: Suppress backtrace in a better way
            sys.tracebacklimit=0
            raise xapi.storage.api.volume.Unimplemented(
                  "Uri is missing LUN information: %s" % uri)

    def attach(self, dbg, uri):
        log.debug("%s: SR.attach: uri=%s" % (dbg, uri))

        # Notify other pool members we have arrived
        inventory = xcp.environ.readInventory()
        session = XenAPI.xapi_local()
        session.xenapi.login_with_password("root", "")
        this_host = session.xenapi.host.get_by_uuid(
            inventory.get("INSTALLATION_UUID"))
        # FIXME: Do not notify offline hosts
        # FIXME: See ffs.call_plugin_in_pool()
        for host in session.xenapi.host.get_all():
            log.debug("%s: refresh host %s config file" % (dbg, session.xenapi.host.get_name_label(host)))
            session.xenapi.host.call_plugin(
                host, "gfs2setup", "gfs2UpdateConf", {})

        for host in session.xenapi.host.get_all():
            if host != this_host:
                log.debug("%s: setup host %s" % (dbg, session.xenapi.host.get_name_label(host)))
                session.xenapi.host.call_plugin(
                    host, "gfs2setup", "gfs2Reload", {})

        # this_host will reload last
        log.debug("%s: refresh host %s" % (dbg, session.xenapi.host.get_name_label(this_host)))
        session.xenapi.host.call_plugin(
            this_host, "gfs2setup", "gfs2Reload", {})

        # Zone in the LUN on this host
        dev_path = plug_device(dbg, uri)

        # Mount the gfs2 filesystem
        mnt_path = mount(dbg, dev_path)

        log.debug("%s: mounted on %s" % (dbg, mnt_path))
        uri = "file://" + mnt_path
        return uri

    def create(self, dbg, uri, name, description, configuration):
        log.debug("%s: SR.create: uri=%s, config=%s" % (dbg, uri, configuration))

        # Fetch the pool uuid to use as cluster id
        session = XenAPI.xapi_local()
        session.xenapi.login_with_password("root", "")
        pool = session.xenapi.pool.get_all()[0]
        pool_uuid = session.xenapi.pool.get_uuid(pool)

        # Cluster id is quite limited in size
        cluster_name = pool_uuid[:8]
        # Generate a UUID for the filesystem name
        # According to mkfs.gfs2 manpage, SR name can only be 1--16 chars in length
        sr_name = str(uuid.uuid4())[0:16]
        fsname = "%s:%s" % (cluster_name, sr_name)

        # Zone-in the LUN
        dev_path = plug_device(dbg, uri)
        log.debug("%s: dev_path = %s" % (dbg, dev_path))

        # Make sure we wipe any previous GFS2 metadata
        dd_dev_path = "of=" + dev_path
        cmd = ["/usr/bin/dd", "if=/dev/zero", dd_dev_path, "bs=1M", "count=10", "oflag=direct"]
        call(dbg, cmd)

        # Make the filesystem
        cmd = ["/usr/sbin/mkfs.gfs2",
               "-t", fsname,
               "-p", "lock_dlm",
               "-r", "2048",
               "-J", "128",
               "-O",
               "-j", "16",
               dev_path]
        call(dbg, cmd)

        # Temporarily mount the filesystem so we can write the SR metadata
        mnt_path = mount_local(dbg, dev_path)

        # FIXME: Move DB specific code to another place
        # create metadata DB
        import sqlite3
        conn = sqlite3.connect(mnt_path + "/sqlite3-metadata.db")
        with conn:
            conn.execute("create table VDI(key integer primary key, snap int,"
                         "parent int, name text, description text, vsize text,"
                         "uuid text, active_on text, gc_status text)")
            # TODO: define indexes, parent, uuid, (active_on?)
        conn.close()

        read_caching = True
        if 'read_caching' in configuration:
            if configuration['read_caching'] not in ['true', 't', 'on', '1', 'yes']:
                read_caching = False

        meta = {
            "name": name,
            "description": description,
            "uri": uri,
            "mountpath": mnt_path,
            "fsname": fsname,
            "read_caching": read_caching,
            "keys": {}
        }
        metapath = mnt_path + "/meta.json"
        log.debug("%s: dumping metadata to %s: %s" % (dbg, metapath, meta))

        with open(metapath, "w") as json_fp:
            json.dump(meta, json_fp)
            json_fp.write("\n")

        umount(dbg, mnt_path)
        unplug_device(dbg, uri)
        return

    def destroy(self, dbg, sr):
        # no need to destroy anything
        return

    def detach(self, dbg, sr):
        # Get the iSCSI uri from the SR metadata
        uri = getFromSRMetadata(dbg, sr, 'uri')

        # Unmount the FS
        sr_path = getSRpath(dbg, sr)
        umount(dbg, sr_path)

        # Unplug device if need be
        unplug_device(dbg, uri)

    def ls(self, dbg, sr):
        import gfs2
        return libvhd.ls(dbg, sr, gfs2.Callbacks())

    def stat(self, dbg, sr):
        # Get the filesystem size
        statvfs = os.statvfs(urlparse.urlparse(sr).path)
        psize = statvfs.f_blocks * statvfs.f_frsize
        fsize = statvfs.f_bfree * statvfs.f_frsize
        log.debug("%s: statvfs says psize = %Ld" % (dbg, psize))

        return {
            "sr": sr,
            "name": "SR Name",
            "description": "GFS2 SR",
            "total_space": psize,
            "free_space": fsize,
            "datasources": [],
            "clustered": True,
            "health": ["Healthy", ""]
        }

if __name__ == "__main__":
    log.log_call_argv()
    cmd = xapi.storage.api.volume.SR_commandline(Implementation())
    base = os.path.basename(sys.argv[0])
    if base == 'SR.probe':
        cmd.probe()
    elif base == 'SR.attach':
        cmd.attach()
    elif base == 'SR.create':
        cmd.create()
    elif base == 'SR.destroy':
        cmd.destroy()
    elif base == 'SR.detach':
        cmd.detach()
    elif base == 'SR.ls':
        cmd.ls()
    elif base == 'SR.stat':
        cmd.stat()
    else:
        raise xapi.storage.api.volume.Unimplemented(base)
