"""Offline trust-boundary tests; no Azure or GitHub requests."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = 'a5089c4761577367f2d6136d5c86f459feb23b7f'
spec = importlib.util.spec_from_file_location('preview_validator', ROOT / 'scripts/validate-azure-preview.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class WorkflowLoader(yaml.SafeLoader):
    pass

WorkflowLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in entries if tag != 'tag:yaml.org,2002:bool']
    for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load(text):
    return yaml.load(text, Loader=WorkflowLoader)


CI = load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8-sig'))
PREVIEW = load((ROOT / '.github/workflows/azure-preview.yml').read_text(encoding='utf-8-sig'))


# Frozen semantic hashes of every unprivileged job at the verified base.
# Independent of Git objects/history; changing a command, matrix or dependency fails.
EXPECTED_CI_CONTRACT = {'on': '76ca36b1c3b6df3b203bf671d5ff389d6ac091a86b9e44dee250eb071509a6b6', 'jobs': {'contract': '932a01b0a3c447bf36267327e582efbab78866590e532681b29cdb4c6444c879', 'producer-sdk': '7896891ed2c705f854cf99295ab02b94ee873fbb288023de714b6de4ac2dda55', 'test': 'bd434c9453d9bd76f32647087c9452698a6fe93509e5fda746d00e6981c47dec', 'build': '30c6da52be9a9c30d18e05a90f0275eb053e5deaa3623cc60ae8d0a43299fd31', 'security': '28f340fafd8faddabea5647cc9908ce4b6aa43f780677147e55e6bec32a2cfcf'}}


def semantic_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def test_existing_unprivileged_ci_jobs_preserved():
    assert semantic_hash(CI['on']) == EXPECTED_CI_CONTRACT['on']
    for name, digest in EXPECTED_CI_CONTRACT['jobs'].items():
        assert semantic_hash(CI['jobs'][name]) == digest
    assert set(CI['jobs']) == set(EXPECTED_CI_CONTRACT['jobs']) | {'workflow-safety', 'gate'}


def test_job_contract_detects_changed_commands():
    import copy
    altered = copy.deepcopy(CI['jobs']['contract'])
    altered['steps'][-1]['run'] = 'true'
    assert semantic_hash(altered) != EXPECTED_CI_CONTRACT['jobs']['contract']


def test_job_contract_works_in_shallow_checkout(tmp_path):
    checkout = tmp_path / 'shallow'
    subprocess.run(['git', 'clone', '--depth', '1', '--no-checkout', ROOT.as_uri(), str(checkout)],
                   check=True, capture_output=True, text=True)
    assert (checkout / '.git/shallow').is_file()
    # Overlay the uncommitted security correction for this local regression only.
    for relative in ('.github/workflows/ci.yml', '.github/workflows/azure-preview.yml',
                     'scripts/validate-azure-preview.py', 'tests/workflow_security/test_manual_preview.py'):
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, destination)
    result = subprocess.run([sys.executable, '-m', 'pytest',
                             'tests/workflow_security/test_manual_preview.py::test_existing_unprivileged_ci_jobs_preserved',
                             '-q', '-p', 'no:cacheprovider'], cwd=checkout, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_azure_credentials_in_automatic_ci():
    assert CI['permissions'] == {'contents': 'read'}
    for job in CI['jobs'].values():
        assert job.get('permissions', {}).get('id-token', 'none') == 'none'
        for step in job.get('steps', []):
            assert not step.get('uses', '').startswith('azure/')
            assert 'az login' not in step.get('run', '')
            assert 'what-if' not in step.get('run', '')


def test_gate_covers_every_check_and_fails_closed():
    gate = CI['jobs']['gate']
    assert set(gate['needs']) == set(CI['jobs']) - {'gate'}
    assert gate['if'] == 'always()'
    assert 'all(.[]; .result == "success")' in gate['steps'][0]['run']


def test_manual_only_and_main_only():
    assert set(PREVIEW['on']) == {'workflow_dispatch'}
    for job in PREVIEW['jobs'].values():
        assert "github.ref == 'refs/heads/main'" in job['if']
        assert "github.repository == 'RameshMalar/scalable-incident-platform'" in job['if']
    inputs = PREVIEW['on']['workflow_dispatch']['inputs']
    assert set(inputs) == {'source_sha', 'review_reference', 'pr_number'}
    assert all(str(value['required']).lower() == 'true' for value in inputs.values())


def test_approval_and_job_permission_separation():
    compile_job, preview_job = (PREVIEW['jobs'][key] for key in ('compile', 'preview'))
    assert 'environment' not in compile_job
    assert compile_job['permissions'].get('id-token', 'none') == 'none'
    assert preview_job['environment'] == 'azure-preview'
    assert preview_job['needs'] == 'compile'
    assert preview_job['permissions']['id-token'] == 'write'
    assert [s for s in compile_job['steps'] if s.get('uses', '').startswith('azure/login')] == []
    steps = preview_job['steps']
    login = next(i for i, s in enumerate(steps) if s.get('uses', '').startswith('azure/login'))
    checks = '\n'.join(s.get('run', '') for s in steps[:login])
    assert all('validate-azure-preview.py ' + mode in checks for mode in ('protection', 'provenance', 'verify', 'identity'))
    credentials = steps[login]['with']
    assert all('vars.AZURE_PREVIEW_' in value for value in credentials.values())


def test_candidate_never_executed_and_artifact_is_same_run():
    text = (ROOT / '.github/workflows/azure-preview.yml').read_text()
    assert 'ref: ${{ github.sha }}' in text
    assert 'test "$(git -C candidate rev-parse HEAD)" = "$SOURCE_SHA"' in text
    assert 'build --no-restore candidate/infra/core.bicep' in text
    for job in PREVIEW['jobs'].values():
        for step in job['steps']:
            assert not step.get('uses', '').startswith('./')
            assert 'python candidate/' not in step.get('run', '')
            assert 'bash candidate/' not in step.get('run', '')
    download = next(s for s in PREVIEW['jobs']['preview']['steps'] if s.get('uses', '').startswith('actions/download-artifact'))
    assert download['with']['artifact-ids'] == '${{ needs.compile.outputs.artifact_id }}'
    assert not {'run-id', 'repository', 'github-token'} & set(download['with'])


def test_preview_has_no_deployment_or_raw_output_upload():
    steps = PREVIEW['jobs']['preview']['steps']
    commands = '\n'.join(s.get('run', '') for s in steps)
    assert 'az deployment group what-if' in commands
    assert '--validation-level ProviderNoRbac' in commands
    for forbidden in ('deployment group create', 'deployment sub create', '--confirm-with-what-if', '--debug', 'cat "$RUNNER_TEMP/preview', 'tee '):
        assert forbidden not in commands
    assert not any(s.get('uses', '').startswith('actions/upload-artifact') for s in steps)


@pytest.mark.parametrize('sha', ['', 'main', 'refs/heads/main', 'a5089c4', 'a' * 39, 'a' * 41, 'A' * 40, 'g' * 40, 'a' * 40 + '\n', 'a' * 40 + ';echo unsafe'])
def test_invalid_shas_rejected(sha):
    with pytest.raises(ValueError):
        validator.validate_inputs({'SOURCE_SHA': sha, 'REVIEW_REFERENCE': 'review-123', 'PR_NUMBER': '60'})


def test_full_sha_accepted():
    validator.validate_inputs({'SOURCE_SHA': BASE, 'REVIEW_REFERENCE': 'review-123', 'PR_NUMBER': '60'})


@pytest.mark.parametrize('reference', ['', ' ', 'line\nbreak', '\x00', 'a' * 501])
def test_invalid_review_rejected(reference):
    with pytest.raises(ValueError):
        validator.validate_inputs({'SOURCE_SHA': BASE, 'REVIEW_REFERENCE': reference, 'PR_NUMBER': '60'})


def protected():
    return ({'private': True}, {'can_admins_bypass': False, 'protection_rules': [{'type': 'required_reviewers', 'reviewers': [{'id': 1}], 'prevent_self_review': True}], 'deployment_branch_policy': {'custom_branch_policies': True, 'protected_branches': False}}, {'branch_policies': [{'type': 'branch', 'name': 'main'}]})


def test_protected_environment_accepted():
    validator.validate_protection(*protected())


@pytest.mark.parametrize('change', ['public', 'bypass', 'self', 'reviewers', 'policy', 'wildcard', 'tag', 'extra'])
def test_unprotected_environment_rejected(change):
    repo, env, policies = protected()
    if change == 'public': repo['private'] = False
    elif change == 'bypass': env['can_admins_bypass'] = True
    elif change == 'self': env['protection_rules'][0]['prevent_self_review'] = False
    elif change == 'reviewers': env['protection_rules'][0]['reviewers'] = []
    elif change == 'policy': env['deployment_branch_policy'] = None
    elif change == 'wildcard': policies['branch_policies'][0]['name'] = '*'
    elif change == 'tag': policies['branch_policies'][0]['type'] = 'tag'
    elif change == 'extra': policies['branch_policies'].append({'type': 'branch', 'name': 'feature/*'})
    with pytest.raises(ValueError): validator.validate_protection(repo, env, policies)


def identity():
    return {'AZURE_PREVIEW_CLIENT_ID': '11111111-1111-1111-1111-111111111111', 'AZURE_PREVIEW_TENANT_ID': '22222222-2222-2222-2222-222222222222', 'AZURE_PREVIEW_SUBSCRIPTION_ID': '33333333-3333-3333-3333-333333333333', 'PRODUCTION_CLIENT_ID': '44444444-4444-4444-4444-444444444444', 'AZURE_PREVIEW_RESOURCE_GROUP': 'rg-incident-platform', 'AZURE_PREVIEW_CONFIGURATION_APPROVED': 'true', 'AZURE_PREVIEW_DEVELOPER_PRINCIPAL_IDS': '[]'}


def test_dedicated_identity_accepted():
    validator.validate_identity(identity())


@pytest.mark.parametrize('change', ['production', 'missing', 'attestation', 'principals', 'scope'])
def test_bad_identity_rejected(change):
    env = identity()
    if change == 'production': env['AZURE_PREVIEW_CLIENT_ID'] = env['PRODUCTION_CLIENT_ID']
    elif change == 'missing': del env['AZURE_PREVIEW_TENANT_ID']
    elif change == 'attestation': env['AZURE_PREVIEW_CONFIGURATION_APPROVED'] = 'false'
    elif change == 'principals': env['AZURE_PREVIEW_DEVELOPER_PRINCIPAL_IDS'] = '["bad"]'
    elif change == 'scope': env['AZURE_PREVIEW_RESOURCE_GROUP'] = '../unsafe'
    with pytest.raises(ValueError): validator.validate_identity(env)


@pytest.mark.parametrize('resource', [{'type': 'Microsoft.Resources/deploymentScripts'}, {'type': 'Microsoft.Resources/deployments', 'properties': {'templateLink': {'uri': 'https://example.invalid'}}}])
def test_executable_or_linked_template_rejected(resource):
    with pytest.raises(ValueError): validator.validate_template({'resources': [resource]})


@pytest.mark.parametrize('tamper', [None, 'template', 'sha', 'reference', 'extra'])
def test_bundle_integrity(tmp_path, tamper):
    content = b'{"resources":[]}'
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / 'core.json').write_bytes(content)
    manifest = {'source_sha': BASE, 'template_hash': digest, 'review_reference': 'review', 'provenance': expected_provenance()}
    if tamper == 'template': (tmp_path / 'core.json').write_bytes(b'{"resources":[{}]}')
    if tamper == 'sha': manifest['source_sha'] = 'b' * 40
    if tamper == 'reference': manifest['review_reference'] = 'other'
    if tamper == 'extra': (tmp_path / 'unexpected.py').write_text('print("unsafe")')
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    if tamper:
        with pytest.raises(ValueError): validator.verify_bundle(tmp_path, BASE, digest, 'review', expected_provenance())
    else:
        validator.verify_bundle(tmp_path, BASE, digest, 'review', expected_provenance())

def test_candidate_compiler_config_rejected(tmp_path):
    infra = tmp_path / 'infra'
    infra.mkdir()
    (infra / 'core.bicep').write_text("targetScope = 'resourceGroup'")
    validator.validate_source(infra)
    (tmp_path / 'bicepconfig.json').write_text('{}')
    with pytest.raises(ValueError): validator.validate_source(infra)


@pytest.mark.parametrize('tamper', ['directory', 'oversized'])
def test_bundle_file_constraints(tmp_path, tamper):
    content = b'{"resources":[]}'
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / 'manifest.json').write_text(json.dumps({'source_sha': BASE, 'template_hash': digest, 'review_reference': 'review', 'provenance': expected_provenance()}))
    if tamper == 'directory': (tmp_path / 'core.json').mkdir()
    else: (tmp_path / 'core.json').write_bytes(b' ' * (validator.MAX_SIZE + 1))
    with pytest.raises(ValueError): validator.verify_bundle(tmp_path, BASE, digest, 'review', expected_provenance())


def test_summary_withholds_resource_details(tmp_path, monkeypatch):
    result = tmp_path / 'result.json'
    result.write_text(json.dumps({'status': 'Succeeded', 'changes': [{'changeType': 'Modify', 'resourceId': 'SENSITIVE-ID', 'delta': [{'after': 'SENSITIVE-VALUE'}]}]}))
    output = tmp_path / 'summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(output))
    monkeypatch.setenv('SOURCE_SHA', BASE)
    monkeypatch.setenv('TEMPLATE_HASH', 'a' * 64)
    validator.summary(result)
    text = output.read_text()
    assert 'Modify: 1' in text
    assert 'SENSITIVE' not in text


def test_summary_failed_preview_rejected(tmp_path):
    result = tmp_path / 'result.json'
    result.write_text('{"status":"Failed","changes":[]}')
    with pytest.raises(ValueError): validator.summary(result)


def test_existing_core_list_expression_is_data_only():
    validator.validate_template({'resources': [{'type': 'Microsoft.App/managedEnvironments', 'properties': {'sharedKey': "[listKeys(resourceId('Microsoft.OperationalInsights/workspaces', 'incident-logs'), '2022-10-01').primarySharedKey]"}}]})

def expected_provenance():
    return {'pr_number': 60, 'repository': validator.REPOSITORY, 'source_sha': BASE, 'base_branch': 'main'}


def valid_pr():
    return {'number': 60, 'state': 'open', 'merged': False,
            'head': {'sha': BASE, 'repo': {'full_name': validator.REPOSITORY}},
            'base': {'ref': 'main', 'repo': {'full_name': validator.REPOSITORY}}}


def test_same_repository_open_pr_accepted():
    assert validator.validate_pr(valid_pr(), '60', BASE) == expected_provenance()


@pytest.mark.parametrize('change', ['fork', 'missing_head_repo', 'base_repo', 'base_branch', 'closed', 'merged', 'sha', 'number', 'missing_sha'])
def test_invalid_pr_provenance_rejected(change):
    pr = valid_pr()
    if change == 'fork': pr['head']['repo']['full_name'] = 'other/fork'
    elif change == 'missing_head_repo': pr['head']['repo'] = None
    elif change == 'base_repo': pr['base']['repo']['full_name'] = 'other/fork'
    elif change == 'base_branch': pr['base']['ref'] = 'develop'
    elif change == 'closed': pr['state'] = 'closed'
    elif change == 'merged': pr['merged'] = True
    elif change == 'sha': pr['head']['sha'] = 'b' * 40
    elif change == 'number': pr['number'] = 61
    elif change == 'missing_sha': del pr['head']['sha']
    with pytest.raises(ValueError): validator.validate_pr(pr, '60', BASE)


@pytest.mark.parametrize('number', ['', '0', '-1', '060', '#60', '60/../61', 'https://github.com/other/repo/pull/60', '60;echo unsafe'])
def test_invalid_pr_references_rejected(number):
    with pytest.raises(ValueError):
        validator.validate_inputs({'SOURCE_SHA': BASE, 'PR_NUMBER': number, 'REVIEW_REFERENCE': 'review'})


def test_pr_move_after_compilation_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SOURCE_SHA', BASE)
    monkeypatch.setenv('PR_NUMBER', '60')
    monkeypatch.setenv('REVIEW_REFERENCE', 'review')
    monkeypatch.setattr(validator, 'api_read', lambda suffix: valid_pr())
    assert validator.provenance() == expected_provenance()
    pr = valid_pr()
    pr['head']['sha'] = 'b' * 40
    monkeypatch.setattr(validator, 'api_read', lambda suffix: pr)
    with pytest.raises(ValueError): validator.provenance()


def test_provenance_api_denial_fails_closed(tmp_path, monkeypatch):
    import urllib.error
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SOURCE_SHA', BASE)
    monkeypatch.setenv('PR_NUMBER', '60')
    monkeypatch.setenv('REVIEW_REFERENCE', 'review')
    def denied(suffix):
        raise urllib.error.HTTPError('https://api.github.com', 403, 'Forbidden', None, None)
    monkeypatch.setattr(validator, 'api_read', denied)
    with pytest.raises(urllib.error.HTTPError): validator.provenance()
    assert not (tmp_path / 'verified-provenance.json').exists()


def test_provenance_is_bound_into_manifest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SOURCE_SHA', BASE)
    monkeypatch.setenv('PR_NUMBER', '60')
    monkeypatch.setenv('REVIEW_REFERENCE', 'review')
    monkeypatch.setenv('GITHUB_OUTPUT', str(tmp_path / 'outputs'))
    (tmp_path / 'bundle').mkdir()
    (tmp_path / 'bundle/core.json').write_text('{"resources":[],"outputs":{"sensitive":{"value":"secret"}}}')
    (tmp_path / 'verified-provenance.json').write_text(json.dumps(expected_provenance()))
    validator.bundle()
    manifest = json.loads((tmp_path / 'bundle/manifest.json').read_text())
    assert manifest['provenance'] == expected_provenance()
    assert 'outputs' not in json.loads((tmp_path / 'bundle/core.json').read_text())
    validator.verify_bundle(tmp_path / 'bundle', BASE, manifest['template_hash'], 'review', expected_provenance())
    altered = expected_provenance()
    altered['pr_number'] = 61
    with pytest.raises(ValueError):
        validator.verify_bundle(tmp_path / 'bundle', BASE, manifest['template_hash'], 'review', altered)


def test_attempt_artifacts_are_distinct_and_download_by_id():
    steps = PREVIEW['jobs']['compile']['steps']
    upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact'))
    name = upload['with']['name']
    assert name == 'reviewed-azure-template-${{ github.run_attempt }}'
    assert name.replace('${{ github.run_attempt }}', '1') != name.replace('${{ github.run_attempt }}', '2')
    download = next(s for s in PREVIEW['jobs']['preview']['steps'] if s.get('uses', '').startswith('actions/download-artifact'))
    assert download['with']['artifact-ids'] == '${{ needs.compile.outputs.artifact_id }}'
    assert 'name' not in download['with']


def test_pr_permission_and_recheck_precedes_login():
    for name in ('compile', 'preview'):
        assert PREVIEW['jobs'][name]['permissions']['pull-requests'] == 'read'
    steps = PREVIEW['jobs']['preview']['steps']
    check = next(i for i, s in enumerate(steps) if 'validate-azure-preview.py provenance' in s.get('run', ''))
    login = next(i for i, s in enumerate(steps) if s.get('uses', '').startswith('azure/login'))
    assert check < login
    run = steps[check]['run']
    assert run.index('provenance') < run.index('verify')
    assert steps[check]['env']['PR_NUMBER'] == '${{ inputs.pr_number }}'


def test_security_files_do_not_match_existing_cd_filters():
    import fnmatch
    cd = load((ROOT / '.github/workflows/cd.yml').read_text(encoding='utf-8-sig'))
    files = ('.github/workflows/ci.yml', '.github/workflows/azure-preview.yml',
             '.github/workflows/AZURE_PREVIEW.md', 'scripts/validate-azure-preview.py',
             'tests/workflow_security/test_manual_preview.py')
    assert not any(fnmatch.fnmatch(path, pattern) for path in files for pattern in cd['on']['push']['paths'])
    assert fnmatch.fnmatch('infra/subscription.bicep', 'infra/**')


def test_job_contract_never_reads_historical_objects(monkeypatch):
    def unavailable(*args, **kwargs):
        raise AssertionError('Historical Git object access forbidden')
    monkeypatch.setattr(subprocess, 'check_output', unavailable)
    test_existing_unprivileged_ci_jobs_preserved()
