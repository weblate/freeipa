# Authors:
#   Pavel Zuna <pzuna@redhat.com>
#
# Copyright (C) 2009  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import re
import ldap as _ldap

from ipalib import api, errors, output
from ipalib import Command, List, Password, Str, Flag, StrEnum
from ipalib.cli import to_cli
if api.env.in_server and api.env.context in ['lite', 'server']:
    try:
        from ipaserver.plugins.ldap2 import ldap2
    except StandardError, e:
        raise e
from ipalib import _

__doc__ = _("""
Migration to IPA

Migrate users and groups from an LDAP server to IPA.

This performs an LDAP query against the remote server searching for
users and groups in a container. In order to migrate passwords you need
to bind as a user that can read the userPassword attribute on the remote
server. This is generally restricted to high-level admins such as
cn=Directory Manager in 389-ds (this is the default bind user).

The default user container is ou=People.

The default group container is ou=Groups.

Users and groups that already exist on the IPA server are skipped.

Two LDAP schemas define how group members are stored: RFC2307 and
RFC2307bis. RFC2307bis uses member and uniquemember to specify group
members, RFC2307 uses memberUid. The default schema is RFC2307bis.

Migrated users do not have Kerberos credentials, they have only their
LDAP password. To complete the migration process, users need to go
to http://ipa.example.com/ipa/migration and authenticate using their
LDAP password in order to generate their Kerberos credentials.

Migration is disabled by default. Use the command ipa config-mod to
enable it:

 ipa config-mod --enable-migration=TRUE

EXAMPLES:

 The simplest migration, accepting all defaults:
   ipa migrate-ds ldap://ds.example.com:389

 Specify the user and group container. This can be used to migrate user and
 group data from an IPA v1 server:
   ipa migrate-ds --user-container='cn=users,cn=accounts' --group-container='cn=groups,cn=accounts' ldap://ds.example.com:389
""")

# USER MIGRATION CALLBACKS AND VARS

_krb_err_msg = _('Kerberos principal %s already exists. Use \'ipa user-mod\' to set it manually.')
_grp_err_msg = _('Failed to add user to the default group. Use \'ipa group-add-member\' to add manually.')
_ref_err_msg = _('Migration of LDAP search reference is not supported.')

_supported_schemas = (u'RFC2307bis', u'RFC2307')


def _pre_migrate_user(ldap, pkey, dn, entry_attrs, failed, config, ctx, **kwargs):
    attr_blacklist = ['krbprincipalkey','memberofindirect','memberindirect']
    attr_blacklist.extend(kwargs.get('attr_blacklist', []))

    # get default primary group for new users
    if 'def_group_dn' not in ctx:
        def_group = config.get('ipadefaultprimarygroup')
        ctx['def_group_dn'] = api.Object.group.get_dn(def_group)
        try:
            (g_dn, g_attrs) = ldap.get_entry(ctx['def_group_dn'], ['gidnumber'])
        except errors.NotFound:
            error_msg = 'Default group for new users not found.'
            raise errors.NotFound(reason=error_msg)
        ctx['def_group_gid'] = g_attrs['gidnumber'][0]

    # fill in required attributes by IPA
    entry_attrs['ipauniqueid'] = 'autogenerate'
    if 'homedirectory' not in entry_attrs:
        homes_root = config.get('ipahomesrootdir', ('/home', ))[0]
        home_dir = '%s/%s' % (homes_root, pkey)
        home_dir = home_dir.replace('//', '/').rstrip('/')
        entry_attrs['homedirectory'] = home_dir
    entry_attrs.setdefault('gidnumber', ctx['def_group_gid'])

    # do not migrate all attributes
    for attr in entry_attrs.keys():
        if attr in attr_blacklist:
            del entry_attrs[attr]

    # do not migrate all object classes
    if 'objectclass' in entry_attrs:
        for object_class in kwargs.get('oc_blacklist', []):
            try:
                entry_attrs['objectclass'].remove(object_class)
            except ValueError:  # object class not present
                pass

    # generate a principal name and check if it isn't already taken
    principal = u'%s@%s' % (pkey, api.env.realm)
    try:
        ldap.find_entry_by_attr(
            'krbprincipalname', principal, 'krbprincipalaux', ['']
        )
    except errors.NotFound:
        entry_attrs['krbprincipalname'] = principal
    else:
        failed[pkey] = unicode(_krb_err_msg % principal)

    return dn


def _post_migrate_user(ldap, pkey, dn, entry_attrs, failed, config, ctx):
    # add user to the default group
    try:
        ldap.add_entry_to_group(dn, ctx['def_group_dn'])
    except errors.ExecutionError, e:
        failed[pkey] = unicode(_grp_err_msg)


# GROUP MIGRATION CALLBACKS AND VARS

def _pre_migrate_group(ldap, pkey, dn, entry_attrs, failed, config, ctx, **kwargs):
    def convert_members_rfc2307bis(member_attr, search_bases, overwrite=False):
        """
        Convert DNs in member attributes to work in IPA.
        """
        new_members = []
        entry_attrs.setdefault(member_attr, [])
        for m in entry_attrs[member_attr]:
            try:
                # what str2dn returns looks like [[('cn', 'foo', 4)], [('dc', 'example', 1)], [('dc', 'com', 1)]]
                rdn = _ldap.dn.str2dn(m ,flags=_ldap.DN_FORMAT_LDAPV3)[0]
                rdnval = rdn[0][1]
            except IndexError:
                api.log.error('Malformed DN %s has no RDN?' % m)
                continue

            if m.lower().endswith(search_bases['user']):
                api.log.info('migrating user %s' % m)
                m = '%s=%s,%s' % (api.Object.user.primary_key.name,
                                  rdnval,
                                  api.env.container_user)
            elif m.lower().endswith(search_bases['group']):
                api.log.info('migrating group %s' % m)
                m = '%s=%s,%s' % (api.Object.group.primary_key.name,
                                  rdnval,
                                  api.env.container_group)
            else:
                api.log.error('entry %s does not belong into any known container' % m)
                continue

            m = ldap.normalize_dn(m)
            new_members.append(m)

        del entry_attrs[member_attr]
        if overwrite:
            entry_attrs['member'] = []
        entry_attrs['member'] += new_members

    def convert_members_rfc2307(member_attr):
        """
        Convert usernames in member attributes to work in IPA.
        """
        new_members = []
        entry_attrs.setdefault(member_attr, [])
        for m in entry_attrs[member_attr]:
            memberdn = '%s=%s,%s' % (api.Object.user.primary_key.name,
                                     m,
                                     api.env.container_user)
            new_members.append(ldap.normalize_dn(memberdn))
        entry_attrs['member'] = new_members

    attr_blacklist = ['memberofindirect','memberindirect']
    attr_blacklist.extend(kwargs.get('attr_blacklist', []))

    schema = kwargs.get('schema', None)
    entry_attrs['ipauniqueid'] = 'autogenerate'
    if schema == 'RFC2307bis':
        search_bases = kwargs.get('search_bases', None)
        if not search_bases:
            raise ValueError('Search bases not specified')

        convert_members_rfc2307bis('member', search_bases, overwrite=True)
        convert_members_rfc2307bis('uniquemember', search_bases)
    elif schema == 'RFC2307':
        convert_members_rfc2307('memberuid')
    else:
        raise ValueError('Schema %s not supported' % schema)

    # do not migrate all attributes
    for attr in entry_attrs.keys():
        if attr in attr_blacklist:
            del entry_attrs[attr]

    # do not migrate all object classes
    if 'objectclass' in entry_attrs:
        for object_class in kwargs.get('oc_blacklist', []):
            try:
                entry_attrs['objectclass'].remove(object_class)
            except ValueError:  # object class not present
                pass

    return dn


# DS MIGRATION PLUGIN

def construct_filter(template, oc_list):
    oc_subfilter = ''.join([ '(objectclass=%s)' % oc for oc in oc_list])
    return template % oc_subfilter

def validate_ldapuri(ugettext, ldapuri):
    m = re.match('^ldaps?://[-\w\.]+(:\d+)?$', ldapuri)
    if not m:
        err_msg = _('Invalid LDAP URI.')
        raise errors.ValidationError(name='ldap_uri', error=err_msg)


class migrate_ds(Command):
    __doc__ = _('Migrate users and groups from DS to IPA.')

    migrate_objects = {
        # OBJECT_NAME: (search_filter, pre_callback, post_callback)
        #
        # OBJECT_NAME - is the name of an LDAPObject subclass
        # search_filter - is the filter to retrieve objects from DS
        # pre_callback - is called for each object just after it was
        #                retrieved from DS and before being added to IPA
        # post_callback - is called for each object after it was added to IPA
        #
        # {pre, post}_callback parameters:
        #  ldap - ldap2 instance connected to IPA
        #  pkey - primary key value of the object (uid for users, etc.)
        #  dn - dn of the object as it (will be/is) stored in IPA
        #  entry_attrs - attributes of the object
        #  failed - a list of so-far failed objects
        #  config - IPA config entry attributes
        #  ctx - object context, used to pass data between callbacks
        #
        # If pre_callback return value evaluates to False, migration
        # of the current object is aborted.
        'user': {
            'filter_template' : '(&(|%s)(uid=*))',
            'oc_option' : 'userobjectclass',
            'oc_blacklist_option' : 'userignoreobjectclass',
            'attr_blacklist_option' : 'userignoreattribute',
            'pre_callback' : _pre_migrate_user,
            'post_callback' : _post_migrate_user
        },
        'group': {
            'filter_template' : '(&(|%s)(cn=*))',
            'oc_option' : 'groupobjectclass',
            'oc_blacklist_option' : 'groupignoreobjectclass',
            'attr_blacklist_option' : 'groupignoreattribute',
            'pre_callback' : _pre_migrate_group,
            'post_callback' : None
        },
    }
    migrate_order = ('user', 'group')

    takes_args = (
        Str('ldapuri', validate_ldapuri,
            cli_name='ldap_uri',
            label=_('LDAP URI'),
            doc=_('LDAP URI of DS server to migrate from'),
        ),
        Password('bindpw',
            cli_name='password',
            label=_('Password'),
            doc=_('bind password'),
        ),
    )

    takes_options = (
        Str('binddn?',
            cli_name='bind_dn',
            label=_('Bind DN'),
            default=u'cn=directory manager',
            autofill=True,
        ),
        Str('usercontainer?',
            cli_name='user_container',
            label=_('User container'),
            doc=_('RDN of container for users in DS'),
            default=u'ou=people',
            autofill=True,
        ),
        Str('groupcontainer?',
            cli_name='group_container',
            label=_('Group container'),
            doc=_('RDN of container for groups in DS'),
            default=u'ou=groups',
            autofill=True,
        ),
        List('userobjectclass?',
            cli_name='user_objectclass',
            label=_('User object class'),
            doc=_('Comma-separated list of objectclasses used to search for user entries in DS'),
            default=(u'person',),
            autofill=True,
        ),
        List('groupobjectclass?',
            cli_name='group_objectclass',
            label=_('Group object class'),
            doc=_('Comma-separated list of objectclasses used to search for group entries in DS'),
            default=(u'groupOfUniqueNames', u'groupOfNames'),
            autofill=True,
        ),
        List('userignoreobjectclass?',
            cli_name='user_ignore_objectclass',
            label=_('Ignore user object class'),
            doc=_('Comma-separated list of objectclasses to be ignored for user entries in DS'),
            default=tuple(),
            autofill=True,
        ),
        List('userignoreattribute?',
            cli_name='user_ignore_attribute',
            label=_('Ignore user attribute'),
            doc=_('Comma-separated list of attributes to be ignored for user entries in DS'),
            default=tuple(),
            autofill=True,
        ),
        List('groupignoreobjectclass?',
            cli_name='group_ignore_objectclass',
            label=_('Ignore group object class'),
            doc=_('Comma-separated list of objectclasses to be ignored for group entries in DS'),
            default=tuple(),
            autofill=True,
        ),
        List('groupignoreattribute?',
            cli_name='group_ignore_attribute',
            label=_('Ignore group attribute'),
            doc=_('Comma-separated list of attributes to be ignored for group entries in DS'),
            default=tuple(),
            autofill=True,
        ),
        StrEnum('schema?',
            cli_name='schema',
            label=_('LDAP schema'),
            doc=_('The schema used on the LDAP server. Supported values are RFC2307 and RFC2307bis. The default is RFC2307bis'),
            values=_supported_schemas,
            default=_supported_schemas[0],
            autofill=True,
        ),
        Flag('continue?',
            doc=_('Continuous operation mode. Errors are reported but the process continues'),
            default=False,
        ),
    )

    has_output = (
        output.Output('result',
            type=dict,
            doc=_('Lists of objects migrated; categorized by type.'),
        ),
        output.Output('failed',
            type=dict,
            doc=_('Lists of objects that could not be migrated; categorized by type.'),
        ),
        output.Output('enabled',
            type=bool,
            doc=_('False if migration mode was disabled.'),
        ),
    )

    exclude_doc = _('comma-separated list of %s to exclude from migration')

    truncated_err_msg = _('''\
search results for objects to be migrated
have been truncated by the server;
migration process might be incomplete\n''')

    migration_disabled_msg = _('''\
Migration mode is disabled. Use \'ipa config-mod\' to enable it.''')

    pwd_migration_msg = _('''\
Passwords have been migrated in pre-hashed format.
IPA is unable to generate Kerberos keys unless provided
with clear text passwords. All migrated users need to
login at https://your.domain/ipa/migration/ before they
can use their Kerberos accounts.''')

    def get_options(self):
        """
        Call get_options of the baseclass and add "exclude" options
        for each type of object being migrated.
        """
        for option in super(migrate_ds, self).get_options():
            yield option
        for ldap_obj_name in self.migrate_objects:
            ldap_obj = self.api.Object[ldap_obj_name]
            name = 'exclude_%ss' % to_cli(ldap_obj_name)
            doc = self.exclude_doc % ldap_obj.object_name_plural
            yield List(
                '%s?' % name, cli_name=name, doc=doc, default=tuple(),
                autofill=True
            )

    def normalize_options(self, options):
        """
        Convert all "exclude" option values to lower-case.

        Also, empty List parameters are converted to None, but the migration
        plugin doesn't like that - convert back to empty lists.
        """
        for p in self.params():
            if isinstance(p, List):
                if options[p.name]:
                    options[p.name] = tuple(
                        v.lower() for v in options[p.name]
                    )
                else:
                    options[p.name] = tuple()

    def _get_search_bases(self, options, ds_base_dn, migrate_order):
        search_bases = dict()
        for ldap_obj_name in migrate_order:
            search_bases[ldap_obj_name] = '%s,%s' % (
                options['%scontainer' % to_cli(ldap_obj_name)], ds_base_dn
            )
        return search_bases

    def migrate(self, ldap, config, ds_ldap, ds_base_dn, options):
        """
        Migrate objects from DS to LDAP.
        """
        migrated = {} # {'OBJ': ['PKEY1', 'PKEY2', ...], ...}
        failed = {} # {'OBJ': {'PKEY1': 'Failed 'cos blabla', ...}, ...}
        search_bases = self._get_search_bases(options, ds_base_dn, self.migrate_order)
        for ldap_obj_name in self.migrate_order:
            ldap_obj = self.api.Object[ldap_obj_name]

            search_filter = construct_filter(self.migrate_objects[ldap_obj_name]['filter_template'],
                                             options[to_cli(self.migrate_objects[ldap_obj_name]['oc_option'])])
            exclude = options['exclude_%ss' % to_cli(ldap_obj_name)]
            context = {}

            migrated[ldap_obj_name] = []
            failed[ldap_obj_name] = {}

            try:
                (entries, truncated) = ds_ldap.find_entries(
                    search_filter, ['*'], search_bases[ldap_obj_name],
                    ds_ldap.SCOPE_ONELEVEL,
                    time_limit=0, size_limit=-1,
                    search_refs=True    # migrated DS may contain search references
                )
            except errors.NotFound:
                if not options.get('continue',False):
                    raise errors.NotFound(
                        reason=_('Container for %(container)s not found') % {'container': ldap_obj_name}
                    )
                else:
                    truncated = False
                    entries = []
            if truncated:
                self.log.error(
                    '%s: %s' % (
                        ldap_obj.name, self.truncated_err_msg
                    )
                )

            blacklists = {}
            for blacklist in ('oc_blacklist', 'attr_blacklist'):
                blacklist_option = self.migrate_objects[ldap_obj_name][blacklist+'_option']
                if blacklist_option is not None:
                    blacklists[blacklist] = options.get(blacklist_option, tuple())
                else:
                    blacklists[blacklist] = tuple()

            for (dn, entry_attrs) in entries:
                if dn is None:  # LDAP search reference
                    failed[ldap_obj_name][entry_attrs[0]] = unicode(_ref_err_msg)
                    continue

                pkey = entry_attrs[ldap_obj.primary_key.name][0].lower()
                if pkey in exclude:
                    continue

                dn = ldap_obj.get_dn(pkey)
                entry_attrs['objectclass'] = list(
                    set(
                        config.get(
                            ldap_obj.object_class_config, ldap_obj.object_class
                        ) + [o.lower() for o in entry_attrs['objectclass']]
                    )
                )

                callback = self.migrate_objects[ldap_obj_name]['pre_callback']
                if callable(callback):
                    dn = callback(
                        ldap, pkey, dn, entry_attrs, failed[ldap_obj_name],
                        config, context, schema = options['schema'],
                        search_bases = search_bases,
                        **blacklists
                    )
                    if not dn:
                        continue

                try:
                    ldap.add_entry(dn, entry_attrs)
                except errors.ExecutionError, e:
                    failed[ldap_obj_name][pkey] = unicode(e)
                else:
                    migrated[ldap_obj_name].append(pkey)

                    callback = self.migrate_objects[ldap_obj_name]['post_callback']
                    if callable(callback):
                        callback(
                            ldap, pkey, dn, entry_attrs, failed[ldap_obj_name],
                            config, context
                        )

        return (migrated, failed)

    def execute(self, ldapuri, bindpw, **options):
        ldap = self.api.Backend.ldap2
        self.normalize_options(options)

        config = ldap.get_ipa_config()[1]

        # check if migration mode is enabled
        if config.get('ipamigrationenabled', ('FALSE', ))[0] == 'FALSE':
            return dict(result={}, failed={}, enabled=False)

        # connect to DS
        ds_ldap = ldap2(shared_instance=False, ldap_uri=ldapuri, base_dn='')
        ds_ldap.connect(bind_dn=options['binddn'], bind_pw=bindpw)

        # retrieve DS base DN
        (entries, truncated) = ds_ldap.find_entries(
            '', ['namingcontexts'], '', ds_ldap.SCOPE_BASE,
            size_limit=-1, time_limit=0,
        )
        try:
            ds_base_dn = entries[0][1]['namingcontexts'][0]
        except (IndexError, KeyError), e:
            raise StandardError(str(e))

        # migrate!
        (migrated, failed) = self.migrate(
            ldap, config, ds_ldap, ds_base_dn, options
        )

        return dict(result=migrated, failed=failed, enabled=True)

    def output_for_cli(self, textui, result, ldapuri, bindpw, **options):
        textui.print_name(self.name)
        if not result['enabled']:
            textui.print_plain(self.migration_disabled_msg)
            return 1
        textui.print_plain('Migrated:')
        textui.print_entry1(
            result['result'], attr_order=self.migrate_order,
            one_value_per_line=False
        )
        for ldap_obj_name in self.migrate_order:
            textui.print_plain('Failed %s:' % ldap_obj_name)
            textui.print_entry1(
                result['failed'][ldap_obj_name], attr_order=self.migrate_order,
                one_value_per_line=True,
            )
        textui.print_plain('-' * len(self.name))
        textui.print_plain(unicode(self.pwd_migration_msg))

api.register(migrate_ds)
