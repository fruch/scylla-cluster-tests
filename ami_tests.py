from sdcm.tester import ClusterTester


class CustomCsTest(ClusterTester):

    def tearDown(self):
        pass

    @property
    def process(self):
        return self.db_cluster.nodes[0].remoter

    def find_command(self, command):
        return self.process.run('which ssh').stdout

    def run_cassandra_stress(self):
        def check_output(result):
            output = result.stdout + result.stderr
            lines = output.splitlines()
            for line in lines:
                if 'java.io.IOException' in line:
                    self.fail('cassandra-stress: %s' % line.strip())
        cassandra_stress_exec = self.find_command('cassandra-stress')
        stress_populate = ('%s write n=10000 -mode cql3 native -pop seq=1..10000' %
                           cassandra_stress_exec)
        result_populate = self.process.run(stress_populate, timeout=600)
        check_output(result_populate)
        stress_mixed = ('%s mixed duration=1m -mode cql3 native '
                        '-rate threads=10 -pop seq=1..10000' %
                        cassandra_stress_exec)
        result_mixed = self.process.run(stress_mixed, shell=True, timeout=300)
        check_output(result_mixed)

    def run_nodetool(self):
        nodetool_exec = self.find_command('nodetool')
        nodetool = '%s status' % nodetool_exec
        self.process.run(nodetool)

    def test_after_install(self):
        self.run_nodetool()
        self.run_cassandra_stress()

    def test_after_stop_start(self):
        self.srv_manager.stop_services()
        self.srv_manager.start_services()
        self.srv_manager.wait_services_up()
        self.run_nodetool()
        self.run_cassandra_stress()

    def test_after_restart(self):
        # check restart
        if self.uuid:
            version = self.version.replace('scylladb-', '')
            last_id = self.cvdb.get_last_id_v2("select * from housekeeping.checkversion where ruid='{}' and repoid='{}' and version like '{}%' and statuscode='r'".format(self.uuid, self.repoid, version))
        self.srv_manager.restart_services()
        self.srv_manager.wait_services_up()
        # check restart
        if self.uuid:
            assert self.cvdb.check_new_record_v2("select * from housekeeping.checkversion where ruid='{}' and repoid='{}' and version like '{}%' and statuscode='r'".format(self.uuid, self.repoid, version), last_id)
        self.run_nodetool()
        self.run_cassandra_stress()
