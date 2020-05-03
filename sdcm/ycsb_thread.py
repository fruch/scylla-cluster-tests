import os
import re
import logging
import time
import uuid
import tempfile
from textwrap import dedent

from sdcm.prometheus import nemesis_metrics_obj
from sdcm.sct_events import YcsbStressEvent, Severity
from sdcm.remote import FailuresWatcher
from sdcm.utils.common import FileFollowerThread
from sdcm.utils.thread import DockerBasedStressThread
from sdcm.utils.docker import RemoteDocker
from sdcm.utils.common import generate_random_string
from sdcm.stress_thread import format_stress_cmd_error

LOGGER = logging.getLogger(__name__)


class YcsbStatsPublisher(FileFollowerThread):
    METRICS = dict()
    collectible_ops = ['read', 'insert', 'update', 'read-failed', 'update-failed', 'verify']

    def __init__(self, loader_node, loader_idx, ycsb_log_filename):
        super().__init__()
        self.loader_node = loader_node
        self.loader_idx = loader_idx
        self.ycsb_log_filename = ycsb_log_filename
        self.uuid = generate_random_string(10)
        for operation in self.collectible_ops:
            gauge_name = self.gauge_name(operation)
            if gauge_name not in self.METRICS:
                metrics = nemesis_metrics_obj()
                self.METRICS[gauge_name] = metrics.create_gauge(gauge_name,
                                                                'Gauge for ycsb metrics',
                                                                ['instance', 'loader_idx', 'uuid', 'type'])

    @staticmethod
    def gauge_name(operation):
        return 'collectd_ycsb_%s_gauge' % operation.replace('-', '_')

    def set_metric(self, operation, name, value):
        metric = self.METRICS[self.gauge_name(operation)]
        metric.labels(self.loader_node.ip_address, self.loader_idx, self.uuid, name).set(value)

    def handle_verify_metric(self, line):
        verify_status_regex = re.compile(r"Return\((?P<status>.*?)\)=(?P<value>\d*)")
        verify_regex = re.compile(r'\[VERIFY:(.*?)\]')
        verify_content = verify_regex.findall(line)[0]

        for status_match in verify_status_regex.finditer(verify_content):
            stat = status_match.groupdict()
            self.set_metric('verify', stat['status'], float(stat['value']))

    def run(self):
        # pylint: disable=too-many-nested-blocks

        # 729.39 current ops/sec;
        # [READ: Count=510, Max=195327, Min=2011, Avg=4598.69, 90=5743, 99=12583, 99.9=194815, 99.99=195327]
        # [CLEANUP: Count=5, Max=3, Min=0, Avg=0.6, 90=3, 99=3, 99.9=3, 99.99=3]
        # [UPDATE: Count=490, Max=190975, Min=2004, Avg=3866.96, 90=4395, 99=6755, 99.9=190975, 99.99=190975]

        regex_dict = dict()
        for operation in self.collectible_ops:
            regex_dict[operation] = re.compile(
                fr'\[{operation.upper()}:\sCount=(?P<count>\d*?),'
                fr'.*?Max=(?P<max>\d*?),.*?Min=(?P<min>\d*?),'
                fr'.*?Avg=(?P<avg>.*?),.*?90=(?P<p90>\d*?),'
                fr'.*?99=(?P<p99>\d*?),.*?99.9=(?P<p999>\d*?),'
                fr'.*?99.99=(?P<p9999>\d*?)[\],\s]'
            )

        while not self.stopped():
            exists = os.path.isfile(self.ycsb_log_filename)
            if not exists:
                time.sleep(0.5)
                continue

            for _, line in enumerate(self.follow_file(self.ycsb_log_filename)):
                if self.stopped():
                    break
                try:
                    for operation, regex in regex_dict.items():
                        match = regex.search(line)
                        if match:
                            if operation == 'verify':
                                self.handle_verify_metric(line)

                            for key, value in match.groupdict().items():
                                if not key == 'count':
                                    try:
                                        value = float(value) / 1000.0
                                    except ValueError:
                                        LOGGER.exception("value isn't a number, default to 0")
                                        value = float(0)
                                self.set_metric(operation, key, float(value))

                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception("fail to send metric")


class YcsbStressThread(DockerBasedStressThread):  # pylint: disable=too-many-instance-attributes

    def copy_template(self, docker):
        if self.params.get('alternator_use_dns_routing'):
            target_address = 'alternator'
        else:
            target_address = self.node_list[0].private_ip_address

        if 'dynamodb' in self.stress_cmd:
            dynamodb_teample = dedent('''
                measurementtype=hdrhistogram
                dynamodb.awsCredentialsFile = /tmp/aws_empty_file
                dynamodb.endpoint = http://{0}:{1}
                dynamodb.connectMax = 200
                requestdistribution = uniform
                dynamodb.consistentReads = false
            '''.format(target_address,
                       self.params.get('alternator_port')))

            dynamodb_primarykey_type = self.params.get('dynamodb_primarykey_type', 'HASH')

            if dynamodb_primarykey_type == 'HASH_AND_RANGE':
                dynamodb_teample += dedent('''
                    dynamodb.primaryKey = p
                    dynamodb.hashKeyName = c
                    dynamodb.primaryKeyType = HASH_AND_RANGE
                ''')
            elif dynamodb_primarykey_type == 'HASH':
                dynamodb_teample += dedent('''
                    dynamodb.primaryKey = p
                    dynamodb.primaryKeyType = HASH
                ''')

            aws_empty_file = dedent(f""""
                accessKey = {self.params.get('alternator_access_key_id')}
                secretKey = {self.params.get('alternator_secret_access_key')}
            """)

            with tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8') as tmp_file:
                tmp_file.write(dynamodb_teample)
                tmp_file.flush()
                docker.send_files(tmp_file.name, os.path.join('/tmp', 'dynamodb.properties'))

            with tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8') as tmp_file:
                tmp_file.write(aws_empty_file)
                tmp_file.flush()
                docker.send_files(tmp_file.name, os.path.join('/tmp', 'aws_empty_file'))

    def build_stress_cmd(self):
        stress_cmd = f'{self.stress_cmd} -s -P /tmp/dynamodb.properties'
        if 'maxexecutiontime' not in stress_cmd:
            stress_cmd += f' -p maxexecutiontime={self.timeout}'
        return stress_cmd

    def _run_stress(self, loader, loader_idx, cpu_idx):
        dns_options = ""
        if self.params.get('alternator_use_dns_routing'):
            dns = RemoteDocker(loader, "scylladb/hydra-loaders:alternator-dns-0.2",
                               command_line=f'python3 /dns_server.py {self.node_list[0].ip_address} '
                                            f'{self.params.get("alternator_port")}',
                               extra_docker_opts=f'--label shell_marker={self.shell_marker}')
            dns_options += f'--dns {dns.internal_ip_address} --dns-option use-vc'
        docker = RemoteDocker(loader, "scylladb/hydra-loaders:ycsb-jdk8-20200211",
                              extra_docker_opts=f'{dns_options} --label shell_marker={self.shell_marker}')
        self.copy_template(docker)
        stress_cmd = self.build_stress_cmd()

        if not os.path.exists(loader.logdir):
            os.makedirs(loader.logdir)
        log_file_name = os.path.join(loader.logdir, 'ycsb-l%s-c%s-%s.log' %
                                     (loader_idx, cpu_idx, uuid.uuid4()))
        LOGGER.debug('ycsb-stress local log: %s', log_file_name)

        def raise_event_callback(sentinal, line):  # pylint: disable=unused-argument
            if line:
                YcsbStressEvent('error', severity=Severity.ERROR, node=loader,
                                stress_cmd=stress_cmd, errors=[line, ])

        LOGGER.debug("running: %s", stress_cmd)

        if self.stress_num > 1:
            node_cmd = 'taskset -c %s bash -c "%s"' % (cpu_idx, stress_cmd)
        else:
            node_cmd = stress_cmd

        node_cmd = 'cd /YCSB && {}'.format(node_cmd)

        YcsbStressEvent('start', node=loader, stress_cmd=stress_cmd)

        with YcsbStatsPublisher(loader, loader_idx, ycsb_log_filename=log_file_name):
            try:
                result = docker.run(cmd=node_cmd,
                                    timeout=self.timeout + self.shutdown_timeout,
                                    log_file=log_file_name,
                                    watchers=[FailuresWatcher(r'\sERROR|=UNEXPECTED_STATE', callback=raise_event_callback, raise_exception=False)])
                return result
            except Exception as exc:  # pylint: disable=broad-except
                errors_str = format_stress_cmd_error(exc)
                YcsbStressEvent(type='failure', node=str(loader), stress_cmd=self.stress_cmd,
                                log_file_name=log_file_name, severity=Severity.ERROR,
                                errors=[errors_str])
                raise
            finally:
                YcsbStressEvent('finish', node=loader, stress_cmd=stress_cmd, log_file_name=log_file_name)
