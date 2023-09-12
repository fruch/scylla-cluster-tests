from dataclasses import dataclass, field
import random

import yaml

from sdcm.nemesis import Nemesis, SisyphusMonkey
from sdcm.cluster import BaseScyllaCluster

PARAMS = dict(nemesis_interval=1, nemesis_filter_seeds=False)


@dataclass
class Node:
    running_nemesis = None
    public_ip_address: str = '127.0.0.1'
    name: str = 'Node1'

    @property
    def scylla_shards(self):
        return 8


@dataclass
class Cluster:
    nodes: list
    params: dict = field(default_factory=lambda: PARAMS)

    def check_cluster_health(self):
        pass


@dataclass
class FakeTester:
    params: dict = field(default_factory=lambda: PARAMS)
    loaders: list = field(default_factory=list)
    db_cluster: Cluster | BaseScyllaCluster = field(default_factory=lambda: Cluster(nodes=[Node(), Node()]))
    monitors: list = field(default_factory=list)

    def __post_init__(self):
        self.db_cluster.params = self.params

    def create_stats(self):
        pass

    def update(self, *args, **kwargs):
        pass

    def get_scylla_versions(self):
        pass

    def get_test_details(self):
        pass

    def id(self):  # pylint: disable=invalid-name,no-self-use
        return 0


class FakeNemesis(Nemesis):
    def __new__(cls, tester_obj, termination_event, *args):  # pylint: disable=unused-argument
        return object.__new__(cls)

    def disrupt(self):
        pass


def test_list_all_available_nemesis(generate_file=True):
    tester = FakeTester()

    tester.params["nemesis_seed"] = '1'
    sisyphus = SisyphusMonkey(tester, None)

    subclasses = sisyphus._get_subclasses()  # pylint: disable=protected-access
    disruption_list, disruptions_dict, disruption_classes = sisyphus.get_list_of_disrupt_methods(
        subclasses_list=subclasses, export_properties=True)

    assert len(disruption_list) == 84

    if generate_file:
        with open('data_dir/nemesis.yml', 'w', encoding="utf-8") as outfile1:
            yaml.dump(disruptions_dict, outfile1, default_flow_style=False)

        with open('data_dir/nemesis_classes.yml', 'w', encoding="utf-8") as outfile2:
            yaml.dump(disruption_classes, outfile2, default_flow_style=False)

    with open('data_dir/nemesis.yml', 'r', encoding="utf-8") as nemesis_file:
        static_nemesis_list = yaml.safe_load(nemesis_file)

    assert static_nemesis_list == disruptions_dict


def test_generate_specific_nemesis_seed():
    tester = FakeTester()
    targeted_nemesis_function = 'disrupt_rolling_restart_cluster'

    for _ in range(50):
        tester.params["nemesis_seed"] = str(random.randint(1, 99))
        sisyphus_nemesis = SisyphusMonkey(tester, None)
        collected_disrupt_methods_names = [disrupt.__name__ for disrupt in sisyphus_nemesis.disruptions_list]
        first_five_functions = collected_disrupt_methods_names[:5]
        if targeted_nemesis_function in first_five_functions:
            break

    print(f'nemesis_seed: {tester.params["nemesis_seed"]}')
