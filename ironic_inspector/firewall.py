# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import os
import re
import subprocess

from eventlet import semaphore
from oslo_config import cfg
from oslo_log import log

from ironic_inspector.common.i18n import _LE, _LW
from ironic_inspector.common import ironic as ir_utils
from ironic_inspector import node_cache


CONF = cfg.CONF
LOG = log.getLogger("ironic_inspector.firewall")
NEW_CHAIN = None
CHAIN = None
INTERFACE = None
LOCK = semaphore.BoundedSemaphore()
BASE_COMMAND = None
BLACKLIST_CACHE = None
ENABLED = True
EMAC_REGEX = 'EMAC=([0-9a-f]{2}(:[0-9a-f]{2}){5}) IMAC=.*'


def _iptables(*args, **kwargs):
    # NOTE(dtantsur): -w flag makes it wait for xtables lock
    cmd = BASE_COMMAND + args
    ignore = kwargs.pop('ignore', False)
    LOG.debug('Running iptables %s', args)
    kwargs['stderr'] = subprocess.STDOUT
    try:
        subprocess.check_output(cmd, **kwargs)
    except subprocess.CalledProcessError as exc:
        output = exc.output.replace('\n', '. ')
        if ignore:
            LOG.debug('Ignoring failed iptables %(args)s: %(output)s',
                      {'args': args, 'output': output})
        else:
            LOG.error(_LE('iptables %(iptables)s failed: %(exc)s'),
                      {'iptables': args, 'exc': output})
            raise


def init():
    """Initialize firewall management.

    Must be called one on start-up.
    """
    if not CONF.firewall.manage_firewall:
        return

    global INTERFACE, CHAIN, NEW_CHAIN, BASE_COMMAND, BLACKLIST_CACHE
    BLACKLIST_CACHE = None
    INTERFACE = CONF.firewall.dnsmasq_interface
    CHAIN = CONF.firewall.firewall_chain
    NEW_CHAIN = CHAIN + '_temp'
    BASE_COMMAND = ('sudo', 'ironic-inspector-rootwrap',
                    CONF.rootwrap_config, 'iptables',)

    # -w flag makes iptables wait for xtables lock, but it's not supported
    # everywhere yet
    try:
        with open(os.devnull, 'wb') as null:
            subprocess.check_call(BASE_COMMAND + ('-w', '-h'),
                                  stderr=null, stdout=null)
    except subprocess.CalledProcessError:
        LOG.warning(_LW('iptables does not support -w flag, please update '
                        'it to at least version 1.4.21'))
    else:
        BASE_COMMAND += ('-w',)

    _clean_up(CHAIN)
    # Not really needed, but helps to validate that we have access to iptables
    _iptables('-N', CHAIN)


def _clean_up(chain):
    _iptables('-D', 'INPUT', '-i', INTERFACE, '-p', 'udp',
              '--dport', '67', '-j', chain,
              ignore=True)
    _iptables('-F', chain, ignore=True)
    _iptables('-X', chain, ignore=True)


def clean_up():
    """Clean up everything before exiting."""
    if not CONF.firewall.manage_firewall:
        return

    _clean_up(CHAIN)
    _clean_up(NEW_CHAIN)


def _should_enable_dhcp():
    """Check whether we should enable DHCP at all.

    We won't even open our DHCP if no nodes are on introspection and
    node_not_found_hook is not set.
    """
    return (node_cache.introspection_active() or
            CONF.processing.node_not_found_hook)


@contextlib.contextmanager
def _temporary_chain(chain, main_chain):
    """Context manager to operate on a temporary chain."""
    # Clean up a bit to account for possible troubles on previous run
    _clean_up(chain)
    _iptables('-N', chain)

    yield

    # Swap chains
    _iptables('-I', 'INPUT', '-i', INTERFACE, '-p', 'udp',
              '--dport', '67', '-j', chain)
    _iptables('-D', 'INPUT', '-i', INTERFACE, '-p', 'udp',
              '--dport', '67', '-j', main_chain,
              ignore=True)
    _iptables('-F', main_chain, ignore=True)
    _iptables('-X', main_chain, ignore=True)
    _iptables('-E', chain, main_chain)


def _disable_dhcp():
    """Disable DHCP completely."""
    global ENABLED, BLACKLIST_CACHE

    if not ENABLED:
        LOG.debug('DHCP is already disabled, not updating')
        return

    LOG.debug('No nodes on introspection and node_not_found_hook is '
              'not set - disabling DHCP')
    BLACKLIST_CACHE = None
    with _temporary_chain(NEW_CHAIN, CHAIN):
        # Blacklist everything
        _iptables('-A', NEW_CHAIN, '-j', 'REJECT')

    ENABLED = False


def update_filters(ironic=None):
    """Update firewall filter rules for introspection.

    Gives access to PXE boot port for any machine, except for those,
    whose MAC is registered in Ironic and is not on introspection right now.

    This function is called from both introspection initialization code and
    from periodic task. This function is supposed to be resistant to unexpected
    iptables state.

    ``init()`` function must be called once before any call to this function.
    This function is using ``eventlet`` semaphore to serialize access from
    different green threads.

    Does nothing, if firewall management is disabled in configuration.

    :param ironic: Ironic client instance, optional.
    """
    global BLACKLIST_CACHE, ENABLED

    if not CONF.firewall.manage_firewall:
        return

    assert INTERFACE is not None
    ironic = ir_utils.get_client() if ironic is None else ironic
    with LOCK:
        if not _should_enable_dhcp():
            _disable_dhcp()
            return

        ports_active = ironic.port.list(limit=0, fields=['address', 'extra'])
        macs_active = set(p.address for p in ports_active)
        to_blacklist = macs_active - node_cache.active_macs()
        ib_mac_mapping = (
            _ib_mac_to_rmac_mapping(to_blacklist, ports_active))

        if (BLACKLIST_CACHE is not None and
                to_blacklist == BLACKLIST_CACHE and not ib_mac_mapping):
            LOG.debug('Not updating iptables - no changes in MAC list %s',
                      to_blacklist)
            return

        LOG.debug('Blacklisting active MAC\'s %s', to_blacklist)
        # Force update on the next iteration if this attempt fails
        BLACKLIST_CACHE = None

        with _temporary_chain(NEW_CHAIN, CHAIN):
            # - Blacklist active macs, so that nova can boot them
            for mac in to_blacklist:
                mac = ib_mac_mapping.get(mac) or mac
                _iptables('-A', NEW_CHAIN, '-m', 'mac',
                          '--mac-source', mac, '-j', 'DROP')
            # - Whitelist everything else
            _iptables('-A', NEW_CHAIN, '-j', 'ACCEPT')

        # Cache result of successful iptables update
        ENABLED = True
        BLACKLIST_CACHE = to_blacklist


def _ib_mac_to_rmac_mapping(blacklist_macs, ports_active):
    """Mapping between host InfiniBand MAC to EthernetOverInfiniBand MAC

    On InfiniBand deployment we need to map between the baremetal host
    InfiniBand MAC to the EoIB MAC. The EoIB MAC addresses are learned
    automatically by the EoIB interfaces and those MACs are recorded
    to the /sys/class/net/<ethoib_interface>/eth/neighs file.
    The InfiniBand GUID is taken from the ironic port client-id extra
    attribute. The InfiniBand GUID is the last 8 bytes of the client-id.
    The file format allows to map the GUID to EoIB MAC. The firewall
    rules based on those MACs get applied to the dnsmasq_interface by the
    update_filters function.

    :param blacklist_macs: List of InfiniBand baremetal hosts macs to
                           blacklist.
    :param ports_active: list of active ironic ports
    :return baremetal InfiniBand to remote mac on ironic node mapping
    """
    ethoib_interfaces = CONF.firewall.ethoib_interfaces
    ib_mac_to_remote_mac = {}
    for interface in ethoib_interfaces:
        neighs_file = (
            os.path.join('/sys/class/net', interface, 'eth/neighs'))
        try:
            with open(neighs_file, 'r') as fd:
                data = fd.read()
        except IOError:
            LOG.error(
                _LE('Interface %s is not Ethernet Over InfiniBand; '
                    'Skipping ...'), interface)
            continue
        for port in ports_active:
            if port.address in blacklist_macs:
                client_id = port.extra.get('client-id')
                if client_id:
                    # Note(moshele): The last 8 bytes in the client-id is
                    # the baremetal node InfiniBand GUID
                    guid = client_id[-23:]
                    p = re.compile(EMAC_REGEX + guid)
                    match = p.search(data)
                    if match:
                        ib_mac_to_remote_mac[port.address] = match.group(1)
    return ib_mac_to_remote_mac
