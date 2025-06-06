import json
import secrets
import string
import time
import traceback
from json import JSONDecodeError

from ceph.ceph import CommandFailed
from tests.cephfs.cephfs_utilsV1 import FsUtils
from tests.cephfs.cephfs_volume_management import wait_for_process
from utility.log import Log

log = Log(__name__)


def run(ceph_cluster, **kw):
    """
    CEPH-83574022 - Create NFS cluster with 2 Nodes. 1 nfs daemon per node and Ensure the exports are accessible via
                    all the NFS nodes in the cluster. and has same data
    Pre-requisites:
    1. Create cephfs volume
       creats fs volume create <vol_name>
    2. Create nfs cluster
       ceph nfs cluster create <nfs_name> <nfs_server>
    Test operation:
    1. Delete nfs cluster
       ceph nfs cluster delete <nfs_name>
    2. Create nfs cluster with same name
       ceph nfs cluster create <nfs_name> <nfs_server1>,<nfs_server2>
    3. Create cephfs nfs export
       ceph nfs export create cephfs <fs_name> <nfs_name> <nfs_export_name> path=<export_path>
    4. Mount nfs mount with cephfs export usong first server
       mount -t nfs -o port=2049 <nfs_server1>:<nfs_export> <nfs_mounting_dir>
    5. Mount nfs mount with cephfs export usong second server
       mount -t nfs -o port=2049 <nfs_server2>:<nfs_export> <nfs_mounting_dir>
    6. Run IO's on first mount , verify same data exists on second mount
    7. Run IO's on second mount , verify same data exists on first mount
    Clean-up:
    1. Remove data in cephfs
    2. Remove cephfs nfs export
    3. Remove all nfs mounts
    """
    try:
        tc = "CEPH-83574028"
        log.info(f"Running cephfs {tc} test case")

        config = kw["config"]
        build = config.get("build", config.get("rhbuild"))

        test_data = kw.get("test_data")
        fs_util = FsUtils(ceph_cluster, test_data=test_data)
        erasure = (
            FsUtils.get_custom_config_value(test_data, "erasure")
            if test_data
            else False
        )
        clients = ceph_cluster.get_ceph_objects("client")
        client1 = clients[0]
        fs_util.prepare_clients(clients, build)
        fs_util.auth_list(clients)
        mon_node_ip = fs_util.get_mon_node_ips()
        mon_node_ip = ",".join(mon_node_ip)
        rhbuild = config.get("rhbuild")
        nfs_servers = ceph_cluster.get_ceph_objects("nfs")
        nfs1 = nfs_servers[0].node.hostname
        nfs2 = nfs_servers[1].node.hostname
        nfs_name = "cephfs-nfs"
        clients = ceph_cluster.get_ceph_objects("client")
        client1 = clients[0]
        nfs_mounting_dir_1 = "/mnt/nfs_" + "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for i in range(5)
        )
        nfs_mounting_dir_2 = "/mnt/nfs_" + "".join(
            secrets.choice(string.ascii_uppercase + string.digits) for i in range(5)
        )

        out, rc = client1.exec_command(
            sudo=True, cmd=f"ceph nfs cluster create {nfs_name} {nfs1},{nfs2}"
        )
        if not wait_for_process(client=client1, process_name=nfs_name, ispresent=True):
            raise CommandFailed("Cluster has not been created")

        try:
            out, rc = client1.exec_command(sudo=True, cmd="ceph nfs cluster ls -f json")
            output = json.loads(out)
        except JSONDecodeError:
            output = json.dumps([out])
        if nfs_name in output:
            log.info("ceph nfs cluster created successfully")
        else:
            raise CommandFailed("Failed to create nfs cluster")

        nfs_export_name = "/export_" + "".join(
            secrets.choice(string.digits) for i in range(3)
        )
        export_path = "/"
        fs_name = "cephfs" if not erasure else "cephfs-ec"
        fs_details = fs_util.get_fs_info(client1, fs_name)

        if not fs_details:
            fs_util.create_fs(client1, fs_name)
        if "5.0" in rhbuild:
            client1.exec_command(
                sudo=True,
                cmd=f"ceph nfs export create cephfs {fs_name} {nfs_name} "
                f"{nfs_export_name} path={export_path}",
            )
        else:
            client1.exec_command(
                sudo=True,
                cmd=f"ceph nfs export create cephfs {nfs_name} "
                f"{nfs_export_name} {fs_name} path={export_path}",
            )
        rc = fs_util.cephfs_nfs_mount(
            client1, nfs1, nfs_export_name, nfs_mounting_dir_1
        )
        if not rc:
            log.error("cephfs nfs export mount failed")
            return 1
        commands = [
            f"mkdir -p {nfs_mounting_dir_1}",
            f"mkdir {nfs_mounting_dir_1}/dir1 {nfs_mounting_dir_1}/dir2",
            f"python3 /home/cephuser/smallfile/smallfile_cli.py --operation create --threads 10 --file-size 4 --files"
            f" 500 --files-per-dir 10 --dirs-per-dir 2 --top {nfs_mounting_dir_1}/dir1",
            f"for n in {{1..10}}; do     dd if=/dev/zero of={nfs_mounting_dir_1}/dir2"
            f"/file$(printf %03d $n) bs=500k count=500; done",
        ]
        for command in commands:
            client1.exec_command(sudo=True, cmd=command, long_running=True)
            log.info("Sleeping for 5 seconds between commands...")
            time.sleep(5)
        rc = fs_util.cephfs_nfs_mount(
            client1, nfs2, nfs_export_name, nfs_mounting_dir_2
        )
        if not rc:
            log.error("cephfs nfs export mount failed")
            return 1
        log.info("Waiting for 30 seconds for nfs mount to be ready for validation")
        time.sleep(30)
        commands = [
            rf"rsync -ani {nfs_mounting_dir_1} {nfs_mounting_dir_2} | grep -qv '^\.'",
            f"mkdir {nfs_mounting_dir_2}/dir3 {nfs_mounting_dir_2}/dir4",
            f"for n in {{1..5}}; do     dd if=/dev/zero of={nfs_mounting_dir_2}/dir3"
            f"/file$(printf %03d $n) bs=500k count=500; done",
            f"python3 /home/cephuser/smallfile/smallfile_cli.py --operation create --threads 10 --file-size 4 "
            f" --files 10 --files-per-dir 10 --dirs-per-dir 2 --top {nfs_mounting_dir_2}/dir4",
        ]
        for command in commands:
            client1.exec_command(sudo=True, cmd=command, long_running=True)
            log.info("Sleeping for 5 seconds between commands...")
            time.sleep(5)

        log.info("Waiting for 30 seconds for nfs mount to be ready for validation")
        time.sleep(30)
        client1.exec_command(
            sudo=True,
            cmd=rf"rsync -ani {nfs_mounting_dir_1} {nfs_mounting_dir_2} | grep -qv '^\.'",
            long_running=True,
        )
        return 0
    except Exception as e:
        log.info(e)
        log.info(traceback.format_exc())
        return 1
    finally:
        log.info("Cleaning up the system")
        commands = [
            f"rm -rf {nfs_mounting_dir_1}/*",
            f"umount {nfs_mounting_dir_1}",
            f"umount {nfs_mounting_dir_2}",
            f"ceph nfs export delete {nfs_name} {nfs_export_name}",
        ]
        for command in commands:
            client1.exec_command(sudo=True, cmd=command)
        client1.exec_command(
            sudo=True, cmd=f"rm -rf {nfs_mounting_dir_1}/", check_ec=False
        )
        client1.exec_command(
            sudo=True, cmd=f"rm -rf {nfs_mounting_dir_2}/", check_ec=False
        )
        client1.exec_command(
            sudo=True,
            cmd=f"ceph nfs cluster delete {nfs_name}",
            check_ec=False,
        )
