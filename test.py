from binascii import unhexlify, hexlify
from btcproxy import ProxiedBitcoinD
from eclair import EclairNode
from ephemeral_port_reserve import reserve
from hashlib import sha256
from itertools import product
from lightningd import LightningNode
from lnaddr import lndecode
from lnd import LndNode
from ptarmd import PtarmNode
from concurrent import futures
from utils import BitcoinD, BtcD
from bech32 import bech32_decode
from electrumutils import ElectrumX, ElectrumNode

from fixtures import *

import logging
import os
import pytest
import sys
import tempfile
import time

impls = [EclairNode, LightningNode, LndNode, PtarmNode, ElectrumNode]

if TEST_DEBUG:
    logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
logging.info("Tests running in '%s'", TEST_DIR)


def transact_and_mine(btc):
    """ Generate some transactions and blocks.

    To make bitcoind's `estimatesmartfee` succeeded.
    """
    addr = btc.rpc.getnewaddress()
    for i in range(10):
        for j in range(10):
            txid = btc.rpc.sendtoaddress(addr, 0.5)
        btc.rpc.generate(1)


def wait_for(success, timeout=30, interval=1):
    start_time = time.time()
    while not success() and time.time() < start_time + timeout:
        time.sleep(interval)
    if time.time() > start_time + timeout:
        raise ValueError("Error waiting for {}", success)


def sync_blockheight(btc, nodes):
    info = btc.rpc.getblockchaininfo()
    blocks = info['blocks']

    print("Waiting for %d nodes to blockheight %d" % (len(nodes), blocks))
    for n in nodes:
        wait_for(lambda: n.info()['blockheight'] == blocks, interval=1)


def generate_until(btc, success, blocks=30, interval=1):
    """Generate new blocks until `success` returns true.

    Mainly used to wait for transactions to confirm since they might
    be delayed and we don't want to add a long waiting time to all
    tests just because some are slow.
    """
    for i in range(blocks):
        time.sleep(interval)
        if success():
            return
        btc.rpc.generate(1)
    time.sleep(interval)
    if not success():
        raise ValueError("Generated %d blocks, but still no success", blocks)


def idfn(impls):
    return "_".join([i.displayName for i in impls])


@pytest.mark.parametrize("impl", impls, ids=idfn)
def test_start(bitcoind, node_factory, impl):
    node = node_factory.get_node(implementation=impl)
    assert node.ping()
    sync_blockheight(bitcoind, [node])


@pytest.mark.parametrize("impls", product(impls, repeat=2), ids=idfn)
def test_connect(node_factory, bitcoind, impls):
    node1 = node_factory.get_node(implementation=impls[0])
    node2 = node_factory.get_node(implementation=impls[1])

    # Needed by lnd in order to have at least one block in the last 2 hours
    bitcoind.rpc.generate(1)

    print("Connecting {}@{}:{} -> {}@{}:{}".format(
        node1.id(), 'localhost', node1.daemon.port,
        node2.id(), 'localhost', node2.daemon.port))
    node1.connect('localhost', node2.daemon.port, node2.id())

    wait_for(lambda: node1.peers(), timeout=5)
    wait_for(lambda: node2.peers(), timeout=5)

    # TODO(cdecker) Check that we are connected
    assert node1.id() in node2.peers()
    assert node2.id() in node1.peers()


def confirm_channel(bitcoind, n1, n2):
    print("Waiting for channel {} -> {} to confirm".format(n1.id(), n2.id()))
    assert n1.id() in n2.peers()
    assert n2.id() in n1.peers()
    for i in range(10):
        time.sleep(2)
        if n1.check_channel(n2) and n2.check_channel(n1):
            print("Channel {} -> {} confirmed".format(n1.id(), n2.id()))
            return True
        bhash = bitcoind.rpc.generate(1)[0]
        n1.block_sync(bhash)
        n2.block_sync(bhash)

    # Last ditch attempt
    return n1.check_channel(n2) and n2.check_channel(n1)


@pytest.mark.parametrize("impls", product(impls, repeat=2), ids=idfn)
def test_open_channel(bitcoind, node_factory, impls):
    node1 = node_factory.get_node(implementation=impls[0])
    node2 = node_factory.get_node(implementation=impls[1])

    node1.connect('localhost', node2.daemon.port, node2.id())

    wait_for(lambda: node1.peers(), interval=1)
    wait_for(lambda: node2.peers(), interval=1)

    node1.addfunds(bitcoind, 2 * 10**7)

    node1.openchannel(node2.id(), 'localhost', node2.daemon.port, 10**7)
    time.sleep(1)
    bitcoind.rpc.generate(2)

    assert confirm_channel(bitcoind, node1, node2)

    assert(node1.check_channel(node2))
    assert(node2.check_channel(node1))

    # Generate some more, to reach the announcement depth
    bitcoind.rpc.generate(4)


@pytest.mark.parametrize("impls", product(impls, repeat=2), ids=idfn)
def test_gossip(node_factory, bitcoind, impls):
    """ Create a network of lightningd nodes and connect to it using 2 new nodes
    """
    # These are the nodes we really want to test
    node1 = node_factory.get_node(implementation=impls[0])
    node2 = node_factory.get_node(implementation=impls[1])

    # Using lightningd since it is quickest to start up
    nodes = [node_factory.get_node(implementation=LightningNode) for _ in range(5)]
    for n1, n2 in zip(nodes[:4], nodes[1:]):
        n1.connect('localhost', n2.daemon.port, n2.id())
        n1.addfunds(bitcoind, 2 * 10**7)
        n1.openchannel(n2.id(), 'localhost', n2.daemon.port, 10**7)
        assert confirm_channel(bitcoind, n1, n2)

    time.sleep(5)
    bitcoind.rpc.generate(30)
    time.sleep(5)

    # Wait for gossip to settle
    for n in nodes:
        wait_for(lambda: len(n.getnodes()) == 5, interval=1, timeout=120)
        wait_for(lambda: len(n.getchannels()) == 8, interval=1, timeout=120)

    # Now connect the first node to the line graph and the second one to the first
    node1.connect('localhost', nodes[0].daemon.port, nodes[0].id())
    node2.connect('localhost', n1.daemon.port, n1.id())

    # They should now be syncing as well
    # TODO(cdecker) Uncomment the following line when eclair exposes non-local channels as well (ACINQ/eclair/issues/126)
    #wait_for(lambda: len(node1.getchannels()) == 8)
    wait_for(lambda: len(node1.getnodes()) == 5, interval=1)

    # Node 2 syncs through node 1
    # TODO(cdecker) Uncomment the following line when eclair exposes non-local channels as well (ACINQ/eclair/issues/126)
    #wait_for(lambda: len(node2.getchannels()) == 8)
    wait_for(lambda: len(node2.getnodes()) == 5, interval=1)


@pytest.mark.parametrize("impl", impls, ids=idfn)
def test_invoice_decode(node_factory, impl):
    capacity = 10**7
    node1 = node_factory.get_node(implementation=impl)

    amount = capacity // 10 * 1000
    payment_request = node1.invoice(amount)
    hrp, data = bech32_decode(payment_request)

    assert hrp and data
    assert hrp.startswith('lnbcrt')

def open_channel_get_invoice(bitcoind, node_factory, impls):
    node1 = node_factory.get_node(implementation=impls[0])
    node2 = node_factory.get_node(implementation=impls[1])
    capacity = 10**7

    node1.connect('localhost', node2.daemon.port, node2.id())

    wait_for(lambda: node1.peers(), interval=1)
    wait_for(lambda: node2.peers(), interval=1)

    node1.addfunds(bitcoind, 2*capacity)
    time.sleep(5)
    bitcoind.rpc.generate(10)
    time.sleep(5)

    txid, csv_delay_imposed_by_remote = node1.openchannel(node2.id(), 'localhost', node2.daemon.port, capacity)
    time.sleep(1)
    mined = bitcoind.rpc.generate(6)

    assert txid in bitcoind.rpc.getblock(mined[0])['tx']
    print('funding tx in block', mined[0])

    sync_blockheight(bitcoind, [node1, node2])
    assert confirm_channel(bitcoind, node1, node2)

    return csv_delay_imposed_by_remote, capacity, node1, node2

@pytest.mark.parametrize("impls", product(impls, repeat=2), ids=idfn)
def test_redeem_htlc_funds(bitcoind, node_factory, impls):
    csv_delay_imposed_by_remote, capacity, node1, node2 = open_channel_get_invoice(bitcoind, node_factory, impls)

    old_bal = sum(node1.wallet.get_balance())

    def add_one_htlc(amount):
        req = node2.invoice(amount)
        node1.add_htlc(req)

    add_one_htlc(capacity // 4 * 1000)
    add_one_htlc(capacity // 4 * 1000 + 1000)

    htlcs = node2.pending_htlcs(node1)
    assert len(htlcs) == 2

    print('htlcs', htlcs)

    node2.daemon.stop()

    gen = node1.force_close(node2)
    closing_txid = next(gen)
    time.sleep(1)

    expiration = htlcs[0].expiration_height
    local_height = node1.info()['blockheight']
    diff = expiration-local_height

    print(f"expiration: {expiration}, local_height: {local_height}, diff: {diff}")

    block_hash = bitcoind.rpc.generate(1)[0]
    txids = bitcoind.rpc.getblock(block_hash)['tx']
    assert closing_txid in txids
    wait_for(lambda: max(node1.tx_heights([closing_txid]).values()) > 0)

    block_hashes = bitcoind.rpc.generate(diff)
    h1 = node1.get_published_e_tx()
    h2 = node1.get_published_e_tx()
    assert h1.name.startswith('our_ctx_htlc_tx')
    assert h2.name.startswith('our_ctx_htlc_tx')
    def wait_for_txs(txs):
        nonlocal block_hashes
        while True:
            txid_list = bitcoind.rpc.getblock(block_hashes[0])['tx']
            if all(x in txid_list for x in txs):
                break
            block_hashes = bitcoind.rpc.generate(1)
    wait_for_txs([h1.tx.txid(), h2.tx.txid()])

    block_hashes = bitcoind.rpc.generate(csv_delay_imposed_by_remote)
    published = node1.get_published_e_tx()
    if published.name.startswith('our_ctx_to_local'):
        published = node1.get_published_e_tx()
    assert published.name.startswith('second_stage')
    wait_for_txs([published.tx.txid()])

    block_hashes = bitcoind.rpc.generate(101)
    print("second stage stage closure", next(gen))

    matured, unconfirmed, unmatured = node1.wallet.get_balance()

    # the 0.5 are fees
    should_have = old_bal + capacity - 0.5 * 10**6

    print(f'new balance: matured {matured}, unmatured {unmatured}, unconfirmed {unconfirmed}')
    print('old balance', old_bal)
    print("should have", should_have)
    print("currently have", matured)
    assert matured > should_have


@pytest.mark.parametrize("impls", product(impls, repeat=2), ids=idfn)
def test_direct_payment(bitcoind, node_factory, impls):
    _csv_delay, capacity, node1, node2 = open_channel_get_invoice(bitcoind, node_factory, impls)

    amount = capacity // 10 * 1000
    req = node2.invoice(amount)
    dec = lndecode(req)

    print("Decoded payment request", req, dec)
    payment_key = node1.send(req)
    assert(sha256(unhexlify(payment_key)).digest() == dec.paymenthash)


def gossip_is_synced(nodes, num_channels):
    print("Checking %d nodes for gossip sync" % (len(nodes)))
    for i, n in enumerate(nodes):
        node_chans = n.getchannels()
        logging.debug("Node {} knows about the following channels {}".format(i, node_chans))
        if len(node_chans) != num_channels:
            print("Node %d is missing %d channels" % (i, num_channels - len(node_chans)))
            return False
    return True


def check_channels(pairs):
    ok = True
    logging.debug("Checking all channels between {}".format(pairs))
    for node1, node2 in pairs:
        ok &= node1.check_channel(node2)
        ok &= node2.check_channel(node1)
    return ok


def node_has_route(node, channels):
    """Check whether a node knows about a specific route.

    The route is a list of node_id tuples
    """
    return set(channels).issubset(set(node.getchannels()))


@pytest.mark.parametrize("impls", product(impls, repeat=3), ids=idfn)
def test_forwarded_payment(bitcoind, node_factory, impls):
    num_nodes = len(impls)
    nodes = [node_factory.get_node(implementation=impls[i]) for i in range(3)]
    capacity = 10**7

    for i in range(num_nodes-1):
        nodes[i].connect('localhost', nodes[i+1].daemon.port, nodes[i+1].id())
        nodes[i].addfunds(bitcoind, 4 * capacity)

    for i in range(num_nodes-1):
        nodes[i].openchannel(nodes[i+1].id(), 'localhost', nodes[i+1].daemon.port, capacity)
        assert confirm_channel(bitcoind, nodes[i], nodes[i+1])

    bitcoind.rpc.generate(6)
    sync_blockheight(bitcoind, nodes)

    # Make sure we have a path
    ids = [n.info()['id'] for n in nodes]
    route = [(ids[i-1], ids[i]) for i in range(1, len(ids))]
    wait_for(lambda: node_has_route(nodes[0], route), timeout=120)
    sync_blockheight(bitcoind, nodes)

    src = nodes[0]
    dst = nodes[len(nodes)-1]
    amount = capacity // 10 * 1000
    req = dst.invoice(amount)

    print("Waiting for a route to be found")
    wait_for(lambda: src.check_route(dst.id(), amount), timeout=120)

    payment_key = src.send(req)
    dec = lndecode(req)
    assert(sha256(unhexlify(payment_key)).digest() == dec.paymenthash)


@pytest.mark.parametrize("impls", product(impls, repeat=2), ids=idfn)
def test_reconnect(bitcoind, node_factory, impls):
    node1 = node_factory.get_node(implementation=impls[0])
    node2 = node_factory.get_node(implementation=impls[1])
    capacity = 10**7

    node1.connect('localhost', node2.daemon.port, node2.id())

    wait_for(lambda: node1.peers(), interval=1)
    wait_for(lambda: node2.peers(), interval=1)

    node1.addfunds(bitcoind, 2*capacity)
    time.sleep(5)
    bitcoind.rpc.generate(10)
    time.sleep(5)

    node1.openchannel(node2.id(), 'localhost', node2.daemon.port, capacity)

    for i in range(30):
        node1.bitcoin.rpc.generate(1)
        time.sleep(1)

    wait_for(lambda: node1.check_channel(node2))
    wait_for(lambda: node2.check_channel(node1))
    sync_blockheight(bitcoind, [node1, node2])

    amount = capacity // 10 * 1000
    req = node2.invoice(amount)
    payment_key = node1.send(req)
    dec = lndecode(req)
    assert(sha256(unhexlify(payment_key)).digest() == dec.paymenthash)

    print("Sleep before restart")
    time.sleep(5)

    print("Restarting")
    node2.restart()

    time.sleep(15)

    wait_for(lambda: node1.check_channel(node2))
    wait_for(lambda: node2.check_channel(node1))
    sync_blockheight(bitcoind, [node1, node2])

    time.sleep(15)

    req = node2.invoice(amount)
    payment_key = node1.send(req)
    dec = lndecode(req)
    assert(sha256(unhexlify(payment_key)).digest() == dec.paymenthash)
