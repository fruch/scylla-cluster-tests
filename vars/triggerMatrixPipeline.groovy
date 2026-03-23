// vars/triggerMatrixPipeline.groovy
// Thin Groovy pipeline wrapper for Python-driven trigger matrix.
// All logic lives in sdcm/utils/trigger_matrix.py — this pipeline only
// sanitizes inputs, builds the CLI command, and executes it.
//
// Cron schedules are passed via the 'cron' parameter because Jenkins declarative
// pipeline triggers must be resolved before a node is allocated, and the Groovy
// sandbox blocks direct file I/O. Generate the cron spec from the matrix YAML with:
//   python3 -c "from sdcm.utils.trigger_matrix import get_parameterized_cron; print(get_parameterized_cron('path/to/matrix.yaml'))"
//
// NOTE: Either scylla_version or an image param is required. When an image is
//       provided without scylla_version, the Python code resolves the version from
//       the image's tags/labels. The resolved version is used for job folder
//       detection and passed to all triggered jobs.
//
// collect_report mode: Python outputs a JSON trigger plan, Groovy executes with
//   build(job:, wait: true, propagate: false) and aggregates results into a report.

import groovy.json.JsonSlurperClassic
import groovy.json.JsonOutput

def call(Map pipelineParams = [:]) {
    def matrixFile = pipelineParams.get('matrix_file', '')
    def cronSpec = pipelineParams.get('cron', '')

    pipeline {
        agent {
            label {
                label "aws-sct-builders-eu-west-1-v3-CI"
                retries 3
            }
        }

        environment {
            AWS_ACCESS_KEY_ID     = credentials('qa-aws-secret-key-id')
            AWS_SECRET_ACCESS_KEY = credentials('qa-aws-secret-access-key')
        }

        parameters {
            string(name: 'matrix_file', defaultValue: matrixFile,
                   description: 'Path to trigger matrix YAML file')
            string(name: 'scylla_version', defaultValue: '',
                   description: 'Scylla version (e.g., master:latest, 2024.2.5-0.20250221.xxx-1). If not provided, resolved from image params.')
            string(name: 'labels_selector', defaultValue: '',
                   description: 'Comma-separated labels to filter jobs (AND logic)')
            string(name: 'backend', defaultValue: '',
                   description: 'Filter jobs by backend (aws/gce/azure/oci). When image params are set, use this to run only matching backend jobs.')
            string(name: 'skip_jobs', defaultValue: '',
                   description: 'Comma-separated job names to skip')
            string(name: 'job_folder', defaultValue: '',
                   description: 'Override auto-detected Jenkins job folder')
            string(name: 'stress_duration', defaultValue: '',
                   description: 'Override stress duration for all jobs')
            string(name: 'region', defaultValue: '',
                   description: 'Override region for all jobs')
            string(name: 'availability_zone', defaultValue: '',
                   description: 'Override availability zone for all jobs')
            string(name: 'provision_type', defaultValue: '',
                   description: 'spot | on_demand | spot_fleet')
            string(name: 'scylla_ami_id', defaultValue: '',
                   description: 'Scylla AMI ID — passed to AWS jobs. Use with backend=aws to run only AWS jobs.')
            string(name: 'gce_image_db', defaultValue: '',
                   description: 'Scylla GCE image — passed to GCE jobs. Use with backend=gce to run only GCE jobs.')
            string(name: 'azure_image_db', defaultValue: '',
                   description: 'Scylla Azure image — passed to Azure jobs. Use with backend=azure to run only Azure jobs.')
            string(name: 'oci_image_db', defaultValue: '',
                   description: 'Scylla OCI image OCID — passed to OCI jobs. Use with backend=oci to run only OCI jobs.')
            string(name: 'unified_package', defaultValue: '',
                   description: 'URL to unified package for offline installer triggers (e.g. PGO builds)')
            booleanParam(name: 'nonroot_offline_install', defaultValue: false,
                   description: 'Install Scylla without required root privilege')
            string(name: 'requested_by_user', defaultValue: '',
                   description: 'User requesting the run')
            choice(choices: getBillingProjectChoices(),
                   description: 'Billing project for the test run (dynamically fetched from finops repository)',
                   name: 'billing_project')
            booleanParam(name: 'collect_report', defaultValue: false,
                   description: 'Wait for all triggered jobs to complete and generate a summary report')
            booleanParam(name: 'dry_run', defaultValue: false,
                   description: 'Preview mode — do not trigger jobs')
        }

        triggers {
            parameterizedCron(cronSpec)
        }

        options {
            timestamps()
            timeout(time: params.collect_report ? 1440 : 30, unit: 'MINUTES')
            buildDiscarder(logRotator(numToKeepStr: '30'))
        }

        stages {
            stage('Trigger Matrix') {
                when {
                    expression { !params.collect_report }
                }
                steps {
                    script {
                        def cmd = buildHydraCommand(params)
                        sh(cmd)
                    }
                }
            }

            stage('Plan Jobs') {
                when {
                    expression { params.collect_report }
                }
                steps {
                    script {
                        def cmd = buildHydraCommand(params, true)
                        def output = sh(script: cmd, returnStdout: true).trim()
                        def planLine = output.split('\n').find { it.startsWith('TRIGGER_PLAN_JSON=') }
                        if (!planLine) {
                            error("Failed to get trigger plan from Python. Output:\n${output}")
                        }
                        env.TRIGGER_PLAN_JSON = planLine.replace('TRIGGER_PLAN_JSON=', '')
                        def plan = new JsonSlurperClassic().parseText(env.TRIGGER_PLAN_JSON)
                        println("Trigger plan: ${plan.size()} jobs to execute with wait+report")
                    }
                }
            }

            stage('Execute and Collect') {
                when {
                    expression { params.collect_report }
                }
                steps {
                    script {
                        def plan = new JsonSlurperClassic().parseText(env.TRIGGER_PLAN_JSON)
                        if (!plan) {
                            println("No jobs in trigger plan. Nothing to execute.")
                            return
                        }

                        def results = Collections.synchronizedMap([:])
                        def parallelJobs = [:]

                        for (def spec in plan) {
                            def jobPath = spec.job_path
                            def jobParams = spec.parameters

                            parallelJobs[jobPath] = {
                                def jobResult = [
                                    job: jobPath,
                                    status: 'NOT_RUN',
                                    url: '',
                                ]
                                try {
                                    def buildParams = jobParams.collect { k, v ->
                                        string(name: k, value: v?.toString() ?: '')
                                    }
                                    println("Triggering: ${jobPath}")
                                    def triggered = build(
                                        job: jobPath,
                                        wait: true,
                                        propagate: false,
                                        parameters: buildParams,
                                    )
                                    jobResult.status = triggered.result ?: 'UNKNOWN'
                                    jobResult.url = triggered.absoluteUrl ?: ''
                                } catch (Exception e) {
                                    jobResult.status = 'FAILURE'
                                    jobResult.error = "Error triggering job: ${e.message}"
                                    println("Error triggering ${jobPath}: ${e.message}")
                                }
                                results[jobPath] = jobResult
                            }
                        }

                        parallel parallelJobs
                        env.COLLECT_RESULTS_JSON = JsonOutput.toJson(results)
                    }
                }
            }

            stage('Generate Report') {
                when {
                    expression { params.collect_report }
                }
                steps {
                    script {
                        def results = new JsonSlurperClassic().parseText(env.COLLECT_RESULTS_JSON)
                        def report = buildTextReport(results)
                        println(report)

                        def htmlReport = buildHtmlReport(results)
                        writeFile file: 'trigger-matrix-report.html', text: htmlReport

                        def hasFailures = results.values().any { it.status != 'SUCCESS' }
                        def passCount = results.count { k, v -> v.status == 'SUCCESS' }
                        currentBuild.description = "${hasFailures ? 'FAILED' : 'PASSED'} | ${passCount}/${results.size()} passed"

                        if (hasFailures) {
                            currentBuild.result = 'FAILURE'
                        }
                    }
                }
            }
        }

        post {
            always {
                script {
                    if (params.collect_report) {
                        archiveArtifacts artifacts: 'trigger-matrix-report.html', allowEmptyArchive: true
                    }
                }
                cleanWs()
            }
        }
    }
}

def buildHydraCommand(params, boolean planMode = false) {
    def safePattern = ~/^[a-zA-Z0-9_.:\-\/,\s]*$/
    // unified_package contains URLs with special chars — use a broader pattern
    def urlSafePattern = ~/^[a-zA-Z0-9_.:\-\/,\s?&=%+~#@!$()]*$/
    def paramChecks = [
        'matrix_file': params.matrix_file,
        'scylla_version': params.scylla_version,
        'labels_selector': params.labels_selector,
        'backend': params.backend,
        'skip_jobs': params.skip_jobs,
        'job_folder': params.job_folder,
        'stress_duration': params.stress_duration,
        'region': params.region,
        'availability_zone': params.availability_zone,
        'provision_type': params.provision_type,
        'scylla_ami_id': params.scylla_ami_id,
        'gce_image_db': params.gce_image_db,
        'azure_image_db': params.azure_image_db,
        'oci_image_db': params.oci_image_db,
        'requested_by_user': params.requested_by_user,
        'billing_project': params.billing_project,
    ]

    paramChecks.each { name, value ->
        if (value && !(value ==~ safePattern)) {
            error("Invalid characters in parameter '${name}': ${value}")
        }
    }

    if (params.unified_package?.trim() && !(params.unified_package ==~ urlSafePattern)) {
        error("Invalid characters in parameter 'unified_package': ${params.unified_package}")
    }

    if (!params.matrix_file?.trim()) {
        error("'matrix_file' parameter is required")
    }
    def hasImageParam = params.scylla_ami_id?.trim() || params.gce_image_db?.trim() || params.azure_image_db?.trim() || params.oci_image_db?.trim()
    if (!params.scylla_version?.trim() && !hasImageParam && !params.unified_package?.trim()) {
        error("Either 'scylla_version', an image param, or 'unified_package' is required")
    }

    def cmd = "./docker/env/hydra.sh trigger-matrix"
    cmd += " --matrix '${params.matrix_file}'"
    if (params.scylla_version?.trim()) {
        cmd += " --scylla-version '${params.scylla_version}'"
    }
    if (params.job_folder?.trim()) {
        cmd += " --job-folder '${params.job_folder}'"
    }
    if (params.labels_selector?.trim()) {
        cmd += " --labels-selector '${params.labels_selector}'"
    }
    if (params.backend?.trim()) {
        cmd += " --backend '${params.backend}'"
    }
    if (params.skip_jobs?.trim()) {
        cmd += " --skip-jobs '${params.skip_jobs}'"
    }
    if (params.stress_duration?.trim()) {
        cmd += " --stress-duration '${params.stress_duration}'"
    }
    if (params.region?.trim()) {
        cmd += " --region '${params.region}'"
    }
    if (params.availability_zone?.trim()) {
        cmd += " --availability-zone '${params.availability_zone}'"
    }
    if (params.provision_type?.trim()) {
        cmd += " --provision-type '${params.provision_type}'"
    }
    if (params.scylla_ami_id?.trim()) {
        cmd += " --scylla-ami-id '${params.scylla_ami_id}'"
    }
    if (params.gce_image_db?.trim()) {
        cmd += " --gce-image-db '${params.gce_image_db}'"
    }
    if (params.azure_image_db?.trim()) {
        cmd += " --azure-image-db '${params.azure_image_db}'"
    }
    if (params.oci_image_db?.trim()) {
        cmd += " --oci-image-db '${params.oci_image_db}'"
    }
    if (params.unified_package?.trim()) {
        cmd += " --unified-package '${params.unified_package}'"
    }
    if (params.nonroot_offline_install) {
        cmd += " --nonroot-offline-install true"
    }
    if (params.billing_project?.trim()) {
        cmd += " --billing-project '${params.billing_project}'"
    }
    if (params.requested_by_user?.trim()) {
        cmd += " --requested-by-user '${params.requested_by_user}'"
    }
    if (params.dry_run) {
        cmd += " --dry-run"
    }
    if (planMode) {
        cmd += " --collect-report"
    }

    return cmd
}

def buildTextReport(Map results) {
    def report = new StringBuilder()
    report.append('=' * 80 + '\n')
    report.append("TRIGGER MATRIX REPORT\n")
    report.append('=' * 80 + '\n\n')

    def passCount = results.count { k, v -> v.status == 'SUCCESS' }
    def totalCount = results.size()
    def hasFailures = results.values().any { it.status != 'SUCCESS' }
    report.append("Overall: ${hasFailures ? 'FAILED' : 'PASSED'} (${passCount}/${totalCount} passed)\n\n")

    results.each { jobPath, result ->
        def icon = result.status == 'SUCCESS' ? '[PASS]' : '[FAIL]'
        report.append("${icon} ${jobPath}")
        if (result.url) {
            report.append(" -> ${result.url}")
        }
        report.append('\n')
        if (result.error) {
            report.append("      Error: ${result.error}\n")
        }
    }

    report.append('\n' + '=' * 80 + '\n')
    return report.toString()
}

def buildHtmlReport(Map results) {
    def hasFailures = results.values().any { it.status != 'SUCCESS' }
    def passCount = results.count { k, v -> v.status == 'SUCCESS' }
    def totalCount = results.size()
    def overallStatus = hasFailures ? 'FAILED' : 'PASSED'
    def overallColor = hasFailures ? '#d32f2f' : '#388e3c'

    def html = new StringBuilder()
    html.append('''<!DOCTYPE html>
<html><head><style>
  body { font-family: Arial, sans-serif; margin: 20px; color: #333; }
  h1 { color: #1a237e; border-bottom: 2px solid #1a237e; padding-bottom: 8px; }
  .summary { margin: 16px 0; padding: 12px 16px; background: #f5f5f5; border-radius: 6px; }
  .status-badge { display: inline-block; padding: 4px 12px; border-radius: 4px; color: #fff; font-weight: bold; }
  table { border-collapse: collapse; margin: 20px 0; width: 100%; }
  th, td { border: 1px solid #ccc; padding: 8px 14px; text-align: left; }
  th { background: #1a237e; color: #fff; }
  tr:nth-child(even) { background: #fafafa; }
  .pass { color: #388e3c; font-weight: bold; }
  .fail { color: #d32f2f; font-weight: bold; }
</style></head><body>
<h1>Trigger Matrix Report</h1>
''')

    html.append("""<div class="summary">
  <strong>Overall:</strong> <span class="status-badge" style="background:${overallColor}">${overallStatus}</span>
  &mdash; ${passCount}/${totalCount} passed
</div>
""")

    html.append('<table><tr><th>Job</th><th>Status</th><th>Link</th></tr>\n')
    results.each { jobPath, result ->
        def cssClass = result.status == 'SUCCESS' ? 'pass' : 'fail'
        def link = result.url ? "<a href=\"${result.url}\">View</a>" : 'N/A'
        html.append("<tr><td>${jobPath}</td><td class=\"${cssClass}\">${result.status}</td><td>${link}</td></tr>\n")
    }
    html.append('</table>\n')

    if (hasFailures) {
        html.append('<h2 style="color:#d32f2f">Failures</h2>\n')
        results.findAll { k, v -> v.status != 'SUCCESS' }.each { jobPath, result ->
            html.append("<p><strong>${jobPath}</strong> &mdash; ${result.status}")
            if (result.error) {
                html.append("<br/><small>${result.error}</small>")
            }
            html.append('</p>\n')
        }
    }

    html.append('</body></html>')
    return html.toString()
}
