#!/usr/bin/env python2
# Copyright (c) 2015 The Bitcoin Core developers
# Distributed under the MIT/X11 software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
#

from test_framework.test_framework import ComparisonTestFramework
from test_framework.util import *
from test_framework.mininode import CTransaction, NetworkThread
from test_framework.blocktools import create_coinbase, create_block
from test_framework.comptool import TestInstance, TestManager
from test_framework.script import CScript, OP_1NEGATE, OP_NOP3, OP_DROP
from binascii import hexlify, unhexlify
import cStringIO
import time

def mtp_invalidate(tx, blockTime):
    '''Modify the nLockTime to make it fails once MTP rule is activated
    '''
    # Disable Sequence lock, Activate nLockTime
    tx.vin[0].nSequence = 0x90FFFFFF
    tx.nLockTime = blockTime - 1

'''
This test is meant to exercise BIP113 (Median Time Past for nLockTime)
Connect to a single node.

regtest lock-in with 108/144 block signalling
activation after a further 144 blocks

mine 2 block and save coinbases for later use
mine 141 blocks to transition from DEFINED to STARTED
mine 100 blocks signalling readiness and 44 not in order to fail to change state this period
mine 108 blocks signalling readiness and 36 blocks not signalling readiness (STARTED->LOCKED_IN)
mine a further 143 blocks (LOCKED_IN)
test that enforcement has not triggered (which triggers ACTIVE)
test that enforcement has triggered
'''

class BIP113Test(ComparisonTestFramework):

    def __init__(self):
        self.num_nodes = 1

    def setup_network(self):
        # Must set the blockversion for this test
        self.nodes = start_nodes(1, self.options.tmpdir,
                                 extra_args=[['-debug', '-whitelist=127.0.0.1', '-blockversion=4']],
                                 binary=[self.options.testbinary])

    def run_test(self):
        test = TestManager(self, self.options.tmpdir)
        test.add_all_connections(self.nodes)
        NetworkThread().start() # Start up network handling in another thread
        test.run()

    def create_transaction(self, node, coinbase, to_address, amount):
        from_txid = node.getblock(coinbase)['tx'][0]
        inputs = [{ "txid" : from_txid, "vout" : 0}]
        outputs = { to_address : amount }
        rawtx = node.createrawtransaction(inputs, outputs)
        tx = CTransaction()
        f = cStringIO.StringIO(unhexlify(rawtx))
        tx.deserialize(f)
        return tx

    def sign_transaction(self, node, tx):
        signresult = node.signrawtransaction(hexlify(tx.serialize()))
        tx = CTransaction()
        f = cStringIO.StringIO(unhexlify(signresult['hex']))
        tx.deserialize(f)
        return tx

    def generate_blocks(self, number, version, test_blocks = []):
        for i in xrange(number):
            block = create_block(self.tip, create_coinbase(self.height), self.last_block_time + 1)
            block.nVersion = version
            block.rehash()
            block.solve()
            test_blocks.append([block, True])
            self.last_block_time += 1
            self.tip = block.sha256
            self.height += 1
        return test_blocks

    def get_bip9_status(self, key):
        info = self.nodes[0].getblockchaininfo()
        for row in info['bip9_softforks']:
            if row['id'] == key:
                return row
        raise IndexError ('key:"%s" not found' % key)

    def get_tests(self):

        self.coinbase_blocks = self.nodes[0].generate(2)
        self.height = 3  # height of the next block to build
        self.tip = int ("0x" + self.nodes[0].getbestblockhash() + "L", 0)
        self.nodeaddress = self.nodes[0].getnewaddress()
        self.last_block_time = time.time()

        assert_equal(self.get_bip9_status('csv')['status'], 'defined')

        # Test 1
        # Advance from DEFINED to STARTED
        test_blocks = self.generate_blocks(141, 4)
        yield TestInstance(test_blocks, sync_every_block=False)

        assert_equal(self.get_bip9_status('csv')['status'], 'started')

        # Test 2
        # Fail to achieve LOCKED_IN 100 out of 144 signal bit 1
        # using a variety of bits to simulate multiple parallel softforks
        test_blocks = self.generate_blocks(50, 536870913) # 0x20000001 (signalling ready)
        test_blocks = self.generate_blocks(20, 4, test_blocks) # 0x00000004 (signalling not)
        test_blocks = self.generate_blocks(50, 536871169, test_blocks) # 0x20000101 (signalling ready)
        test_blocks = self.generate_blocks(24, 536936448, test_blocks) # 0x20010000 (signalling not)
        yield TestInstance(test_blocks, sync_every_block=False)

        assert_equal(self.get_bip9_status('csv')['status'], 'started')

        # Test 3
        # 108 out of 144 signal bit 1 to achieve lock-in
        # using a variety of bits to simulate multiple parallel softforks
        test_blocks = self.generate_blocks(58, 536870913) # 0x20000001 (signalling ready)
        test_blocks = self.generate_blocks(26, 4, test_blocks) # 0x00000004 (signalling not)
        test_blocks = self.generate_blocks(50, 536871169, test_blocks) # 0x20000101 (signalling ready)
        test_blocks = self.generate_blocks(10, 536936448, test_blocks) # 0x20010000 (signalling not)
        yield TestInstance(test_blocks, sync_every_block=False)

        assert_equal(self.get_bip9_status('csv')['status'], 'locked_in')

        # Test 4
        # 143 more version 536870913 blocks (waiting period-1)
        test_blocks = self.generate_blocks(143, 4)
        yield TestInstance(test_blocks, sync_every_block=False)

        assert_equal(self.get_bip9_status('csv')['status'], 'locked_in')

        # Test 5
        # Check that the new MTP rules are not enforced
        spendtx = self.create_transaction(self.nodes[0], self.coinbase_blocks[0], self.nodeaddress, 1.0)
        mtp_invalidate(spendtx, self.last_block_time + 1)
        spendtx = self.sign_transaction(self.nodes[0], spendtx)
        spendtx.rehash()

        block = create_block(self.tip, create_coinbase(self.height), self.last_block_time + 1)
        block.nVersion = 536870913
        block.vtx.append(spendtx)
        block.hashMerkleRoot = block.calc_merkle_root()
        block.rehash()
        block.solve()

        self.last_block_time += 1
        self.tip = block.sha256
        self.height += 1
        yield TestInstance([[block, True]])

        assert_equal(self.get_bip9_status('csv')['status'], 'active')

        # Test 6
        # Check that the new MTP rules are enforced
        spendtx = self.create_transaction(self.nodes[0], self.coinbase_blocks[1], self.nodeaddress, 1.0)
        mtp_invalidate(spendtx, self.last_block_time + 1)
        spendtx = self.sign_transaction(self.nodes[0], spendtx)
        spendtx.rehash()

        block = create_block(self.tip, create_coinbase(self.height), self.last_block_time + 1)
        block.nVersion = 536870913
        block.vtx.append(spendtx)
        block.hashMerkleRoot = block.calc_merkle_root()
        block.rehash()
        block.solve()
        self.last_block_time += 1
        yield TestInstance([[block, False]])

if __name__ == '__main__':
    BIP113Test().main()
