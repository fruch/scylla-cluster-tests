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
import logging
import os
import unittest

from sdcm.utils.data_validator import LongevityDataValidator
from sdcm import sct_config


class ConfigurationBase(unittest.TestCase):  # pylint: disable=too-many-public-methods
    _testMethodName = "runTest"

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.ERROR)
        logging.getLogger('botocore').setLevel(logging.CRITICAL)
        logging.getLogger('boto3').setLevel(logging.CRITICAL)
        logging.getLogger('anyconfig').setLevel(logging.ERROR)

        cls.setup_default_env()

        os.environ['SCT_CLUSTER_BACKEND'] = 'aws'
        os.environ['SCT_AMI_ID_DB_SCYLLA'] = 'ami-06f919eb'
        cls.conf = sct_config.SCTConfiguration()

        cls.clear_sct_env_variables()

        # some of the tests assume this basic case is define, to avoid putting this again and again in each test
        # and so we can run those tests specifically
        cls.setup_default_env()

    def tearDown(self):
        self.clear_sct_env_variables()
        self.setup_default_env()

    @classmethod
    def setup_default_env(cls):
        os.environ['SCT_CONFIG_FILES'] = 'internal_test_data/minimal_test_case.yaml'

    @classmethod
    def clear_sct_env_variables(cls):
        for k in os.environ:
            if k.startswith('SCT_'):
                del os.environ[k]


class TestDataValidator(ConfigurationBase):
    def test_view_names_for_updated_data(self):
        os.environ[
            'SCT_CONFIG_FILES'] = 'unit_tests/test_data/test_data_validator/lwt-basic-3h.yaml'
        os.environ['SCT_CLUSTER_BACKEND'] = 'aws'
        os.environ['SCT_AMI_ID_DB_SCYLLA'] = 'ami-06f919eb'
        os.environ['SCT_INSTANCE_TYPE_DB'] = 'i3.large'
        self.params = sct_config.SCTConfiguration()  # pylint: disable=attribute-defined-outside-init
        self.params.verify_configuration()

        data_validator = LongevityDataValidator(longevity_self_object=self,
                                                user_profile_name='c-s_lwt',
                                                base_table_partition_keys=['domain', 'published_date'])
        data_validator._validate_updated_per_view = [True, True]   # pylint: disable=protected-access
        views_list = data_validator.list_of_view_names_for_update_test()
        assert views_list == [('blogposts_update_one_column_lwt_indicator',
                               'blogposts_update_one_column_lwt_indicator_after_update',
                               'blogposts_update_one_column_lwt_indicator_expect', True),
                              ('blogposts_update_2_columns_lwt_indicator',
                               'blogposts_update_2_columns_lwt_indicator_after_update',
                               'blogposts_update_2_columns_lwt_indicator_expect', True)]

    def test_view_names_for_updated_data_not_found(self):
        os.environ[
            'SCT_CONFIG_FILES'] = 'unit_tests/test_data/test_data_validator/no-validation-views-lwt-basic-3h.yaml'
        os.environ['SCT_CLUSTER_BACKEND'] = 'aws'
        os.environ['SCT_AMI_ID_DB_SCYLLA'] = 'ami-06f919eb'
        os.environ['SCT_INSTANCE_TYPE_DB'] = 'i3.large'
        self.params = sct_config.SCTConfiguration()  # pylint: disable=attribute-defined-outside-init
        self.params.verify_configuration()

        data_validator = LongevityDataValidator(longevity_self_object=self,
                                                user_profile_name='c-s_lwt',
                                                base_table_partition_keys=['domain', 'published_date'])
        data_validator._validate_updated_per_view = [True, True]   # pylint: disable=protected-access
        views_list = data_validator.list_of_view_names_for_update_test()
        assert views_list == []
