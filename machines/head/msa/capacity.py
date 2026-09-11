"""Unpaid capacity-wait allowance; never a model or paid-worker timeout."""
import os
import re


DEFAULT_WAIT_SECONDS = 7200
MAX_WAIT_SECONDS = 7200


def wait_seconds(value=None, *, environ=None):
    if value is None:
        value = (os.environ if environ is None else environ).get(
            'BIO_MSA_CAPACITY_WAIT_SECONDS', str(DEFAULT_WAIT_SECONDS))
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,6}', value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= MAX_WAIT_SECONDS:
        raise ValueError('Capacity wait must be whole seconds from 0 to 7200')
    return value


def preparation_allowance(prepared):
    if prepared.get('msa_backend') != 'private' or not prepared.get('msa_applicable', False):
        return 0
    environment = dict(os.environ, **prepared.get('environment', {}))
    return wait_seconds(environ=environment)


if __name__ == '__main__':
    print(wait_seconds())
