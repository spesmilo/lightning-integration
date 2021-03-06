import asyncio
import binascii
import logging
import os
import sys
import tempfile
import threading
import time
import queue

from electrum import constants, simple_config, util
from electrum.daemon import Daemon
from electrum.storage import WalletStorage
from electrum.wallet import Wallet
from electrum.lnutil import REMOTE
from electrum.address_synchronizer import TX_HEIGHT_LOCAL
from electrum.transaction import Transaction
from electrum.lnwatcher import ListenerItem

from electrumx.server.controller import Controller
from electrumx.server.env import Env

from ephemeral_port_reserve import reserve
from utils import BITCOIND_CONFIG

bh2u = lambda x: binascii.hexlify(x).decode('ascii')

class ElectrumX:
    def __init__(self, directory, bitcoind):
        self.directory = directory
        self.bitcoind = bitcoind
        self.logger = logging.getLogger('electrumx')
        self.logger.info("__init__")
        self.loop = asyncio.new_event_loop()
        self.evt = asyncio.Event(loop=self.loop)

    def kill(self):
        self.logger.info("kill")
        async def set_evt():
            self.evt.set()
        asyncio.run_coroutine_threadsafe(set_evt(), self.loop)

    def start(self):
        self.logger.info("start")
        self.tcp_port = reserve()
        self.rpc_port = reserve()
        os.environ.update({
            "COIN": "BitcoinSegwit",
            "TCP_PORT": str(self.tcp_port),
            "RPC_PORT": str(self.rpc_port),
            "NET": "regtest",
            "DAEMON_URL": "http://rpcuser:rpcpass@127.0.0.1:" + str(self.bitcoind.rpcport),
            "DB_DIRECTORY": self.directory,
            "MAX_SESSIONS": "50",
        })
        def target():
            loop = self.loop
            asyncio.set_event_loop(loop)
            env = Env()
            env.loop_policy = asyncio.get_event_loop_policy()
            self.controller = Controller(env)
            logging.basicConfig(level=logging.DEBUG)
            loop.run_until_complete(asyncio.wait([self.controller.serve(self.evt), self.evt.wait()], return_when=asyncio.FIRST_COMPLETED))
            loop.close()

        self.thread = threading.Thread(target=target)
        self.thread.start()

    def wait(self):
        self.logger.info("wait")
        self.thread.join()

class ElectrumDaemon:
    def __init__(self, electrumx, port):
        self.logger = logging.getLogger('electrum-daemon')
        self.directory = electrumx.directory
        self.electrumx = electrumx
        self.port = port
        self.actual = None

    def start(self):
        self.logger.info("start")
        user_config = {
            "auto_connect": False,
            "oneserver": True,
            "server": "localhost:" + str(self.electrumx.tcp_port) + ":t",
            "request_initial_sync": False,
            "lightning_listen": "127.0.0.1:" + str(self.port),
        }
        config = simple_config.SimpleConfig(user_config, read_user_dir_function=lambda: self.directory)
        constants.set_regtest()
        self.actual = Daemon(config)
        assert self.actual.network.asyncio_loop.is_running()
        wallet_path = self.actual.cmd_runner.create(segwit=True)['path']
        storage = WalletStorage(wallet_path)
        wallet = Wallet(storage)
        wallet.start_network(self.actual.network)
        self.actual.add_wallet(wallet)

    def stop(self):
        if not self.actual:
            return
        next(iter(self.actual.wallets.values())).stop_threads()
        self.actual.stop()

class ElectrumNode:
    displayName = "electrum"

    def __init__(self, lightning_dir, lightning_port, btc, executor=None, node_id=0, get_electrumx=None):
        self.logger = logging.getLogger('electrum-node')
        self.logger.info("__init__")
        self.electrumx = get_electrumx()
        self.lightning_port = lightning_port
        self.daemon = ElectrumDaemon(self.electrumx, lightning_port)
        self.broadcasted_encumbered_txs = queue.Queue()

    @property
    def wallet(self):
        wallets = list(self.daemon.actual.wallets.values())
        assert len(wallets) == 1
        return wallets[0]

    def peers(self):
        self.logger.info("peers")
        return [bh2u(x) for x in self.wallet.lnworker.peers.keys()]

    def id(self):
        return bh2u(self.wallet.lnworker.node_keypair.pubkey)

    def openchannel(self, node_id, host, port, satoshis):
        self.logger.info("openchannel")
        # second parameter is local_amt_sat not capacity!
        r = self.wallet.lnworker.open_channel(node_id, satoshis, 0)
        self.logger.info("open channel result {}".format(r))
        chan = self.chan(node_id)
        csv_delay = chan.config[REMOTE].to_self_delay
        funding_txid = chan.funding_outpoint.txid
        return funding_txid, csv_delay

    def chan(self, node_id):
        chan = next(iter(self.wallet.lnworker.channels.values()))
        assert chan.node_id == bytes.fromhex(node_id), (bh2u(chan.node_id), node_id)
        return chan

    def addfunds(self, bitcoind, satoshis):
        self.logger.info("addfunds")
        addr = self.wallet.get_unused_address()
        assert addr
        matured, unconfirmed, unmatured = self.wallet.get_addr_balance(addr)
        assert matured + unconfirmed + unmatured == 0
        assert addr is not None
        bitcoind.rpc.sendtoaddress(addr, float(satoshis) / 10**8)
        bitcoind.rpc.generate(1)
        while True:
            matured, unconfirmed, unmatured = self.wallet.get_addr_balance(addr)
            if matured + unmatured != 0:
                break
            self.logger.info('still waiting, have {} {} {}'.format(matured, unconfirmed, unmatured))
            time.sleep(1)
        self.logger.info("funds added!")

    def ping(self):
        self.logger.info("ping")
        return True

    def check_channel(self, remote):
        self.logger.info("check_channel")
        try:
            chan = self.chan(remote.id())
        except StopIteration:
            return False
        else:
            return chan.get_state() == "OPEN"

    def getchannels(self):
        self.logger.info("getchannels")
        channel_infos = self.wallet.network.path_finder.channel_db._id_to_channel_info.values()
        channels = set()
        for chan_info in channel_infos:
            channels.add((bh2u(chan_info.node_id_1), bh2u(chan_info.node_id_2)))
            channels.add((bh2u(chan_info.node_id_2), bh2u(chan_info.node_id_1)))
        return channels

    def getnodes(self):
        """ set of graph pubkeys excluding self """
        self.logger.info("getnodes")
        channel_infos = self.wallet.network.path_finder.channel_db._id_to_channel_info.values()
        nodes = set()
        for chan_info in channel_infos:
            nodes.add(bh2u(chan_info.node_id_1))
            nodes.add(bh2u(chan_info.node_id_2))
        nodes.remove(self.wallet.lnworker.node_keypair.pubkey)
        return nodes

    def invoice(self, amount):
        self.logger.info("invoice")
        return self.wallet.lnworker.add_invoice(amount, "cup of coffee")

    def add_htlc(self, req):
        self.logger.info("add_htlc")
        addr, peer, coro = self.wallet.lnworker.pay(req)
        coro.result(5)
        return addr, peer

    def send(self, req):
        self.logger.info("send")
        addr, peer = self.add_htlc(req)
        coro = peer.payment_preimages[addr.paymenthash].get()
        preimage = asyncio.run_coroutine_threadsafe(coro, self.wallet.network.asyncio_loop).result(5)
        return bh2u(preimage)

    def connect(self, host, port, node_id):
        coro = self.wallet.lnworker.add_peer(host, port, bytes.fromhex(node_id))
        fut = asyncio.run_coroutine_threadsafe(coro, asyncio.get_event_loop())
        fut.result(5)

    def info(self):
        local_height = self.daemon.actual.network.get_local_height()
        self.logger.info("info: height: {}".format(local_height))
        return {'blockheight': local_height, 'id': self.id()}

    def block_sync(self, blockhash):
        self.logger.info("block_sync")
        time.sleep(1)

    def restart(self):
        self.logger.info("restart")
        self.daemon.stop()
        time.sleep(5)
        self.daemon = ElectrumDaemon(self.electrumx, self.lightning_port)
        self.daemon.start()

    def check_route(self, node_id, amount_sat):
        net = self.wallet.network
        method = net.path_finder.find_path_for_payment
        async def f():
            return method(bytes.fromhex(self.id()), bytes.fromhex(node_id), amount_sat * 1000)
        coro = asyncio.run_coroutine_threadsafe(f(), net.asyncio_loop)
        return coro.result()

    def get_published_e_tx(self):
        return self.broadcasted_encumbered_txs.get(timeout=30)

    def force_close(self, remote):
        chan = self.chan(remote.id())
        chan_id = chan.channel_id
        loop = self.wallet.network.asyncio_loop
        item = chan.lnwatcher.tx_progress[chan.funding_outpoint.to_str()] = ListenerItem(all_done=asyncio.Event(loop=loop), tx_queue=asyncio.Queue(loop=loop))
        async def watch_queue():
            seen = set()
            while True:
                e_tx = await item.tx_queue.get()
                if e_tx in seen:
                    continue
                seen.add(e_tx)
                self.broadcasted_encumbered_txs.put(e_tx)
        coro = asyncio.run_coroutine_threadsafe(watch_queue(), loop)

        coro = asyncio.run_coroutine_threadsafe(self.wallet.lnworker.force_close_channel(chan_id), loop)
        yield coro.result(5)
        yield asyncio.run_coroutine_threadsafe(item.all_done.wait(), loop).result(30)

    @property
    def addr_sync(self):
        return self.daemon.actual.network.lnwatcher

    def tx_heights(self, txs, wallet=False, seconds=5):
        """ returns a dict that maps txids to their confirmation heights in
        the address synchronizer of the LNWatcher if wallet is False,
        or the wallet, if wallet is True
        """
        addr_sync = self.addr_sync if not wallet else self.wallet
        async def f():
            return {txid: addr_sync.get_tx_height(txid).height for txid in txs}
        return asyncio.run_coroutine_threadsafe(f(), self.wallet.network.asyncio_loop).result(seconds)
