#!/usr/bin/env python3
"""Read-only compatibility gate. No polling, service control or secret output."""
import argparse
import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location('lane_contract', Path(__file__).with_name('configure-gsales-mac-lane.py'))
lane = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lane)


def verify_contract(values, gpoint_tenant, gsales_tenant, legacy_tenant=None, expected_credentials=None):
    for expected in (gpoint_tenant, gsales_tenant):
        if str(UUID(expected)) != expected or UUID(expected).int == 0:
            raise RuntimeError('worker_tenant_contract_invalid')
    if gpoint_tenant == gsales_tenant:
        raise RuntimeError('worker_lane_tenants_not_distinct')
    for key in ('GPOINT_AGATA_WORKER_TOKEN', 'GSALES_AGATA_WORKER_TOKEN'):
        if not values.get(key):
            raise RuntimeError('worker_lane_auth_config_missing')
    if expected_credentials is not None:
        if (values['GPOINT_AGATA_WORKER_TOKEN'] != expected_credentials.get('GPOINT_AGATA_WORKER_TOKEN')
                or values['GSALES_AGATA_WORKER_TOKEN'] != expected_credentials.get('GSALES_WORKER_TOKEN')):
            raise RuntimeError('worker_lane_auth_config_mismatch')
    if values['GPOINT_AGATA_WORKER_TOKEN'] == values['GSALES_AGATA_WORKER_TOKEN']:
        raise RuntimeError('worker_lane_credentials_not_distinct')
    if values.get('GPOINT_AGATA_WORKER_URL', 'https://gpoint.one/api/agata-worker').rstrip('/') != 'https://gpoint.one/api/agata-worker':
        raise RuntimeError('worker_gpoint_protocol_contract_mismatch')
    if values.get('GSALES_AGATA_WORKER_URL', '').rstrip('/') != lane.URL:
        raise RuntimeError('worker_gsales_protocol_contract_mismatch')
    if values.get('GPOINT_AGATA_TENANT_ID') or legacy_tenant:
        if (values.get('GPOINT_AGATA_TENANT_ID') or legacy_tenant) != gpoint_tenant:
            raise RuntimeError('worker_gpoint_tenant_contract_mismatch')
    else:
        raise RuntimeError('worker_gpoint_tenant_contract_missing')
    if values.get('GSALES_AGATA_TENANT_ID') != gsales_tenant:
        raise RuntimeError('worker_gsales_tenant_contract_mismatch')
    return {'compatibility': 'PASS'}


def contract_evidence(values, gpoint_tenant, gsales_tenant, expected_credentials):
    point_url = values.get('GPOINT_AGATA_WORKER_URL', 'https://gpoint.one/api/agata-worker').rstrip('/')
    sales_url = values.get('GSALES_AGATA_WORKER_URL', '').rstrip('/')
    return {
        'protocols': {'GPOINT': point_url, 'GSALES': sales_url},
        'tenants': {'GPOINT': gpoint_tenant, 'GSALES': gsales_tenant},
        'lanes': {
            'GPOINT': 'CONFIGURED' if values.get('GPOINT_AGATA_WORKER_TOKEN') else 'MISSING',
            'GSALES': 'CONFIGURED' if values.get('GSALES_AGATA_WORKER_TOKEN') else 'MISSING',
        },
        'credentialParity': {
            'GPOINT': 'MATCH' if values.get('GPOINT_AGATA_WORKER_TOKEN') == expected_credentials.get('GPOINT_AGATA_WORKER_TOKEN') else 'MISMATCH',
            'GSALES': 'MATCH' if values.get('GSALES_AGATA_WORKER_TOKEN') == expected_credentials.get('GSALES_WORKER_TOKEN') else 'MISMATCH',
            'distinct': 'YES' if values.get('GPOINT_AGATA_WORKER_TOKEN') != values.get('GSALES_AGATA_WORKER_TOKEN') else 'NO',
        },
    }


# These values bypass load_env_file or select another config/support directory.
CONTRACT_ENV_KEYS = ('HOME', 'GPOINT_AGATA_WORKER_CONFIG', 'GPOINT_AGATA_WORKER_SUPPORT',
    'GPOINT_AGATA_WORKER_LOCAL_TEST', 'GPOINT_AGATA_WORKER_URL', 'GPOINT_AGATA_WORKER_TOKEN',
    'GPOINT_AGATA_TENANT_ID', 'GSALES_AGATA_WORKER_URL', 'GSALES_AGATA_WORKER_TOKEN',
    'GSALES_AGATA_TENANT_ID')


def verify_launch_environment(plist_env, inherited_env):
    canonical = {'HOME': str(lane.HOME), 'GPOINT_AGATA_WORKER_CONFIG': str(lane.CONFIG),
                 'GPOINT_AGATA_WORKER_SUPPORT': str(lane.SUPPORT)}
    for environment in (inherited_env, plist_env):
        if not isinstance(environment, dict):
            raise RuntimeError('worker_launch_environment_invalid')
        for key in CONTRACT_ENV_KEYS:
            value = environment.get(key)
            if key in environment and (environment is plist_env or value not in (None, '')) and (key not in canonical or value != canonical[key]):
                raise RuntimeError('worker_launch_contract_override')


def inspect_launch_environment():
    plist = plistlib.loads(lane.private_file(lane.HOME / 'Library/LaunchAgents' / (lane.LABEL + '.plist')))
    inherited = {}
    for key in CONTRACT_ENV_KEYS:
        result = subprocess.run(['/bin/launchctl', 'getenv', key], capture_output=True, text=True, timeout=2)
        if result.returncode:
            raise RuntimeError('worker_launch_environment_unproven')
        inherited[key] = result.stdout.strip()
    verify_launch_environment(plist.get('EnvironmentVariables', {}), inherited)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--gpoint-tenant', required=True)
    parser.add_argument('--gsales-tenant', required=True)
    parser.add_argument('--mode', choices=('preflight', 'apply'), default='apply')
    args = parser.parse_args()
    source = lane.verify_candidate(args.candidate, allow_source_drift=True)
    if args.mode == 'apply' and source['sourceStatus'] != 'EXACT_MATCH':
        raise RuntimeError('installed_worker_candidate_hash_mismatch')
    inspect_launch_environment()
    values = lane.parse_env(lane.private_file(lane.CONFIG).decode())
    legacy = None
    if not values.get('GPOINT_AGATA_TENANT_ID'):
        # Reuse unchanged worker's source-verified legacy binding reader.
        sys.path.insert(0, str(lane.SUPPORT))
        worker_spec = importlib.util.spec_from_file_location('installed_worker', lane.SUPPORT / 'gpoint_mac_worker.py')
        worker = importlib.util.module_from_spec(worker_spec)
        sys.modules[worker_spec.name] = worker
        worker_spec.loader.exec_module(worker)
        legacy = worker._legacy_gpoint_tenant_binding()
    credentials = json.loads(sys.stdin.read())
    if not isinstance(credentials, dict) or set(credentials) != {'GPOINT_AGATA_WORKER_TOKEN', 'GSALES_WORKER_TOKEN'}:
        raise RuntimeError('expected_credentials_missing')
    result = verify_contract(values, args.gpoint_tenant, args.gsales_tenant, legacy, credentials)
    print(json.dumps({**result, 'candidateSha': args.candidate, 'sourceStatus': source['sourceStatus'],
        'assets': source['assets'], 'contract': contract_evidence(values, args.gpoint_tenant,
        args.gsales_tenant, credentials)}))


if __name__ == '__main__':
    safe_reasons = {
        'worker_tenant_contract_invalid', 'worker_lane_tenants_not_distinct',
        'worker_lane_auth_config_missing', 'worker_lane_auth_config_mismatch',
        'worker_lane_credentials_not_distinct', 'worker_gpoint_protocol_contract_mismatch',
        'worker_gsales_protocol_contract_mismatch', 'worker_gpoint_tenant_contract_mismatch',
        'worker_gpoint_tenant_contract_missing', 'worker_gsales_tenant_contract_mismatch',
        'worker_launch_environment_invalid', 'worker_launch_contract_override',
        'candidate_checkout_not_clean_exact_sha', 'installed_worker_asset_invalid',
        'installed_worker_candidate_hash_mismatch', 'installed_launchagent_identity_invalid',
        'protected_file_permissions_invalid', 'worker_launch_environment_unproven',
        'expected_credentials_missing',
    }
    try:
        main()
    except Exception as exc:
        reason = str(exc) if str(exc) in safe_reasons else 'worker_contract_check_failed'
        print(f'WORKER_COMPATIBILITY_BLOCKED reason={reason}', file=sys.stderr)
        sys.exit(1)
