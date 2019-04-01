pipeline {
  agent {
    label {
      label "aws-scylla-qa-builder1"
    }
  }
  options {
      timestamps()
      timeout(time: 1, unit: 'HOURS')
      buildDiscarder(logRotator(numToKeepStr: '10'))
  }
  stages {
    stage("precommit") {
      steps {
        script {
            sh './docker/env/hydra.sh bash -c "cd /sct; pre-commit run -a"'
            pullRequest.createStatus(status: 'success',
                             context: 'jenkins/precommit',
                             description: 'Precommit passed',
                             targetUrl: "${env.JOB_URL}/workflow-stage")
        }
      }
    }
    stage("unittest") {
      steps {
        script {
            sh './docker/env/hydra.sh python sdcm/sct_config.py'
            pullRequest.createStatus(status: 'success',
                             context: 'jenkins/unittests',
                             description: 'All unit test passed',
                             targetUrl: "${env.JOB_URL}/workflow-stage")
        }
      }
    }
    stage("test microbenchmarking,py") {
      steps {
        script {
            sh './docker/env/hydra.sh python sdcm/microbenchmarking.py --help'
            pullRequest.createStatus(status: 'success',
                             context: 'jenkins/microbenchmarking',
                             description: 'microbenchmarking.py is runnable',
                             targetUrl: "${env.JOB_URL}/workflow-stage")
        }
      }
    }
  }
}
