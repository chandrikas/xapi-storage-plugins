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
from xapi.storage.libs import blkinfo
from xapi.storage import log
import fcntl
import json
import xcp.environ
import XenAPI

# For a block device /a/b/c, we will mount it at <mountpoint_root>/a/b/c
mountpoint_root = "/var/run/sr-mount/"

def getSRMountPath(dbg, uri, check=True):
    dev_path = blkinfo.get_device_path(dbg, uri)
    mnt_path = os.path.abspath(mountpoint_root + dev_path)

    if check:
        if not(os.path.isdir(mnt_path)) or not(os.path.ismount(mnt_path)):
            raise xapi.storage.api.volume.Sr_not_attached(mnt_path)
    return mnt_path

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
        try:
            call(dbg, cmd)
        except:
            raise
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
        dev_path = blkinfo.get_device_path(dbg, uri)

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
        srs = []
        uris = []

        u = urlparse.urlparse(uri)
        if u.scheme == None:
            raise xapi.storage.api.volume.SR_does_not_exist(
                  "The SR URI is invalid")

        if u.scheme == 'iscsi':
            object_map = []
            # used for refcounting
            probe_uuid = str(uuid.uuid4())
            keys = libiscsi.decomposeISCSIuri(dbg, u)
            if keys['target'] == None:
                raise xapi.storage.api.volume.SR_does_not_exist(
                      "The SR URI is invalid")
            if keys['iqn'] == None:
                # uri has target but no IQN information
                # Return possible URI options by querying 
                # the target for IQN information
                iqn_map = libiscsi.discoverIQN(dbg, keys)
                if len(iqn_map) == 0:
                    raise xapi.storage.api.volume.SR_does_not_exist(
                          "No IQNs available at target")
                for record in iqn_map:
                    object_map.append(record[2])
            elif keys['scsiid'] == None:
                # uri has target and IQN but no LUN information
                # Return possible URI options by querying 
                # the target for LUN information
                try:
                    target_path = libiscsi.login(dbg, probe_uuid, keys)
                    lun_map = libiscsi.discoverLuns(dbg, target_path)
                finally: 
                    libiscsi.logout(dbg, probe_uuid, keys['iqn'])
                if len(lun_map) == 0:
                    raise xapi.storage.api.volume.SR_does_not_exist(
                          "No LUNs available at targetIQN")
                for record in lun_map:
                    object_map.append(record[4])
            else: 
                # URI is complete. Find out if the underlying                
                # device is formatted using GFS2.
                try: 
                    libiscsi.login(dbg, probe_uuid, keys)
                    dev_path = blkinfo.get_device_path(dbg, uri)
                    if blkinfo.get_format(dbg, dev_path) == "gfs2":
                        mount = False
                        try: 
                            mnt_path = getSRMountPath(dbg, uri)
                        except:
                            #mount path doesn't exist
                            mount = True
                            mnt_path = mount_local(dbg, dev_path)
                        # stat takes sr_path which is 
                        # file://<mnt_path>
                        sr_path = "file://%s" % mnt_path
                        srs.append(self.stat(dbg, sr_path))
                        if mount == True:
                            umount(dbg, mnt_path)
                finally: 
                    libiscsi.logout(dbg, probe_uuid, keys['iqn'])
                    
            if len(object_map):
                for obj in object_map: 
                    new_uri = uri + "/" + obj
                    uris.append(new_uri)
        else:
            #HBA transport
            dev_path = blkinfo.get_device_path(dbg, uri)
            if blkinfo.get_format(dbg, dev_path) == "gfs2":
                mount = False
                try: 
                    mnt_path = getSRMountPath(dbg, uri)
                except:
                    #mount path doesn't exist
                    mount = True
                    mnt_path = mount_local(dbg, dev_path)
                # stat takes sr_path which is 
                # file://<mnt_path>
                sr_path = "file://%s" % mnt_path
                srs.append(self.stat(dbg, sr_path))
                if mount == True:
                    umount(dbg, mnt_path)

        return {
            "srs": srs,
            "uris": uris
        }


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

        sr = "file://" + mnt_path

        # Start GC for this host
        libvhd.startGC(dbg, "gfs2", sr)

        return sr

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
                         "uuid text, active_on text, gc_status text, nonpersistent integer)")
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
            "unique_id": mnt_path[18:],
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

        # stop GC
        libvhd.stopGC(dbg, "gfs2", sr)

        # Unmount the FS
        mnt_path = getSRMountPath(dbg, uri)
        umount(dbg, mnt_path)

        # Unplug device if need be
        unplug_device(dbg, uri)

    def ls(self, dbg, sr):
        import gfs2
        return libvhd.ls(dbg, sr, gfs2.Callbacks())

    def stat(self, dbg, sr):
        # SR path (sr) is file://<mnt_path>
        # Get mnt_path by dropping url scheme
        uri = urlparse.urlparse(sr)
        mnt_path = "/%s/%s" % (uri.netloc, uri.path)

        if not(os.path.isdir(mnt_path)) or not(os.path.ismount(mnt_path)):
            raise xapi.storage.api.volume.Sr_not_attached(mnt_path)

        # Get the filesystem size
        statvfs = os.statvfs(mnt_path)
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
