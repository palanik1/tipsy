#!/usr/bin/env python

# TIPSY: Telco pIPeline benchmarking SYstem
#
# Copyright (C) 2018 by its authors (See AUTHORS)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import argparse
import json
import math
import multiprocessing
import random
import scapy
import sys
try:
    from pathlib import PosixPath
except ImportError:
    # python2
    PosixPath = str
from scapy.all import *

try:
    import args_from_schema
except ImportError:
    from . import args_from_schema

__all__ = ["gen_pcap"]


class PicklablePacket(object):
    """A container for scapy packets that can be pickled (in contrast
    to scapy packets themselves). https://stackoverflow.com/a/4312192"""

    __slots__ = ['contents', 'time']

    def __init__(self, pkt):
        self.contents = bytes(pkt)
        self.time = pkt.time

    def __call__(self):
        """Get the original scapy packet."""
        pkt = Ether(self.contents)
        pkt.time = self.time
        return pkt


class ObjectView(object):
    def __init__(self, **kwargs):
        tmp = {k.replace('-', '_'): v for k, v in kwargs.items()}
        self.__dict__.update(**tmp)

    def __repr__(self):
        return self.__dict__.__repr__()

    def as_dict(self):
        return self.__dict__


def byte_seq(template, seq):
    return template % (int(seq / 254), (seq % 254) + 1)


def gen_packets(params_tuple):
    (dir, pkt_num, pkt_size, conf, id, workers) = params_tuple
    pl = conf.name
    pkts = []
    pkt_gen_func = getattr(sys.modules[__name__],
                           '_gen_pkts_%s' % pl)
    if dir[0] == 'b':
        gen_pkts = pkt_gen_func('u', pkt_size, conf, int(pkt_num/2)+1,
                                id, workers)
        gen_pkts.extend(pkt_gen_func('d', pkt_size, conf, int(pkt_num/2)+1,
                                     id, workers))
    else:
        gen_pkts = pkt_gen_func(dir[0], pkt_size, conf, pkt_num, id, workers)
    gen_pkts = gen_pkts[:pkt_num]
    random.shuffle(gen_pkts)
    return [PicklablePacket(p) for p in gen_pkts]


def _get_auto_portfwd_pktnum(conf, dir):
    return 10


def _get_auto_l2fwd_pktnum(conf, dir):
    if 'd' in dir:  # downstream
        return len(conf.downstream_table)
    elif 'u' in dir:  # upstream
        return len(conf.upstream_table)
    elif 'b' in dir:  # bidir
        return max(len(conf.upstream_table),
                   len(conf.downstream_table))
    else:
        raise ValueError


def _get_auto_l3fwd_pktnum(conf, dir):
    if 'd' in dir:  # downstream
        return len(conf.downstream_l3_table)
    elif 'u' in dir:  # upstream
        return len(conf.upstream_l3_table)
    elif 'b' in dir:  # bidir
        return max(len(conf.upstream_l3_table),
                   len(conf.downstream_l3_table))
    else:
        raise ValueError


def _get_auto_mgw_pktnum(conf, dir):
    return len(conf.users)


def _get_auto_vmgw_pktnum(conf, dir):
    return len(conf.users)


def _get_auto_bng_pktnum(conf, dir):
    return len(conf.nat_table)


def _gen_pkts_portfwd(dir, pkt_size, conf, pkt_num, id, workers):
    return [_gen_pkt_portfwd(pkt_size) for _ in range(pkt_num)]


def _gen_pkt_portfwd(pkt_size):
    smac = byte_seq('aa:bb:bb:aa:%02x:%02x', random.randrange(1, 65023))
    dmac = byte_seq('aa:cc:dd:cc:%02x:%02x', random.randrange(1, 65023))
    dip = byte_seq('3.3.%d.%d', random.randrange(1, 255))
    p = Ether(dst=dmac, src=smac) / IP(dst=dip)
    p = add_payload(p, pkt_size)
    return p


def _gen_pkts_l2fwd(dir, pkt_size, conf, pkt_num, id, workers):
    table = get_table_slice(conf, '%s_table' % expand_direction(dir),
                            id, workers)
    pkts = []
    for i in range(pkt_num):
        dmac = table[i % len(table)].mac
        pkts.append(_gen_pkt_l2fwd(pkt_size, dmac))
    return pkts


def _gen_pkt_l2fwd(pkt_size, dmac):
    smac = byte_seq('aa:bb:bb:aa:%02x:%02x', random.randrange(1, 65023))
    dip = byte_seq('3.3.%d.%d', random.randrange(1, 255))
    p = Ether(dst=dmac, src=smac) / IP(dst=dip)
    p = add_payload(p, pkt_size)
    return p


def _gen_pkts_l3fwd(dir, pkt_size, conf, pkt_num, id, workers):
    table = get_table_slice(conf, '%s_l3_table' % expand_direction(dir),
                            id, workers)
    # NB.  In the uplink case, the traffic leaves Tester via its
    # uplink port and arrives at the downlink of the SUT.
    mac = getattr(conf.sut, '%sl_port_mac' % get_other_direction(dir[0]))
    pkts = []
    for i in range(pkt_num):
        ip = table[i % len(table)].ip
        pkts.append(_gen_pkt_l3fwd(pkt_size, mac, ip))
    return pkts


def _gen_pkt_l3fwd(pkt_size, mac, ip):
    p = Ether(dst=mac) / IP(dst=ip)
    p = add_payload(p, pkt_size)
    return p


def _gen_pkts_mgw(dir, pkt_size, conf, pkt_num, id, workers):
    pkts = []
    gw = conf.gw
    for i in range(pkt_num):
        server = random.choice(conf.srvs)
        user = random.choice(conf.users)
        proto = random.choice([TCP, UDP])
        if 'd' in dir:
            pkts.append(_gen_dl_pkt_mgw(pkt_size, proto, gw,
                                        server, user))
        elif 'u' in dir:
            bst = conf.bsts[user.tun_end]
            pkts.append(_gen_ul_pkt_mgw(pkt_size, proto, gw,
                                        server, user, bst))
    return pkts


def _gen_dl_pkt_mgw(pkt_size, proto, gw, server, user):
    p = (
        Ether(dst=gw.mac) /
        IP(src=server.ip, dst=user.ip) /
        proto()
    )
    p = add_payload(p, pkt_size)
    return p


def _gen_ul_pkt_mgw(pkt_size, proto, gw, server, user, bst):
    p = (
        Ether(src=bst.mac, dst=gw.mac, type=0x0800) /
        IP(src=bst.ip, dst=gw.ip) /
        UDP(sport=4789, dport=4789) /
        VXLAN(vni=user.teid, flags=0x08) /
        Ether(dst=gw.mac, type=0x0800) /
        IP(src=user.ip, dst=server.ip) /
        proto()
    )
    p = add_payload(p, pkt_size)
    return p


def _gen_pkts_bng(dir, pkt_size, conf, pkt_num, id, workers):
    pkts = []
    protos = {'6': TCP, '17': UDP}
    gw = conf.gw
    for i in range(pkt_num):
        server = random.choice(conf.srvs)
        user = random.choice(conf.users)
        user_nat = random.choice([e for e in conf.nat_table
                                  if e.priv_ip == user.ip])
        proto = protos[str(user_nat.proto)]
        if 'd' in dir:
            pkts.append(_gen_dl_pkt_bng(pkt_size, proto, gw,
                                        server, user, user_nat))
        elif 'u' in dir:
            cpe = conf.cpe[user.tun_end]
            pkts.append(_gen_ul_pkt_bng(pkt_size, proto, gw,
                                        server, user, user_nat, cpe))
    return pkts


def _gen_dl_pkt_bng(pkt_size, proto, gw, server, user, user_nat):
    p = (
        Ether(dst=gw.mac) /
        IP(src=server.ip, dst=user_nat.pub_ip) /
        proto(sport=user_nat.pub_port, dport=user_nat.pub_port)
    )
    p = add_payload(p, pkt_size)
    return p


def _gen_ul_pkt_bng(pkt_size, proto, gw, server, user, user_nat, cpe):
    p = (
        Ether(src=cpe.mac, dst=gw.mac, type=0x0800) /
        IP(src=cpe.ip, dst=gw.ip) /
        UDP(sport=4789, dport=4789) /
        VXLAN(vni=user.teid, flags=0x08) /
        Ether(dst=gw.mac, type=0x0800) /
        IP(src=user.ip, dst=server.ip) /
        proto(sport=user_nat.priv_port, dport=user_nat.priv_port)
    )
    p = add_payload(p, pkt_size)
    return p


def _gen_dl_pkt_vmgw(pkt_size, conf):
    p = _gen_dl_pkt_mgw(pkt_size, conf)
    p = vwrap(p, conf)
    return p


def _gen_ul_pkt_vmgw(pkt_size, conf):
    p = _gen_ul_pkt_mgw(pkt_size, conf)
    p = vwrap(p, conf)
    return p


def add_payload(p, pkt_size):
    if len(p) < pkt_size:
        #"\x00" is a single zero byte
        s = "\x00" * (pkt_size - len(p))
        p = p / Padding(s)
    return p


def vwrap(pkt, conf):
    'Add VXLAN header for infra processing'
    vxlanpkt = (
        Ether(src=conf.dcgw.mac, dst=conf.gw.mac) /
        IP(src=conf.dcgw.ip, dst=conf.gw.ip) /
        UDP(sport=4788, dport=4789) /
        VXLAN(vni=conf.dcgw.vni) /
        pkt
    )
    return vxlanpkt


def expand_direction(dir):
    if 'u' in dir:
        return 'upstream'
    elif 'd' in dir:
        return 'downstream'
    elif 'b' in dir:
        return 'bidir'
    else:
        raise ValueError

def get_other_direction(dir):
    return {'u': 'd', 'd': 'u'}[dir]


def get_table_slice(conf, table_name, thread_id, workers):
    table = getattr(conf, table_name)
    c = int(math.ceil(len(table)/float(workers)))
    table = table[c * thread_id : max(len(table), c * (thread_id + 1))]
    random.shuffle(table)
    return table


def json_load(file, object_hook=None):
    if type(file) == str:
        with open(file, 'r') as infile:
            return json.load(infile, object_hook=object_hook)
    elif type(file) == PosixPath:
        with file.open('r') as infile:
            return json.load(infile, object_hook=object_hook)
    else:
        return json.load(file, object_hook=object_hook)


def parse_args(defaults=None):
    if defaults:
        required = False
    else:
        required = True
    parser = argparse.ArgumentParser()
    args_from_schema.add_args(parser, 'traffic')
    parser.formatter_class = argparse.ArgumentDefaultsHelpFormatter
    pa_args = None
    if defaults:
        parser.set_defaults(**defaults)
        pa_args = []
    args = parser.parse_args(pa_args)
    if args.json:
        new_defaults = json_load(args.json,
                                 lambda x: ObjectView(**x)).as_dict()
        parser.set_defaults(**new_defaults)
        args = parser.parse_args(pa_args)
    if args.thread == 0:
        args.thread = multiprocessing.cpu_count()

    return args


def gen_pcap(defaults=None):
    args = parse_args(defaults)
    conf = json_load(args.conf, object_hook=lambda x: ObjectView(**x))

    if args.random_seed:
        random.seed(args.random_seed)

    if args.pkt_num == 0:
        try:
            get_pktnum = getattr(sys.modules[__name__],
                                 '_get_auto_%s_pktnum' % conf.name)
            args.pkt_num = get_pktnum(conf, args.dir)
        except AttributeError:
            sys.exit("Error: auto pkt-num is not supported by '%s'.\n"
                     % conf.name)

    dir = '%sl' % args.dir[0]
    wargs = []
    worker_num = min(args.pkt_num, args.thread)
    pkt_left = args.pkt_num
    ppw = args.pkt_num // worker_num
    for id in range(worker_num):
        wargs.append((dir, ppw, args.pkt_size, conf, id, worker_num))
        pkt_left -= ppw
    if pkt_left > 0:
        wargs.append((dir, args.pkt_num % worker_num, args.pkt_size, conf,
                      worker_num, worker_num + 1))
        worker_num += 1
    workers = multiprocessing.Pool(worker_num)

    pkts = workers.map(gen_packets, wargs)
    pkts = [p for wpkts in pkts for p in wpkts]
    pkts = map(PicklablePacket.__call__, pkts)

    if args.ascii:
        print("Dumping packets:")
        for p in pkts:
            if sys.stdout.isatty():
                #scapy.config.conf.color_theme = themes.DefaultTheme()
                scapy.config.conf.color_theme = themes.ColorOnBlackTheme()
            # p.show()
            print(p.__repr__())
    else:
        wrpcap(args.output, pkts)


if __name__ == "__main__":
    gen_pcap()
