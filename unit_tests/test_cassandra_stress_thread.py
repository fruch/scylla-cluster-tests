# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2022 ScyllaDB
from pathlib import Path

import pytest

from sdcm.stress_thread import CassandraStressThread
from unit_tests.dummy_remote import LocalLoaderSetDummy

pytestmark = [
    pytest.mark.usefixtures("events"),
    pytest.mark.skip(reason="those are integration tests only"),
]


def test_01_cassandra_stress(request, docker_scylla, params):
    loader_set = LocalLoaderSetDummy()

    cmd = (
        """cassandra-stress write cl=ONE duration=1m -schema 'replication(factor=1) """
        """compaction(strategy=SizeTieredCompactionStrategy)' -mode cql3 native """
        """-rate threads=10 -pop seq=1..10000000 -log interval=5"""
    )

    cs_thread = CassandraStressThread(
        loader_set, cmd, node_list=[docker_scylla], timeout=120, params=params
    )

    def cleanup_thread():
        cs_thread.kill()

    request.addfinalizer(cleanup_thread)

    cs_thread.run()

    output = cs_thread.get_results()
    assert "latency mean" in output[0]
    assert float(output[0]["latency mean"]) > 0

    assert "latency 99th percentile" in output[0]
    assert float(output[0]["latency 99th percentile"]) > 0


def test_02_cassandra_stress_user_profile(request, docker_scylla, params):
    loader_set = LocalLoaderSetDummy()

    cmd = (
        "cassandra-stress user profile=/tmp/c-s_profile_2si_2queries.yaml ops'(insert=10,si_p_read1=1,si_p_read2=1)' "
        "cl=ONE duration=1m -mode cql3 native -rate threads=10"
    )

    cs_thread = CassandraStressThread(
        loader_set, cmd, node_list=[docker_scylla], timeout=120, params=params
    )

    def cleanup_thread():
        cs_thread.kill()

    request.addfinalizer(cleanup_thread)

    cs_thread.run()

    output = cs_thread.get_results()
    assert "latency mean" in output[0]
    assert float(output[0]["latency mean"]) > 0

    assert "latency 99th percentile" in output[0]
    assert float(output[0]["latency 99th percentile"]) > 0


@pytest.mark.docker_scylla_args(ssl=True)
def test_03_cassandra_stress_client_encrypt(request, docker_scylla, params):

    loader_set = LocalLoaderSetDummy()

    cmd = (
        """cassandra-stress write cl=ONE duration=1m -schema 'replication(factor=1) """
        """compaction(strategy=SizeTieredCompactionStrategy)' -mode cql3 native """
        """-rate threads=10 -pop seq=1..10000000 -log interval=5"""
    )

    cs_thread = CassandraStressThread(
        loader_set,
        cmd,
        node_list=[docker_scylla],
        timeout=120,
        client_encrypt=True,
        ssl_dir=(Path(__file__).parent.parent / "data_dir" / "ssl_conf").absolute(),
        params=params,
    )

    def cleanup_thread():
        cs_thread.kill()

    request.addfinalizer(cleanup_thread)

    cs_thread.run()

    output = cs_thread.get_results()
    assert "latency mean" in output[0]
    assert float(output[0]["latency mean"]) > 0

    assert "latency 99th percentile" in output[0]
    assert float(output[0]["latency 99th percentile"]) > 0
