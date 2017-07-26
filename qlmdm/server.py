from base64 import b64encode
from collections import defaultdict
import datetime
from mongo_proxy import MongoProxy
import os
from pymongo import MongoClient
from tempfile import NamedTemporaryFile

from qlmdm import (
    load_settings,
    get_setting as main_get_setting,
    set_setting as main_set_setting,
    get_logger as main_get_logger,
    save_settings as main_save_settings,
    signatures_dir,
    get_selectors as main_get_selectors,
    encrypt_document as main_encrypt_document,
    gpg_command,
    top_dir,
)

db = None


def get_setting(setting, default=None, check_defaults=True):
    return main_get_setting(load_settings('server'), setting, default,
                            check_defaults)


def get_port_setting(port, setting, default=None):
    global_setting = get_setting(setting, default)
    settings_port = get_setting('port')
    if isinstance(settings_port, int) or isinstance(settings_port, list):
        return global_setting
    return main_get_setting(settings_port[port], setting, global_setting,
                            check_defaults=False)


def set_setting(setting, value):
    return main_set_setting(load_settings('server'), setting, value)


def save_settings():
    main_save_settings('server')


def get_logger(name):
    return main_get_logger(get_setting, name)


def get_db():
    global db

    if db:
        return db

    database_name = get_setting('database:name')
    host = get_setting('database:host')

    if not host:
        connection = MongoClient()
    else:
        if not isinstance(host, str):
            host = ','.join(host)
        kwargs = {}
        replicaset = get_setting('database:replicaset')
        if replicaset:
            kwargs['replicaset'] = replicaset
        connection = MongoClient(host, **kwargs)

    newdb = connection[database_name]

    username = get_setting('database:username')
    if username:
        password = get_setting('database:password')
        newdb.authenticate(username, password)

    db = MongoProxy(newdb)
    return db


def patch_hosts(patch_path, patch_mode=0o755, patch_content=b'', signed=True,
                hosts=None):
    db = get_db()
    all_hosts = db.clients.distinct('hostname')
    if hosts is None:
        hosts = all_hosts
    if isinstance(hosts, str):
        hosts = [hosts]
    bad_hosts = set(hosts) - set(all_hosts)
    if bad_hosts:
        if len(bad_hosts) > 1:
            s = 's'
            verb = 'are'
        else:
            s = ''
            verb = 'is'
        raise Exception('Host{s} {hosts} {verb} not in the database'.format(
            s=s, verb=verb, hosts=', '.join(sorted(bad_hosts))))
    conflict = db.patches.find_one({'files.path': patch_path,
                                    'pending_hosts': {'$in': hosts}})
    if conflict:
        conflicting_hosts = list(set(hosts) & set(conflict['pending_hosts']))
        conflicting_hosts.sort()
        raise Exception('Patch for {} conflicts with patch ID {} on hosts {}'.
                        format(patch_path, conflict['_id'], conflicting_hosts))

    # Somebody please explain to me why b64encode returns bytes rather than
    # str in python3, when the whole, entire prupose of b64encode is to turn
    # arbitrary bytes into ASCII. This is stupid.

    files = [
        {
            'path': patch_path,
            'mode': patch_mode,
            'content': b64encode(patch_content).decode('ascii'),
        },
    ]

    if signed:
        files.append({
            'path': os.path.join(signatures_dir, patch_path + '.sig'),
            'mode': 0o644,
            'content': b64encode(sign_data(patch_content)).decode('ascii'),
        })

    result = db.patches.insert_one({
        'submitted_at': datetime.datetime.utcnow(),
        'pending_hosts': hosts,
        'completed_hosts': [],
        'files': files,
    })
    return result.inserted_id


def open_issue(hostname, issue_name, as_of=None):
    """Opens an issue for the specified hostname if there isn't one"""
    db = get_db()
    spec = {'hostname': hostname, 'name': issue_name}
    if as_of:
        # Don't reopen an issue that was closed manually after we last
        # received data about it.
        spec['$or'] = [{'closed_at': {'$exists': False}},
                       {'closed_at': {'$gt': as_of}}]
    else:
        spec['closed_at'] = {'$exists': False}

    existing = db.issues.find_one(spec)
    if not existing:
        db.issues.insert_one({'hostname': hostname,
                              'name': issue_name,
                              'opened_at': datetime.datetime.utcnow()})


def close_issue(hostname, issue_name):
    """Closes any open issues for the specified host and issue name"""
    db = get_db()
    spec = {'closed_at': {'$exists': False}}
    if hostname:
        spec['hostname'] = hostname
    if issue_name:
        spec['name'] = issue_name
    ids = [d['_id'] for d in db.issues.find(spec)]
    if not ids:
        return ids
    db.issues.update_many(
        {'_id': {'$in': ids}},
        {'$set': {'closed_at': datetime.datetime.utcnow()}})
    return ids


def suspend_host(hostname):
    """Suspend client(s) until their next submission to the server

    `hostname` should be a single host name or an iterable of multiple host
    names.

    Returns the hostnames of the suspended clients, i.e., the clients that have
    records in the database and weren't already suspended."""

    db = get_db()

    if not hostname:
        raise Exception('Must specify hostname or list of hostnames')

    if isinstance(hostname, str):
        hostname_spec = hostname
    else:
        hostname_spec = {'$in': list(hostname)}

    spec = {'hostname': hostname_spec,
            '$or': [{'suspended': False},
                    {'suspended': {'$exists': False}}]}

    matches = {d['_id']: d['hostname'] for d in
               db.clients.find(spec, projection=['hostname'])}

    if not matches:
        return []

    db.clients.update_many(
        {'_id': {'$in': list(matches.keys())}},
        {'$set': {'suspended': True}})
    return list(matches.values())


def unsuspend_host(hostname):
    """Unsuspend client(s)

    `hostname` should be a single host name or an iterable of multiple host
    names.

    Returns the hostnames of the unsuspended clients.
    """

    db = get_db()

    if not hostname:
        raise Exception('Must specify hostname or list of hostnames')

    if isinstance(hostname, str):
        hostname_spec = hostname
    else:
        hostname_spec = {'$in': list(hostname)}

    spec = {'hostname': hostname_spec, 'suspended': True}

    matches = {d['_id']: d['hostname'] for d in
               db.clients.find(spec, projection=['hostname'])}

    if not matches:
        return []

    db.clients.update_many(
        {'_id': {'$in': list(matches.keys())}},
        {'$unset': {'suspended': True}})
    return list(matches.values())


def snooze_issue(hostname, issue_name, snooze_until):
    """Snooze any open issues for the specified host and issue name

    Returns the ids of the snoozed issues."""

    db = get_db()

    spec = {'closed_at': {'$exists': False},
            '$or': [{'unsnooze_at': {'$exists': False}},
                    {'unsnooze_at': {'$lt': snooze_until}}]}

    if hostname:
        spec['hostname'] = hostname

    if issue_name:
        spec['name'] = issue_name

    ids = [d['_id'] for d in db.issues.find(spec, projection=['_id'])]
    if not ids:
        return []

    db.issues.update_many(
        {'_id': {'$in': ids}},
        {'$set': {'snoozed_at': datetime.datetime.now(),
                  'unsnooze_at': snooze_until}})
    return ids


def unsnooze_issue(hostname, issue_name):
    """Unsnooze any snoozed issues for the specified host and issue name

    Returns the ids of the unsnoozed issues."""

    db = get_db()

    now = datetime.datetime.utcnow()
    spec = {'closed_at': {'$exists': False}, 'unsnooze_at': {'$gt': now}}

    if hostname:
        spec['hostname'] = hostname

    if issue_name:
        spec['name'] = issue_name

    ids = [d['_id'] for d in db.issues.find(spec, projection=['_id'])]
    if not ids:
        return []

    db.issues.update_many(
        {'_id': {'$in': ids}},
        {'$set': {'unsnoozed_at': now, 'unsnooze_at': now}})
    return ids


def get_open_issues(primary_key='host', hostname=None, issue_name=None,
                    include_suspended=False):
    """Returns a dictionary of matching open issues

    You can specify 'host' or 'issue' as the primary key. The secondary key is
    whichever one you don't specify."""

    if primary_key == 'host':
        primary_key = 'hostname'
        secondary_key = 'name'
    elif primary_key == 'issue':
        primary_key = 'name'
        secondary_key = 'hostname'
    else:
        raise Exception('Unrecognized primary key {}'.format(primary_key))

    issues = defaultdict(dict)
    db = get_db()
    spec = {'closed_at': {'$exists': False}}
    if hostname:
        spec['hostname'] = hostname
    if issue_name:
        spec['name'] = issue_name

    open_issues = db.issues.find(spec)
    if not include_suspended:
        suspended = {d['hostname'] for d in
                     db.clients.find({'suspended': True},
                                     projection={'_id': False,
                                                 'hostname': True})}
        open_issues = (i for i in open_issues
                       if i['hostname'] not in suspended)

    for issue in open_issues:
        issues[issue[primary_key]][issue[secondary_key]] = issue

    return dict(issues)


def get_selectors():
    return main_get_selectors(get_setting)


def encrypt_document(*args, **kwargs):
    return main_encrypt_document(get_setting, *args, **kwargs)


def sign_file(file, top_dir=top_dir):
    signature_file = os.path.join(top_dir, signatures_dir, file + '.sig')
    file = os.path.join(top_dir, file)
    os.makedirs(os.path.dirname(signature_file), exist_ok=True)
    gpg_command('--detach-sig', '-o', signature_file, file)
    return signature_file[len(top_dir)+1:]


def sign_data(data):
    with NamedTemporaryFile() as data_file, \
         NamedTemporaryFile() as signature_file:
        data_file.write(data)
        data_file.flush()
        gpg_command('--detach-sig', '-o', signature_file.name, data_file.name)
        return signature_file.read()


def audit_trail_write(tags, records):
    """Write records to the audit trail

    `tags` is a dictionary of key/value tags that should be added to every
    record. If `audited_at` isn't in the dictionary, it is added with a current
    timestamp.

    `records` is an iterator of records to write, or (if a dict) a single
    record.
    """

    if 'audited_at' not in tags:
        tags['audited_at'] = datetime.datetime.utcnow()

    if isinstance(records, dict):
        records = (records,)

    records = ({**tags, **r} for r in records)

    # This could potentially cause duplicate audit records to be inserted into
    # the database if there is an AutoReconnect error in the middle of
    # inserting, but that's an acceptable risk, since duplicate records can be
    # distinguished after the fact, for the sake of making this run faster.
    get_db().audit_trail.insert_many(records, ordered=False)
