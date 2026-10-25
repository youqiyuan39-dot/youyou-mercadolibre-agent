"""Local checks for captured official estimates; never authorizes publication.

Hashes establish artifact integrity, not authenticity or extraction accuracy.
The caller supplies trusted expected inputs independently of the submitted quote.
No browser, account, network, draft write, or publication operation occurs here.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import copy
import hashlib
from pathlib import Path
import re

SOURCE_URL = 'https://global-selling.mercadolibre.com/listings/cost-calculator'
SOURCE_TYPE = 'MELI_OFFICIAL_REVENUE_CALCULATOR'
BINDING_KEYS = ('draft_name', 'sku_id', 'store_id', 'source_capture_sha256')
TEXT_INPUTS = ('origin_country', 'site_id', 'logistic_type', 'category_id',
               'listing_type_id', 'currency_id')
PACKAGE_KEYS = ('weight_g', 'length_cm', 'width_cm', 'height_cm')
FEES = ('selling_fee', 'international_shipping_fee', 'local_shipping_fee')
COST_KEYS = ('purchase', 'domestic_shipping', 'forwarder_service', 'packaging',
             'other', 'ads_reserve', 'tax_reserve', 'returns_reserve')
NOTICE = ('Artifact hashes prove integrity only, not official origin or accurate '
          'extraction. Source acquisition is not connected. Calculation only; '
          'this result is not a publication credential or realized profit.')


def _number(value, label, positive=False, cents=False):
    if value is None or isinstance(value, bool):
        raise ValueError(label + ': explicit decimal required')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(label + ': invalid decimal') from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(label + ': finite ' + ('positive' if positive else 'nonnegative') + ' value required')
    if result > Decimal('1000000000000'):
        raise ValueError(label + ': value exceeds supported range')
    if cents and result != result.quantize(Decimal('0.01')):
        raise ValueError(label + ': quote currency precision exceeds cents')
    return result


def _time(value, label):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise ValueError(label + ': invalid timestamp') from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(label + ': timezone required')
    return result.astimezone(timezone.utc)


def _inputs(value):
    if not isinstance(value, dict):
        raise ValueError('inputs: object required')
    required = set(TEXT_INPUTS) | {'category_path', 'listing_price', 'package', 'free_shipping'}
    if set(value) != required:
        raise ValueError('inputs: exact complete supported input fields required')
    result = {}
    for key in TEXT_INPUTS:
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError('inputs.' + key + ': nonempty string required')
        result[key] = value[key]
    for key, expected in {'origin_country': 'CN', 'site_id': 'MLM',
                          'logistic_type': 'remote', 'currency_id': 'USD'}.items():
        if result[key] != expected:
            raise ValueError('inputs.' + key + ': unsupported scope')
    path = value['category_path']
    if not isinstance(path, list) or not path or any(not isinstance(p, str) or not p.strip() for p in path):
        raise ValueError('inputs.category_path: complete category path required')
    result['category_path'] = list(path)
    if type(value['free_shipping']) is not bool:
        raise ValueError('inputs.free_shipping: explicit boolean required')
    result['free_shipping'] = value['free_shipping']
    result['listing_price'] = _number(value['listing_price'], 'listing_price', True, True)
    package = value['package']
    if not isinstance(package, dict) or set(package) != set(PACKAGE_KEYS):
        raise ValueError('inputs.package: all four package measurements required')
    result['package'] = {k: _number(package[k], 'package.' + k, True) for k in PACKAGE_KEYS}
    return result


def validate_quote(root, quote, expected, now=None, max_age_seconds=3600):
    """Return {ok, errors, quote?, publication_authorized: False}.

    expected must independently contain binding and every input field. Evidence
    paths must be relative to root and resolve within it (including symlinks).
    This validates recorded evidence, not whether a collector observed the UI.
    """
    result = {'ok': False, 'errors': [], 'publication_authorized': False,
              'source_authenticity_verified': False, 'authenticity_verified': False, 'notice': NOTICE}
    try:
        if not isinstance(quote, dict) or not isinstance(expected, dict):
            raise ValueError('quote and expected: objects required')
        if type(quote.get('schema_version')) is not int or quote['schema_version'] != 1:
            raise ValueError('schema_version: unsupported')
        if quote.get('source_type') != SOURCE_TYPE or quote.get('source_url') != SOURCE_URL:
            raise ValueError('source: official calculator source required')
        binding, wanted = quote.get('binding'), expected.get('binding')
        if not isinstance(binding, dict) or not isinstance(wanted, dict):
            raise ValueError('binding: independently supplied binding required')
        if set(binding) != set(BINDING_KEYS) or set(wanted) != set(BINDING_KEYS):
            raise ValueError('binding: exact complete binding required')
        if any(not isinstance(wanted[k], str) or not wanted[k].strip() for k in BINDING_KEYS):
            raise ValueError('binding: nonempty strings required')
        if not re.fullmatch('[a-fA-F0-9]{64}', wanted['source_capture_sha256']):
            raise ValueError('binding.source_capture_sha256: invalid hash')
        if binding != wanted:
            raise ValueError('binding: differs from expected')
        actual_inputs, expected_inputs = _inputs(quote.get('inputs')), _inputs(expected.get('inputs'))
        if actual_inputs != expected_inputs:
            raise ValueError('inputs: differs from expected')
        observed = _time(quote.get('observed_at'), 'observed_at')
        current = _time(now if now is not None else datetime.now(timezone.utc), 'now')
        ttl = _number(max_age_seconds, 'max_age_seconds', True)
        age = Decimal(str((current - observed).total_seconds()))
        if age < 0 or age > ttl:
            raise ValueError('observed_at: future or expired quote')
        artifacts = quote.get('evidence_files')
        if not isinstance(artifacts, list) or not artifacts:
            raise ValueError('evidence_files: original artifacts required')
        root = Path(root).resolve(strict=True)
        seen = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ValueError('evidence_files: invalid artifact')
            relative = artifact.get('path')
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                raise ValueError('evidence_files.path: relative path required')
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file() or path in seen:
                raise ValueError('evidence_files.path: outside root, duplicate, or not a file')
            seen.add(path)
            digest = artifact.get('sha256')
            if not isinstance(digest, str) or not re.fullmatch('[a-fA-F0-9]{64}', digest):
                raise ValueError('evidence_files.sha256: invalid hash')
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest.lower():
                raise ValueError('evidence_files.sha256: artifact changed')
        outputs = quote.get('outputs')
        if not isinstance(outputs, dict) or set(outputs) != set(FEES) | {'currency_id', 'net_proceeds'}:
            raise ValueError('outputs: exact complete fee breakdown required')
        if outputs['currency_id'] != 'USD':
            raise ValueError('outputs.currency_id: must be USD')
        amounts = {k: _number(outputs[k], 'outputs.' + k, k == 'net_proceeds', True)
                   for k in (*FEES, 'net_proceeds')}
        gross = actual_inputs['listing_price']
        if amounts['net_proceeds'] > gross:
            raise ValueError('net_proceeds: exceeds listing price')
        # Four independently cent-rounded terms can differ from gross by 2 cents.
        delta = gross - sum(amounts.values(), Decimal(0))
        if abs(delta) > Decimal('0.02'):
            raise ValueError('outputs: fees plus net do not reconcile with gross')
        result.update(ok=True, quote=copy.deepcopy(quote), checked_at=current.isoformat(),
                      age_seconds=str(age), reconciliation_difference_usd=str(delta),
                      status='RECORDED_ESTIMATE_VALIDATED', authenticity_verified=False)
    except (ValueError, TypeError, OSError, KeyError, InvalidOperation) as exc:
        result['errors'] = [str(exc)]
    return result


def evaluate_budget(validated_quote, costs_usd, policy):
    """Calculate an estimate from explicit USD costs, never a release credential.

    Costs exclude platform fees already deducted in the quote's net proceeds.
    Missing costs are unknown: callers must explicitly provide zero when valid.
    Reserve values are amounts, not percentages; no rates are silently invented.
    """
    result = {'ok': False, 'errors': [], 'publication_authorized': False,
              'source_authenticity_verified': False,
              'authenticity_verified': False,
              'status': 'CALCULATION_ONLY', 'notice': NOTICE}
    try:
        if not isinstance(validated_quote, dict) or validated_quote.get('ok') is not True or validated_quote.get('status') != 'RECORDED_ESTIMATE_VALIDATED':
            raise ValueError('validated_quote: successful local validation required')
        if not isinstance(costs_usd, dict) or set(costs_usd) != set(COST_KEYS):
            raise ValueError('costs_usd: every supported cost must be explicit; no platform fees')
        costs = {k: _number(costs_usd[k], 'costs_usd.' + k) for k in COST_KEYS}
        if not isinstance(policy, dict) or policy.get('margin_policy_status') != 'CONFIRMED' or policy.get('confirmed_margin_basis') != 'BUYER_PRICE' or policy.get('minimum_margin_operator') != 'GT':
            raise ValueError('policy: confirmed BUYER_PRICE GT required')
        if _number(policy.get('minimum_margin_pct'), 'minimum_margin_pct') != Decimal('10'):
            raise ValueError('policy: supported minimum is strictly greater than 10 percent')
        quote = validated_quote['quote']
        gross = _number(quote['inputs']['listing_price'], 'listing_price', True, True)
        net = _number(quote['outputs']['net_proceeds'], 'net_proceeds', True, True)
        if net > gross or quote['outputs']['currency_id'] != 'USD':
            raise ValueError('quote: invalid net proceeds or currency')
        # Do not turn permitted cent-rounding differences into extra margin.
        implied_net = gross - sum((_number(quote['outputs'][k], k, cents=True) for k in FEES), Decimal(0))
        if implied_net <= 0 or abs(implied_net - net) > Decimal('0.02'):
            raise ValueError('quote: fee reconciliation invalid')
        net = min(net, implied_net)
        contribution = net - sum(costs.values(), Decimal(0))
        # Compare the unrounded inequality; display rounding never drives release.
        passed = contribution * Decimal(100) > gross * Decimal(10)
        result.update(ok=True, contribution_usd=str(contribution),
                      buyer_price_usd=str(gross), net_proceeds_usd=str(net),
                      sales_margin_pct=str(contribution / gross * Decimal(100)),
                      margin_basis='BUYER_PRICE', minimum_margin_operator='GT',
                      minimum_margin_pct='10', floor_passed=passed,
                      decision='PASS_ESTIMATED_FLOOR' if passed else 'BELOW_OR_AT_FLOOR',
                      costs_usd={k: str(v) for k, v in costs.items()})
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        result['errors'] = [str(exc)]
    return result
