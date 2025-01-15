import datetime
import re
import time
import traceback

from packaging import version

from ceph.ceph_admin import CephAdmin
from ceph.rados.core_workflows import RadosOrchestrator
from ceph.rados.pool_workflows import PoolFunctions
from tests.rados.monitor_configurations import MonConfigMethods
from utility.log import Log
from utility.utils import method_should_succeed

log = Log(__name__)


def run(ceph_cluster, **kw):
    """
    Test to create an inconsistent object functionality during scrub/deep-scrub in EC pool.
    Returns:
        1 -> Fail, 0 -> Pass
    """

    global old_deep_scrub_value, pool_name, fix_version, acting_set
    log.info(run.__doc__)
    config = kw["config"]
    cephadm = CephAdmin(cluster=ceph_cluster, **config)
    rados_object = RadosOrchestrator(node=cephadm)
    mon_obj = MonConfigMethods(rados_obj=rados_object)
    client_node = ceph_cluster.get_nodes(role="client")[0]
    pool_obj = PoolFunctions(node=cephadm)
    ceph_nodes = kw.get("ceph_nodes")
    osd_list = []
    mclock_max_capacity_values = ["2.985434", "0.198044", "0.198104"]
    file_name = "/tmp/deep_scrub_time.txt"
    bug_fixed_flag = False
    try:

        ceph_version = rados_object.run_ceph_command(cmd="ceph version")
        log.info(f"Current version on the cluster : {ceph_version}")

        match_str = re.match(
            r"ceph version ((\d+)\.\d+\.\d+-\d+)", ceph_version["version"]
        )

        full_version, major = match_str.groups()
        if int(major) == 18:
            fix_version = "18.2.1-269"
        # TODO - Add the condition for the Squid version

        if compare_versions(
            fix_version, full_version
        ) and rados_object.check_file_exists_on_client(loc=file_name):
            bug_fixed_flag = True
        pool_name = config["pool_name"]
        if not bug_fixed_flag:
            # set global autoscaler to off
            rados_object.configure_pg_autoscaler(**{"default_mode": "off"})
            method_should_succeed(rados_object.create_pool, **config)
            rados_object.bench_write(pool_name=pool_name, rados_write_duration=90)
        # Get the OSD list
        for node in ceph_nodes:
            if node.role == "osd":
                node_osds = rados_object.collect_osd_daemon_ids(node)
                osd_list = osd_list + node_osds
            log.info(f"The OSDs in the cluster are-{osd_list}")

            # Get the osd_mclock_max_capacity_iops_hdd values for all OSDs
        for osd_id in osd_list:
            capacity_iops_value = mon_obj.show_config(
                daemon="osd", id=osd_id, param="osd_mclock_max_capacity_iops_hdd"
            )
            log.info(
                f"The osd-{osd_id} osd_mclock_max_capacity_iops_hdd value - {capacity_iops_value}"
            )
        pg_id = pool_obj.get_pool_id(pool_name=pool_name)
        log.info(f"The {pool_name} pool id is -{pg_id}")

        # Get the pg acting set
        acting_set = rados_object.get_pg_acting_set(pool_name=pool_name)
        log.info(f"The PG acting set is -{acting_set}")
        # Reproducing the bug
        if not bug_fixed_flag:
            # Modify the acting set osd_mclock_max_capacity_iops_hdd value
            for osd_id, value in zip(acting_set, mclock_max_capacity_values):
                section_id = f"osd.{osd_id}"
                mon_obj.set_config(
                    section=section_id,
                    name="osd_mclock_max_capacity_iops_hdd",
                    value=value,
                )
                time.sleep(5)
                rados_object.change_osd_state(action="restart", target=osd_id)
                capacity_iops_value = mon_obj.show_config(
                    daemon="osd", id=osd_id, param="osd_mclock_max_capacity_iops_hdd"
                )
                log.info(f"The osd.{osd_id} value is set to the -{capacity_iops_value}")
        else:
            # Verification of the bug
            mon_obj.set_config(
                section="osd",
                name="osd_mclock_force_run_benchmark_on_init",
                value="true",
            )
            for osd_id in acting_set:
                section_id = f"osd.{osd_id}"
                mon_obj.remove_config(
                    section=section_id, name="osd_mclock_max_capacity_iops_hdd"
                )
                rados_object.change_osd_state(action="restart", target=osd_id)
                capacity_iops_value = mon_obj.show_config(
                    daemon="osd", id=osd_id, param="osd_mclock_max_capacity_iops_hdd"
                )
                log.info(
                    f"The osd.{osd_id} value after removing the parameter -{capacity_iops_value}"
                )
            time.sleep(60)
            # Verification of the osd_mclock_max_capacity_iops_hdd values
            for osd_id in acting_set:
                capacity_iops_value = mon_obj.show_config(
                    daemon="osd", id=osd_id, param="osd_mclock_max_capacity_iops_hdd"
                )
                if float(capacity_iops_value) < 50 or float(capacity_iops_value) > 500:
                    log.error(
                        f"The osd_mclock_max_capacity_iops_hdd parameter value for the osd.{osd_id} is "
                        f"{capacity_iops_value}. The value range is in between 50 and 500"
                    )
                    return 1

        # Perform deep-scrub on the PG
        deep_scrub_result = perform_deep_scrub(rados_object, pg_id)

        log.info(f"The deep scrub results are -{deep_scrub_result}")
        if not bug_fixed_flag:
            cmd_touch_file = f"touch {file_name}"
            client_node.exec_command(cmd=cmd_touch_file)
            cmd_deep_scrub_result = f"echo {deep_scrub_result} > {file_name}"
            client_node.exec_command(cmd=cmd_deep_scrub_result, sudo=True)
        else:

            cmd_get_deep_scrub_value = f"cat {file_name}"
            old_deep_scrub_value = client_node.exec_command(
                cmd=cmd_get_deep_scrub_value, sudo=True
            )

        parsed_time = datetime.datetime.strptime(
            old_deep_scrub_value[0].strip(), "%H:%M:%S.%f"
        )
        old_time_as_timedelta = datetime.timedelta(
            hours=parsed_time.hour,
            minutes=parsed_time.minute,
            seconds=parsed_time.second,
            microseconds=parsed_time.microsecond,
        )
        if deep_scrub_result > old_time_as_timedelta:
            log.error(
                f"The deep-scrub operation before the fix is {old_deep_scrub_value} and "
                f"after the fix is-{deep_scrub_result}"
            )
            return 1
    except Exception as e:
        log.info(e)
        log.info(traceback.format_exc())
        return 1
    finally:
        log.info(
            "\n\n================ Execution of finally block =======================\n\n"
        )
        # Deletion not required in the bug-affected version.
        if bug_fixed_flag:
            # Delete the pool
            method_should_succeed(rados_object.delete_pool, pool_name)
            for osd_id in acting_set:
                section_id = f"osd.{osd_id}"
                mon_obj.remove_config(
                    section=section_id, name="osd_mclock_max_capacity_iops_hdd"
                )
                rados_object.change_osd_state(action="restart", target=osd_id)

        mon_obj.remove_config(
            section="osd", name="osd_mclock_force_run_benchmark_on_init"
        )
    return 0


def perform_deep_scrub(rados_object, pool_id):
    """
    The method is used to initial the deep-scrub and time taken to complete the deep-scrub operation
    Args:
        rados_object: Rados object
        pool_id: pool id

    Returns: Return the time taken to complete the deep-scrub operation

    """
    global current_deep_scrub_stamp
    # The pool has the sing PG
    pg_id = f"{pool_id}.0"
    init_pool_pg_dump = rados_object.get_ceph_pg_dump(pg_id=pg_id)
    init_deep_scrub_stamp = datetime.datetime.strptime(
        init_pool_pg_dump["last_deep_scrub_stamp"], "%Y-%m-%dT%H:%M:%S.%f%z"
    )
    deep_scrub_status = False
    log.info("The stats before starting deep-scrub")
    log.info(f"last_deep_scrub_stamp: {init_pool_pg_dump['last_deep_scrub_stamp']}")
    log.debug(f"Initiating deep-scrubbing on pg : {pg_id}")
    rados_object.run_deep_scrub(pgid=pg_id)
    time.sleep(2)

    # Parse the timestamp string into a datetime object
    while not deep_scrub_status:
        pool_pg_dump = rados_object.get_ceph_pg_dump(pg_id=pg_id)
        current_deep_scrub_stamp = datetime.datetime.strptime(
            pool_pg_dump["last_deep_scrub_stamp"], "%Y-%m-%dT%H:%M:%S.%f%z"
        )
        if current_deep_scrub_stamp > init_deep_scrub_stamp:
            log.info(f"The current deep-scrub time stamp is-{current_deep_scrub_stamp}")
            deep_scrub_difference = current_deep_scrub_stamp - init_deep_scrub_stamp
            log.info(
                f"The deep-scrub operation took -{deep_scrub_difference} time to complete the operation"
            )
            return deep_scrub_difference
        log.info("The deep-scrub is in progress.....")
        log.info(
            f"The initial deep-scrub is-{init_deep_scrub_stamp} and "
            f"current deep-scrub is-{current_deep_scrub_stamp}"
        )
        time.sleep(10)


def compare_versions(bug_fixed_version, current_version):
    """
    Compares two version strings.
    Args:
      bug_fixed_version: The version which  fixed the bug
      current_version: The current version of the cluster

    Returns:
       False,if current_version is lesser than the bug_fixed_version else True
    """
    if version.parse(current_version) < version.parse(bug_fixed_version):
        return False
    else:
        return True
