# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2011-2013 University of Southern California / ISI
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Class for Tilera bare-metal nodes.
"""

import base64
import os

import jinja2
from oslo.config import cfg

from nova.compute import instance_types
from ironic.common import exception
from ironic.common import utils
from ironic.common import states
from ironic import db
from ironic.manager import base
from ironic.openstack.common.db import exception as db_exc
from ironic.openstack.common import fileutils
from ironic.openstack.common import log as logging

tilera_opts = [
    cfg.StrOpt('net_config_template',
               default='$pybasedir/ironic/net-dhcp.ubuntu.template',
               help='Template file for injected network config'),
    ]

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.register_opts(tilera_opts)
CONF.import_opt('use_ipv6', 'ironic.netconf')


def build_network_config(network_info):
    try:
        assert isinstance(network_info, list)
    except AssertionError:
        network_info = [network_info]
    interfaces = []
    for id, (network, mapping) in enumerate(network_info):
        address_v6 = None
        gateway_v6 = None
        netmask_v6 = None
        if CONF.use_ipv6:
            address_v6 = mapping['ip6s'][0]['ip']
            netmask_v6 = mapping['ip6s'][0]['netmask']
            gateway_v6 = mapping['gateway_v6']
        interface = {
                'name': 'eth%d' % id,
                'address': mapping['ips'][0]['ip'],
                'gateway': mapping['gateway'],
                'netmask': mapping['ips'][0]['netmask'],
                'dns': ' '.join(mapping['dns']),
                'address_v6': address_v6,
                'gateway_v6': gateway_v6,
                'netmask_v6': netmask_v6,
            }
        interfaces.append(interface)

    tmpl_path, tmpl_file = os.path.split(CONF.net_config_template)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(tmpl_path))
    template = env.get_template(tmpl_file)
    return template.render({'interfaces': interfaces,
                            'use_ipv6': CONF.use_ipv6})


def get_image_dir_path(instance):
    """Generate the dir for an instances disk."""
    return os.path.join(CONF.instances_path, instance['name'])


def get_image_file_path(instance):
    """Generate the full path for an instances disk."""
    return os.path.join(CONF.instances_path, instance['name'], 'disk')


def get_tilera_nfs_path(node_id):
    """Generate the path for an instances Tilera nfs."""
    tilera_nfs_dir = "fs_" + str(node_id)
    return os.path.join(CONF.tftp_root, tilera_nfs_dir)


def get_partition_sizes(instance):
    instance_type = instance_types.extract_instance_type(instance)
    root_mb = instance_type['root_gb'] * 1024
    swap_mb = instance_type['swap']

    if swap_mb < 1:
        swap_mb = 1

    return (root_mb, swap_mb)


def get_tftp_image_info(instance):
    """Generate the paths for tftp files for this instance.

    Raises NovaException if
    - instance does not contain kernel_id
    """
    image_info = {
            'kernel': [None, None],
            }
    try:
        image_info['kernel'][0] = str(instance['kernel_id'])
    except KeyError:
        pass

    missing_labels = []
    for label in image_info.keys():
        (uuid, path) = image_info[label]
        if not uuid:
            missing_labels.append(label)
        else:
            image_info[label][1] = os.path.join(CONF.tftp_root,
                            instance['uuid'], label)
    if missing_labels:
        raise exception.NovaException(_(
            "Can not activate Tilera bootloader. "
            "The following boot parameters "
            "were not passed to baremetal driver: %s") % missing_labels)
    return image_info


class Tilera(base.NodeDriver):
    """Tilera bare metal driver."""

    def __init__(self, virtapi):
        super(Tilera, self).__init__(virtapi)

    def _collect_mac_addresses(self, context, node):
        macs = set()
        for nic in db.bm_interface_get_all_by_bm_node_id(context, node['id']):
            if nic['address']:
                macs.add(nic['address'])
        return sorted(macs)

    def _cache_tftp_images(self, context, instance, image_info):
        """Fetch the necessary kernels and ramdisks for the instance."""
        fileutils.ensure_tree(
                os.path.join(CONF.tftp_root, instance['uuid']))

        LOG.debug(_("Fetching kernel and ramdisk for instance %s") %
                        instance['name'])
        for label in image_info.keys():
            (uuid, path) = image_info[label]
            utils.cache_image(
                    context=context,
                    target=path,
                    image_id=uuid,
                    user_id=instance['user_id'],
                    project_id=instance['project_id'],
                )

    def _cache_image(self, context, instance, image_meta):
        """Fetch the instance's image from Glance

        This method pulls the relevant AMI and associated kernel and ramdisk,
        and the deploy kernel and ramdisk from Glance, and writes them
        to the appropriate places on local disk.

        Both sets of kernel and ramdisk are needed for Tilera booting, so these
        are stored under CONF.tftp_root.

        At present, the AMI is cached and certain files are injected.
        Debian/ubuntu-specific assumptions are made regarding the injected
        files. In a future revision, this functionality will be replaced by a
        more scalable and os-agnostic approach: the deployment ramdisk will
        fetch from Glance directly, and write its own last-mile configuration.
        """
        fileutils.ensure_tree(get_image_dir_path(instance))
        image_path = get_image_file_path(instance)

        LOG.debug(_("Fetching image %(ami)s for instance %(name)s") %
                        {'ami': image_meta['id'], 'name': instance['name']})
        utils.cache_image(context=context,
                             target=image_path,
                             image_id=image_meta['id'],
                             user_id=instance['user_id'],
                             project_id=instance['project_id']
                        )

        return [image_meta['id'], image_path]

    def _inject_into_image(self, context, node, instance, network_info,
            injected_files=None, admin_password=None):
        """Inject last-mile configuration into instances image

        Much of this method is a hack around DHCP and cloud-init
        not working together with baremetal provisioning yet.
        """
        partition = None
        if not instance['kernel_id']:
            partition = "1"

        ssh_key = None
        if 'key_data' in instance and instance['key_data']:
            ssh_key = str(instance['key_data'])

        if injected_files is None:
            injected_files = []
        else:
            injected_files = list(injected_files)

        net_config = build_network_config(network_info)

        if instance['hostname']:
            injected_files.append(('/etc/hostname', instance['hostname']))

        LOG.debug(_("Injecting files into image for instance %(name)s") %
                        {'name': instance['name']})

        utils.inject_into_image(
                    image=get_image_file_path(instance),
                    key=ssh_key,
                    net=net_config,
                    metadata=instance['metadata'],
                    admin_password=admin_password,
                    files=injected_files,
                    partition=partition,
                )

    def cache_images(self, context, node, instance,
            admin_password, image_meta, injected_files, network_info):
        """Prepare all the images for this instance."""
        tftp_image_info = get_tftp_image_info(instance)
        self._cache_tftp_images(context, instance, tftp_image_info)

        self._cache_image(context, instance, image_meta)
        self._inject_into_image(context, node, instance, network_info,
                injected_files, admin_password)

    def destroy_images(self, context, node, instance):
        """Delete instance's image file."""
        utils.unlink_without_raise(get_image_file_path(instance))
        utils.rmtree_without_raise(get_image_dir_path(instance))

    def activate_bootloader(self, context, node, instance):
        """Configure Tilera boot loader for an instance

        Kernel and ramdisk images are downloaded by cache_tftp_images,
        and stored in /tftpboot/{uuid}/

        This method writes the instances config file, and then creates
        symlinks for each MAC address in the instance.

        By default, the complete layout looks like this:

        /tftpboot/
            ./{uuid}/
                 kernel
            ./fs_node_id/
        """
        (root_mb, swap_mb) = get_partition_sizes(instance)
        tilera_nfs_path = get_tilera_nfs_path(node['id'])
        image_file_path = get_image_file_path(instance)

        deployment_key = utils.random_alnum(32)
        db.bm_node_update(context, node['id'],
                {'deploy_key': deployment_key,
                 'image_path': image_file_path,
                 'pxe_config_path': tilera_nfs_path,
                 'root_mb': root_mb,
                 'swap_mb': swap_mb})

        if os.path.exists(image_file_path) and \
           os.path.exists(tilera_nfs_path):
            utils.execute('mount', '-o', 'loop', image_file_path,
                tilera_nfs_path, run_as_root=True)

    def deactivate_bootloader(self, context, node, instance):
        """Delete Tilera bootloader images and config."""
        try:
            db.bm_node_update(context, node['id'],
                    {'deploy_key': None,
                     'image_path': None,
                     'pxe_config_path': None,
                     'root_mb': 0,
                     'swap_mb': 0})
        except exception.NodeNotFound:
            pass

        tilera_nfs_path = get_tilera_nfs_path(node['id'])

        if os.path.ismount(tilera_nfs_path):
            utils.execute('rpc.mountd', run_as_root=True)
            utils.execute('umount', '-f', tilera_nfs_path, run_as_root=True)

        try:
            image_info = get_tftp_image_info(instance)
        except exception.NovaException:
            pass
        else:
            for label in image_info.keys():
                (uuid, path) = image_info[label]
                utils.unlink_without_raise(path)

        try:
            self._collect_mac_addresses(context, node)
        except db_exc.DBError:
            pass

        if os.path.exists(os.path.join(CONF.tftp_root,
                instance['uuid'])):
            utils.rmtree_without_raise(
                os.path.join(CONF.tftp_root, instance['uuid']))

    def _iptables_set(self, node_ip, user_data):
        """Sets security setting (iptables:port) if needed.

        iptables -A INPUT -p tcp ! -s $IP --dport $PORT -j DROP
        /tftpboot/iptables_rule script sets iptables rule on the given node.
        """
        rule_path = CONF.tftp_root + "/iptables_rule"
        if user_data is not None:
            open_ip = base64.b64decode(user_data)
            utils.execute(rule_path, node_ip, open_ip)

    def activate_node(self, context, node, instance):
        """Wait for Tilera deployment to complete."""

        locals = {'error': '', 'started': False}

        try:
            row = db.bm_node_get(context, node['id'])
            if instance['uuid'] != row.get('instance_uuid'):
                locals['error'] = _("Node associated with another instance"
                                    " while waiting for deploy of %s")

            status = row.get('task_state')
            if (status == states.DEPLOYING and
                locals['started'] is False):
                LOG.info(_('Tilera deploy started for instance %s')
                           % instance['uuid'])
                locals['started'] = True
            elif status in (states.DEPLOYDONE,
                            states.BUILDING,
                            states.ACTIVE):
                LOG.info(_("Tilera deploy completed for instance %s")
                           % instance['uuid'])
                node_ip = node['pm_address']
                user_data = instance['user_data']
                try:
                    self._iptables_set(node_ip, user_data)
                except Exception:
                    self.deactivate_bootloader(context, node, instance)
                    raise exception.NovaException(_("Node is "
                          "unknown error state."))
            elif status == states.DEPLOYFAIL:
                locals['error'] = _("Tilera deploy failed for instance %s")
        except exception.NodeNotFound:
                locals['error'] = _("Baremetal node deleted while waiting "
                                "for deployment of instance %s")

        if locals['error']:
            raise exception.InstanceDeployFailure(
                    locals['error'] % instance['uuid'])

    def deactivate_node(self, context, node, instance):
        pass
