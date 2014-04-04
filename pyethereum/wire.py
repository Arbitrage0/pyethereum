import struct
import time
import rlp
from utils import big_endian_to_int as idec
from utils import int_to_big_endian as ienc
import logging
from manager import ChainProxy

ienc4 = lambda x: struct.pack('>I', x)  # 4 bytes big endian integer
logger = logging.getLogger(__name__)


def list_ienc(lst):
    "recursively big endian encode all integers in a list"
    nlst = []
    for i, e in enumerate(lst):
        if isinstance(e, list):
            nlst.append(list_ienc(e))
        elif isinstance(e, int):
            nlst.append(ienc(e))
        else:
            nlst.append(e)
    return nlst


def lrlp_decode(data):
    "always return a list"
    d = rlp.decode(data)
    if isinstance(d, str):
        d = [d]
    return d


def load_packet(packet):
    return Packeter.load_packet(packet)


class Packeter(object):
    """
    Translates between the network and the local data
    https://github.com/ethereum/wiki/wiki/%5BEnglish%5D-Wire-Protocol

    stateless!
    """

    cmd_map = dict(((0x00, 'Hello'),
                   (0x01, 'Disconnect'),
                   (0x02, 'Ping'),
                   (0x03, 'Pong'),
                   (0x10, 'GetPeers'),
                   (0x11, 'Peers'),
                   (0x12, 'Transactions'),
                   (0x13, 'Blocks'),
                   (0x14, 'GetChain'),
                   (0x15, 'NotInChain'),
                   (0x16, 'GetTransactions')))
    cmd_map_by_name = dict((v, k) for k, v in cmd_map.items())

    disconnect_reasons_map = dict((
        ('Disconnect requested', 0x00),
        ('TCP sub-system error', 0x01),
        ('Bad protocol', 0x02),
        ('Useless peer', 0x03),
        ('Too many peers', 0x04),
        ('Already connected', 0x05),
        ('Wrong genesis block', 0x06),
        ('Incompatible network protocols', 0x07),
        ('Client quitting', 0x08)))
    disconnect_reasons_map_by_id = \
        dict((v, k) for k, v in disconnect_reasons_map.items())

    # as sent by Ethereum(++)/v0.3.11/brew/Darwin/unknown
    SYNCHRONIZATION_TOKEN = 0x22400891
    PROTOCOL_VERSION = 0x08
    NETWORK_ID = 0
    CLIENT_ID = 'Ethereum(py)/0.0.1'
    CAPABILITIES = 0x01 + 0x02 + 0x04  # node discovery + transaction relaying

    def __init__(self, config):
        self.config = config
        self.CLIENT_ID = self.config.get('network', 'client_id') \
            or self.CLIENT_ID

    @staticmethod
    def load_packet(cls, packet):
        '''
        Though TCP provides a connection-oriented medium, Ethereum nodes
        communicate in terms of packets. These packets are formed as a 4-byte
        synchronisation token (0x22400891), a 4-byte "payload size", to be
        interpreted as a big-endian integer and finally an N-byte
        RLP-serialised data structure, where N is the aforementioned
        "payload size". To be clear, the payload size specifies the number of
        bytes in the packet ''following'' the first 8.

        :return: (success, result), where result should be None when fail,
        and (header, payload_len, cmd, data) when success
        '''
        header = idec(packet[:4])
        if header != cls.SYNCHRONIZATION_TOKEN:
            return False, 'check header failed, skipping message,'\
                'sync token was hex: {0:x}'.format(header)

        try:
            payload_len = idec(packet[4:8])
            payload = lrlp_decode(packet[8:8 + payload_len])
        except Exception as e:
            return False, str(e)

        if (not len(payload)) or (idec(payload[0]) not in cls.cmd_map):
            return False, 'check cmd failed'

        cmd = Packeter.cmd_map.get(idec(payload[0]))
        return True, (header, payload_len, cmd, payload[1:])

    def load_cmd(self, packet):
        success, res = self.load_packet(packet)
        if not success:
            raise Exception(res)
        _, _, cmd, data = res
        return cmd, data

    @staticmethod
    def dump_packet(cls, data):
        """
        4-byte synchronisation token, (0x22400891),
        a 4-byte "payload size", to be interpreted as a big-endian integer
        an N-byte RLP-serialised data structure
        """
        payload = rlp.encode(list_ienc(data))
        packet = ienc4(cls.SYNCHRONIZATION_TOKEN)
        packet += ienc4(len(payload))
        packet += payload
        return packet

    def dump_Hello(self):
        payload = [0x00,
                   self.PROTOCOL_VERSION,
                   self.NETWORK_ID,
                   self.CLIENT_ID,
                   self.config.getint('network', 'listen_port'),
                   self.CAPABILITIES,
                   self.config.get('wallet', 'pub_key')
                   ]
        return self.dump_packet(payload)

    def dump_Ping(self):
        """
        [0x02]
        Requests an immediate reply of Pong from the peer.
        """
        return self.dump_packet([0x02])

    def dump_Pong(self):
        """
        [0x03]
        Reply to peer's Ping packet.
        """
        return self.dump_packet([0x03])

    def dump_Disconnect(self, reason=None):
        """
        [0x01, REASON]
        Inform the peer that a disconnection is imminent; if received, a peer
        should disconnect immediately. When sending, well-behaved hosts give
        their peers a fighting chance (read: wait 2 seconds) to disconnect to
        before disconnecting themselves.
        REASON is an optional integer specifying one of a number of reasons
        """
        assert not reason or reason in self.disconnect_reasons_map

        payload = [0x01]
        if reason:
            payload.append(self.disconnect_reasons_map[reason])
        self.dump_packet(payload)

    def dump_GetPeers(self):
        self.dump_packet([0x10])

    def dump_Peers(self, peers):
        '''
        :param peers: a sequence of (ip, port, pid)
        :return: None if no peers
        '''
        data = [0x11]
        for ip, port, pid in peers:
            ip = list((chr(int(x)) for x in ip.split('.')))
            data.append([ip, port, pid])
        if len(data) > 1:
            self.dump_packet(data)
        else:
            return None

    def dump_Transactions(self, transactions):
        data = [0x12] + [transactions]
        self.dump_packet(data)

    def dump_GetTransactions(self):
        self.dump_packet([0x16])


class WireProtocol(object):
    def __init__(self, peermgr, config):
        self.peermgr = peermgr
        self.packeter = Packeter(config)
        self.chainmgr = ChainProxy()

    def rcv_packet(self, peer, packet):
        try:
            cmd, data = self.packeter.load_cmd(packet)
        except Exception as e:
            logger.warn(e)
            return self.send_Disconnect(peer, reason='Bad protocol')

        # good peer
        peer.last_valid_packet_received = time.time()

        func_name = "_rcv_%s".format(cmd)

        if not hasattr(self, func_name):
            logger.warn('unknown cmd \'{0}\''.format(func_name))
            return
            """
            return self.send_Disconnect(
                peer,
                reason='Incompatible network protocols')
            raise NotImplementedError('%s not implmented')
            """
        # check Hello was sent

        # call the correspondig method
        return getattr(self, func_name)(peer, data)

    def send_Hello(self, peer):
        peer.send_packet(self.packeter.dump_Hello())
        peer.hello_sent = True

    def _rcv_Hello(self, peer, data):
        """
        [0x00, PROTOCOL_VERSION, NETWORK_ID, CLIENT_ID, CAPABILITIES,
        LISTEN_PORT, NODE_ID]
        First packet sent over the connection, and sent once by both sides.
        No other messages may be sent until a Hello is received.
        PROTOCOL_VERSION is one of:
            0x00 for PoC-1;
            0x01 for PoC-2;
            0x07 for PoC-3.
            0x08 sent by Ethereum(++)/v0.3.11/brew/Darwin/unknown
        NETWORK_ID should be 0.
        CLIENT_ID Specifies the client software identity,
            as a human-readable string (e.g. "Ethereum(++)/1.0.0").
        CAPABILITIES specifies the capabilities of the client as a set of
            flags; presently three bits are used:
                0x01 for peers discovery,
                0x02 for transaction relaying,
                0x04 for block-chain querying.
        LISTEN_PORT specifies the port that the client is listening on
                    (on the interface that the present connection traverses).
                    If 0 it indicates the client is not listening.
        NODE_ID is optional and specifies a 512-bit hash,
            (potentially to be used as public key) that identifies this node.

        [574621841, 116, 'Hello', '\x08', '',
        'Ethereum(++)/v0.3.11/brew/Darwin/unknown', '\x07', 'v_',
        "\xc5\xfe\xc6\xea\xe4TKvz\x9e\xdc\xa7\x01\xf6b?\x7fB\xe7\xfc(#t\xe9}"
        "\xafh\xf3Ot'\xe5u\x07\xab\xa3\xe5\x95\x14 |P\xb0C\xa2\xe4jU\xc8z|"
        "\x86\xa6ZV!Q6\x82\xebQ$4+"]

        [574621841, 27, 'Hello', '\x08', '\x00', 'Ethereum(py)/0.0.1', 'vb',
        '\x07']
        """

        # check compatibility
        if idec(data[0]) != self.PROTOCOL_VERSION:
            return self.send_Disconnect(
                peer,
                reason='Incompatible network protocols')

        if idec(data[1]) != self.NETWORK_ID:
            return self.send_Disconnect(peer, reason='Wrong genesis block')

        """
        spec has CAPABILITIES after PORT, CPP client the other way round.
        emulating the latter https://github.com/ethereum/cpp-ethereum/blob
        /master/libethereum/PeerNetwork.cpp#L144
        """

        # TODO add to known peers list
        peer.hello_received = True
        if len(data) == 6:
            peer.node_id = data[5]

        # reply with hello if not send
        if not peer.hello_sent:
            peer.send_packet(peer, self.packeter.dump_Hello())
            peer.hello_sent = True

    def send_Ping(self, peer):
        peer.send_packet(self.packeter.dump_Ping())

    def _rcv_Ping(self, peer, data):
        self.send_Pong(peer)

    def send_Pong(self, peer):
        peer.send_packet(self.packeter.dump_Pong())

    def _rcv_Pong(self, peer, data):
        self.send_GetTransactions(peer)  # FIXME

    def send_Disconnect(self, peer, reason=None):
        peer.send_packet(self.packeter.dump_Disconnect())
        # end connection
        time.sleep(2)
        self.peermgr.remove_peer(peer)

    def _rcv_Disconnect(self, peer, data):
        if len(data):
            reason = self.packeter.disconnect_reasons_map_by_id[idec(data[0])]
            logger.info('{0} sent disconnect, {1} '.format(repr(peer), reason))
        self.peermgr.remove_peer(peer)

    def _rcv_GetPeers(self, peer, data):
        """
        [0x10]
        Request the peer to enumerate some known peers for us to connect to.
        This should include the peer itself.
        """
        self.send_Peers(peer)

    def send_GetPeers(self, peer):
        peer.send_packet(self.packeter.dump_GetPeers())

    def _rcv_Peers(self, peer, data):
        """
        [0x11, [IP1, Port1, Id1], [IP2, Port2, Id2], ... ]
        Specifies a number of known peers. IP is a 4-byte array 'ABCD' that
        should be interpreted as the IP address A.B.C.D. Port is a 2-byte array
        that should be interpreted as a 16-bit big-endian integer.
        Id is the 512-bit hash that acts as the unique identifier of the node.

        IPs look like this: ['6', '\xcc', '\n', ')']
        """
        for ip, port, pid in data:
            assert isinstance(ip, list)
            ip = '.'.join(str(ord(b or '\x00')) for b in ip)
            port = idec(port)
            logger.debug('received peer address: {0}:{1}'.format(ip, port))
            self.peermgr.add_peer_address(ip, port, pid)

    def send_Peers(self, peer):
        packet = self.packeter.dump_Peers(
            self.peermgr.get_known_peer_addresses())
        if packet:
            peer.send_packet()

    def _rcv_Blocks(self, peer, data):
        """
        [0x13, [block_header, transaction_list, uncle_list], ... ]
        Specify (a) block(s) that the peer should know about. The items in the
        list (following the first item, 0x13) are blocks in the format
        described in the main Ethereum specification.
        """

        # FIXME, currently just dumps data
        for e in data:
            header, transaction_list, uncle_list = e
            logger.debug('received block:  parent:{0}'
                         .format(header[0].encode('hex')))

    def _rcv_Transactions(self, peer, data):
        """
        [0x12, [nonce, receiving_address, value, ... ], ... ] Specify (a)
        transaction(s) that the peer should make sure is included on its
        transaction queue. The items in the list (following the first item
        0x12) are transactions in the format described in the main Ethereum
        specification.
        """
        logger.info('received transactions', len(data), peer)
        self.chainmgr.addTransactions(data)

    def send_Transactions(self, peer, transactions):
        peer.send_transaction(self.packeter.dump_Transactions(transactions))

    def _rcv_GetTransactions(self, peer):
        """
        [0x16]
        Request the peer to send all transactions currently in the queue.
        See Transactions.
        """
        logger.debug('received get_transaction', peer)
        self.chainmgr.request_transactions(peer.id())

    def send_GetTransactions(self, peer):
        logger.info('asking for transactions')
        peer.send_packet(self.packeter.dump_GetTransactions())

    def _broadcast(self, method, data):
        for peer in self.peermgr.connected_peers:
            method(peer, data)

    def process_chainmanager_queue(self):

        while True:
            msg = self.chainmgr.pop_message()
            if not msg:
                return
            cmd, data = msg[0], msg[1:]
            logger.debug('%r received %s datalen:%d' % (self, cmd, len(data)))

            if cmd == "send_transactions":
                transaction_list = data[0]
                peer = self.peermgr.get_peer_by_id(data[1])
                if peer:
                    self.send_Transactions(peer, transaction_list)
                else:  # broadcast
                    self._broadcast(self.send_Transactions, transaction_list)

            elif cmd == 'pingpong':
                reply = data[0]
                logger.debug('%r received pingpong(reply=%r)' % (self, reply))
                if reply:
                    self.chainmgr.pingpong()
            else:
                raise Exception('unknown commad')
