#!groovy

def call(Map pipelineParams) {
    pipeline {
        agent {
            label {
                label "aws-eu-west1-qa-builder1"
            }
        }
        environment {
            AWS_ACCESS_KEY_ID     = credentials('qa-aws-secret-key-id')
            AWS_SECRET_ACCESS_KEY = credentials('qa-aws-secret-access-key')
        }
        parameters {
            string(defaultValue: "${pipelineParams.get('db_instance_type', 'i3.xlarge')}",
                   description: 'any type support by scylla cloud',
                   name: 'db_instance_type')

            string(defaultValue: "${pipelineParams.get('n_db_nodes', '3')}",
                   description: 'any type support by scylla cloud',
                   name: 'n_db_nodes')

            string(defaultValue: "${pipelineParams.get('provision_type', 'spot_low_price')}",
                   description: 'spot_low_price|on_demand|spot_fleet|spot_low_price|spot_duration',
                   name: 'provision_type')

            string(defaultValue: "${pipelineParams.get('post_behavior_db_nodes', 'keep')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_db_nodes')
            string(defaultValue: "${pipelineParams.get('post_behavior_loader_nodes', 'destroy')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_loader_nodes')
            string(defaultValue: "${pipelineParams.get('post_behavior_monitor_nodes', 'keep-on-failure')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_monitor_nodes')

            string(defaultValue: "${pipelineParams.get('email_recipients', 'qa@scylladb.com')}",
                   description: 'email recipients of email report',
                   name: 'email_recipients')


        }
        options {
            timestamps()
            disableConcurrentBuilds()
            timeout(pipelineParams.timeout)
            buildDiscarder(logRotator(numToKeepStr: '20'))
        }

        stages {
            stage('Checkout') {
               steps {
                  dir("siren-tests") {
                    git(url: 'git@github.com:scylladb/siren-tests.git',
                      credentialsId:'b8a774da-0e46-4c91-9f74-09caebaea261',
                      branch: 'master')
                  }
                  dir('scylla-cluster-tests') {
                      checkout scm
                  }
               }
            }
            stage('Create Cluster with siren-tests') {
                steps {
                        wrap([$class: 'BuildUser']) {
                            dir('siren-tests') {
                                sh """
                                #!/bin/bash
                                set -xe

                                export SIRENADA_BROWSER=chrome-headless

                                source /opt/rh/rh-python35/enable
                                # update the environment
                                ~/.local/bin/pipenv --bare install

                                export SIRENADA_REGION=eu-west-1
                                export SIRENADA_NUMBER_OF_NODES=${params.n_db_nodes}
                                export SIRENADA_CLUSTER_KEEP=50
                                export SIRENADA_INSTANCE_TYPE=${params.db_instance_type}

                                ~/.local/bin/pipenv run ./runtests.py --sct-conf
                                """
                            }
                        }
                }
            }
            stage('Run SCT Test') {
                steps {
                    catchError(stageResult: 'FAILURE') {
                        wrap([$class: 'BuildUser']) {
                            dir('scylla-cluster-tests') {
                                sh """
                                #!/bin/bash
                                set -xe
                                env

                                export SCT_CLUSTER_BACKEND=aws-siren
                                export SCT_INTRA_NODE_COMM_PUBLIC=true
                                export SCT_REGION_NAME=eu-west-1
                                export SCT_CONFIG_FILES="['${pipelineParams.test_config}', '`realpath ../siren-tests/test_results/scylla_cloud.yaml`']"

                                export SCT_POST_BEHAVIOR_DB_NODES="${params.post_behavior_db_nodes}"
                                export SCT_POST_BEHAVIOR_LOADER_NODES="${params.post_behavior_loader_nodes}"
                                export SCT_POST_BEHAVIOR_MONITOR_NODES="${params.post_behavior_monitor_nodes}"
                                export SCT_INSTANCE_PROVISION=${pipelineParams.params.get('provision_type', '')}
                                export SCT_AMI_ID_DB_SCYLLA_DESC=\$(echo \$GIT_BRANCH | sed -E 's+(origin/|origin/branch-)++')
                                export SCT_AMI_ID_DB_SCYLLA_DESC=\$(echo \$SCT_AMI_ID_DB_SCYLLA_DESC | tr ._ - | cut -c1-8 )

                                echo "start test ......."
                                ./docker/env/hydra.sh run-test ${pipelineParams.test_name} --backend aws-siren --logdir /sct
                                echo "end test ....."
                               """
                            }
                        }
                    }
                }
            }
            stage('Collect log data') {
                steps {
                    catchError(stageResult: 'FAILURE') {
                        script {
                            wrap([$class: 'BuildUser']) {
                                dir('scylla-cluster-tests') {
                                    def test_config = groovy.json.JsonOutput.toJson(pipelineParams.test_config)

                                    sh """
                                    #!/bin/bash

                                    set -xe
                                    env

                                    export SCT_CONFIG_FILES=${test_config}

                                    echo "start collect logs ..."
                                    ./docker/env/hydra.sh collect-logs --logdir /sct --backend aws
                                    echo "end collect logs"
                                    """
                                }
                            }
                        }
                    }
                }
            }
            stage('Clean resources') {
                steps {
                    catchError(stageResult: 'FAILURE') {
                        script {
                            wrap([$class: 'BuildUser']) {
                                dir('scylla-cluster-tests') {
                                    def aws_region = groovy.json.JsonOutput.toJson(params.aws_region)
                                    def test_config = groovy.json.JsonOutput.toJson(pipelineParams.test_config)

                                    sh """
                                    #!/bin/bash

                                    set -xe
                                    env

                                    export SCT_CONFIG_FILES=${test_config}
                                    export SCT_REGION_NAME=${aws_region}
                                    export SCT_POST_BEHAVIOR_DB_NODES="${params.post_behavior_db_nodes}"
                                    export SCT_POST_BEHAVIOR_LOADER_NODES="${params.post_behavior_loader_nodes}"
                                    export SCT_POST_BEHAVIOR_MONITOR_NODES="${params.post_behavior_monitor_nodes}"

                                    echo "start clean resources ..."
                                    ./docker/env/hydra.sh clean-resources --logdir /sct --backend aws
                                    echo "end clean resources"
                                    """
                                }
                            }
                        }
                    }
                }
            }
            stage('Send email with result') {
                steps {
                    catchError(stageResult: 'FAILURE') {
                        script {
                            wrap([$class: 'BuildUser']) {
                                dir('scylla-cluster-tests') {
                                    def email_recipients = groovy.json.JsonOutput.toJson(params.email_recipients)

                                    sh """
                                    #!/bin/bash

                                    set -xe
                                    env

                                    echo "Start send email ..."
                                    ./docker/env/hydra.sh send-email --logdir /sct --email-recipients '${email_recipients}'
                                    echo "Email sent"
                                    """
                                }
                            }
                        }
                    }
                }
            }
        }
        post {
            always {
                script {
                    if (currentBuild.result != 'SUCCESS') {
                         wrap([$class: 'BuildUser']) {
                            mail to: 'siren@scylladb.com',
                                 subject: "SCT ${currentBuild.result}: Job ${env.JOB_NAME} ([${env.BUILD_NUMBER}])",
                                 body: "Check console output at ${env.BUILD_URL}"
                         }
                    }

                    if (pipelineParams.params.post_behavior_db_nodes == 'destroy') {
                        dir('siren-tests') {
                                sh '''
                                #!/bin/bash
                                set -xe
                                source /opt/rh/rh-python35/enable
                                ~/.local/bin/pipenv run ./runtests.py --untag-cluster=test_results/cluster_id.json
                                '''
                            }
                    }
                }
            }
        }
    }
}
