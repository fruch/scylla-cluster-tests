#!/usr/bin/env python
from __future__ import print_function
import sys
import os.path

import requests
import click


sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from sdcm.keystore import KeyStore


@click.command(help="Check test configuration file")
@click.argument('index_name', type=str)
def fix_es_mapping(index_name):
    ks = KeyStore()
    es_conf = ks.get_elasticsearch_credentials()

    mapping_url = "{es_url}/{index_name}/_mappings".format(index_name=index_name, **es_conf)
    res = requests.get(mapping_url + "?include_type_name=true", auth=(es_conf["es_user"], es_conf["es_password"]))
    output = res.json()[index_name]

    output['mappings']['test_stats']['dynamic'] = False

    output['mappings']['test_stats']['properties']['coredumps'] = dict(type='object')
    output['mappings']['test_stats']['properties']['setup_details']['properties']['db_cluster_details'] = dict(
        type='object')
    output['mappings']['test_stats']['properties']['system_details'] = {"dynamic": False, "properties": {}}

    res = requests.put(mapping_url,
                       json=output['mappings'], auth=(es_conf["es_user"], es_conf["es_password"]))
    print(res.text)
    res.raise_for_status()

    click.secho("fixed {index_name}".format(index_name=index_name), fg='green')


if __name__ == '__main__':
    fix_es_mapping()  # pylint: disable=no-value-for-parameter
