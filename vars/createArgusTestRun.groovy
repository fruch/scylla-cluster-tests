#!groovy

def call(Map params) {
    def test_config = groovy.json.JsonOutput.toJson(params.test_config)
    retry(3) {
		sh """#!/bin/bash
			set -xe

			echo "Creating Argus test run ..."
			if [[ -n "${params.requested_by_user ? params.requested_by_user : ''}" ]] ; then
				export BUILD_USER_REQUESTED_BY=${params.requested_by_user}
			fi
			export SCT_CLUSTER_BACKEND="${params.backend}"
			export SCT_CONFIG_FILES=${test_config}

			if [[ -n "${params.reuse_cluster ?: ''}" ]] ; then
				export SCT_REUSE_CLUSTER="${params.reuse_cluster}"
			fi

            if [[ "${params.backend}" == "xcloud" ]] ; then
                export SCT_XCLOUD_PROVIDER="${params.xcloud_provider}"
                export SCT_XCLOUD_ENV="${params.xcloud_env}"
            fi

			./docker/env/hydra.sh create-argus-test-run

			echo " Argus test run created."
		"""
    }
    if (!currentBuild.description) {
        currentBuild.description = ''
    }
    String runButton = """
        <div style="margin: 12px 4px;">
            <a
                href='https://argus.scylladb.com/tests/scylla-cluster-tests/${SCT_TEST_ID}'
            >
                Argus: <span style='font-weight: 500'>${SCT_TEST_ID}</span>
            </a>
        </div>
    """
    currentBuild.description += "${runButton}"

    // Read test_metadata from config YAML and add to build description for Argus indexing
    try {
        def configFiles = params.test_config instanceof List ? params.test_config : [params.test_config]
        for (configFile in configFiles) {
            if (fileExists(configFile)) {
                def yamlContent = readYaml file: configFile
                if (yamlContent?.test_metadata) {
                    def meta = yamlContent.test_metadata
                    String metaHtml = """
                        <div style="margin: 4px; font-size: 0.85em; color: #555;">
                            <b>Metadata:</b> tier=${meta.tier ?: 'n/a'} |
                            type=${meta.test_type ?: 'n/a'} |
                            duration=${meta.duration_class ?: 'n/a'} |
                            backends=${meta.supported_backends ?: 'n/a'}
                        </div>
                    """
                    currentBuild.description += metaHtml
                    break
                }
            }
        }
    } catch (Exception e) {
        echo "Warning: Could not read test_metadata for build description: ${e.message}"
    }
}
